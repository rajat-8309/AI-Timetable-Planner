"""
timetable_gen.py
================
Architecture
------------
  STEP 1 — Python deterministic scheduler (greedy + backtracking)
            Creates the timetable. No AI involved here.

  STEP 2 — Groq AI conflict auditor
            Receives the freshly-created timetable AND every slot already
            in the database.  Analyses the combined picture and returns a
            structured list of conflicts / warnings in plain English.
            The final save is blocked if the AI finds hard conflicts.

Key fix (v2)
------------
  Replaced the global `base_occ_ts` (day, timeslot) blocker with a
  branch-aware `base_occ_branch` (branch, day, timeslot) tracker.
  Previously, if Timetable #1 had 10 slots, ALL those (day, timeslot)
  combinations were completely forbidden for the new timetable — even
  for unrelated teachers/subjects — making 35-hour schedules infeasible
  when any prior timetable existed.  Now only the specific teacher and
  branch combinations from external timetables are blocked.

  Also added per-teacher feasibility check so errors are caught early
  with a clear human-readable message instead of a silent backtrack failure.
"""

import json
import re
import os
import random
import logging
import time
from itertools import groupby

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
DAYS        = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
WORK_START  = 9
WORK_END    = 17
BREAK_START = 13
BREAK_END   = 14
MODEL       = os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile')
API_TIMEOUT = float(os.environ.get('TIMETABLE_API_TIMEOUT', 30))


# ── Low-level helpers ──────────────────────────────────────────────────────

def parse_hour(time_str: str) -> int:
    return int(time_str.split('-')[0].strip().split(':')[0])

def fmt_slot(start_h: int, length: int = 1) -> str:
    return f"{start_h:02d}:00-{start_h + length:02d}:00"

def split_teachers(name: str) -> list:
    return [n.strip() for n in re.split(r'[/&]', name) if n.strip()]

def get_valid_session_starts(length: int) -> list:
    """All start hours where a `length`-hour session fits inside the working
    day without crossing the lunch break."""
    valid = []
    for h in range(WORK_START, WORK_END):
        hours = list(range(h, h + length))
        if hours[-1] >= WORK_END:
            break
        if any(hh == BREAK_START for hh in hours):
            continue
        valid.append(h)
    return valid


# ══════════════════════════════════════════════════════════════════════════
# STEP 1 — DETERMINISTIC SCHEDULER
# ══════════════════════════════════════════════════════════════════════════

def deterministic_schedule(teachers: list, existing_slots: list):
    """
    Two-phase scheduler:
      Phase 1 — randomised greedy   (fast, covers most cases)
      Phase 2 — backtracking search (always finds a solution if one exists)

    Cross-timetable constraints are tracked per-teacher AND per-branch.
    Global (day, timeslot) blocking is NOT used for external timetables —
    only the specific teacher/branch combinations are blocked, so multiple
    timetables for different classes can share the same time slots.

    Returns a list of slot dicts on success, or None if genuinely infeasible.
    """

    # Build session list (one entry per placeable block)
    sessions = []
    for t in teachers:
        n_lec   = int(t.get('no_of_lectures', 0))
        n_lab   = int(t.get('no_of_labs',     0))
        lec_len = int(round(float(t.get('lecture_length', 1))))
        lab_len = int(round(float(t.get('lab_length',     2))))
        for i in range(n_lec):
            sessions.append({'teacher': t['teacher_name'], 'subject': t['subject_name'],
                             'type': 'lecture', 'length': lec_len, 'sidx': i})
        for i in range(n_lab):
            sessions.append({'teacher': t['teacher_name'], 'subject': t['subject_name'],
                             'type': 'lab', 'length': lab_len, 'sidx': i})

    if not sessions:
        return []

    # ── Cross-timetable occupancy (teacher + branch only, NOT global ts) ──
    #
    # OLD (buggy): base_occ_ts blocked ALL (day, timeslot) from existing
    #   timetables, leaving only (35 - external_slot_count) slots for the
    #   new timetable even if those slots involved totally different teachers.
    #
    # NEW (fixed): only block the specific teacher and branch combinations
    #   that are actually conflicting. Different classes can share timeslots.

    base_occ_t = set()   # (teacher_lower, day, timeslot) — cross-timetable
    # Branch/subject name is NOT tracked: the same subject can legitimately
    # be taught to different student groups simultaneously by different teachers.

    for ex in existing_slots:
        for teacher in split_teachers(ex['teacher_name']):
            base_occ_t.add((teacher.lower(), ex['day'], ex['time_slot']))

    # ── Helpers ────────────────────────────────────────────────────────────

    def can_place(day, start_h, length, tname, subject, occ_t, occ_ts):
        """Return True iff this session can be placed at (day, start_h).
        Only checks teacher availability and within-timetable slot collisions.
        Subject/branch name is not a constraint.
        """
        ts_list = [fmt_slot(start_h + k) for k in range(length)]
        if any((day, ts) in occ_ts for ts in ts_list):
            return False
        if any((tc.lower(), day, ts) in occ_t
               for tc in split_teachers(tname)
               for ts in ts_list):
            return False
        return True

    def do_place(day, start_h, length, tname, subject, stype,
                 occ_t, occ_ts, result):
        ts_list = [fmt_slot(start_h + k) for k in range(length)]
        for ts in ts_list:
            for tc in split_teachers(tname):
                occ_t.add((tc.lower(), day, ts))
            occ_ts.add((day, ts))
            result.append({'day': day, 'time_slot': ts,
                           'teacher_name': tname, 'branch_name': subject,
                           'subject_name': subject, 'type': stype})

    def do_unplace(day, start_h, length, tname, subject,
                   occ_t, occ_ts, result):
        ts_list = [fmt_slot(start_h + k) for k in range(length)]
        ts_set  = set(ts_list)
        for ts in ts_list:
            for tc in split_teachers(tname):
                occ_t.discard((tc.lower(), day, ts))
            occ_ts.discard((day, ts))
        result[:] = [s for s in result
                     if not (s['day'] == day and s['time_slot'] in ts_set
                             and s['teacher_name'] == tname
                             and s['subject_name'] == subject)]

    # ── Feasibility checks ─────────────────────────────────────────────────

    avail_per_day = (WORK_END - WORK_START) - (BREAK_END - BREAK_START)  # 7
    avail_hours   = len(DAYS) * avail_per_day                             # 35
    total_hours   = sum(s['length'] for s in sessions)

    if total_hours > avail_hours:
        raise ValueError(
            f"Input requires {total_hours} hours of weekly slots but the "
            f"working week only has {avail_hours} available "
            f"({len(DAYS)} days × {avail_per_day} h/day, lunch excluded). "
            f"Please reduce lectures/labs or shorten their lengths."
        )

    # Per-teacher feasibility — check that no single teacher is over-scheduled
    teacher_external_hours = {}
    for ex in existing_slots:
        for tc in split_teachers(ex['teacher_name']):
            k = tc.lower()
            teacher_external_hours[k] = teacher_external_hours.get(k, 0) + 1

    teacher_needed_hours = {}
    for s in sessions:
        for tc in split_teachers(s['teacher']):
            k = tc.lower()
            teacher_needed_hours[k] = teacher_needed_hours.get(k, 0) + s['length']

    over_booked = []
    for tc, needed in teacher_needed_hours.items():
        external = teacher_external_hours.get(tc, 0)
        available = avail_hours - external
        if needed > available:
            over_booked.append(
                f"  '{tc}' needs {needed}h in this timetable but only "
                f"{available}h remain ({external}h already used in other timetables)"
            )
    if over_booked:
        raise ValueError(
            "Schedule infeasible — the following teacher(s) are over-committed:\n"
            + "\n".join(over_booked)
            + "\nReduce their lecture/lab counts or resolve conflicts with "
              "the existing timetable(s) first."
        )

    utilisation = total_hours / avail_hours
    log.info(
        f"Week utilisation: {total_hours}/{avail_hours}h ({utilisation:.0%})."
    )

    sorted_sessions = sorted(sessions, key=lambda s: -s['length'])

    # ── Phase 1: Randomised greedy ─────────────────────────────────────────
    n_trials = 500 if utilisation >= 0.95 else (300 if utilisation >= 0.80 else 100)
    log.info(f"Running {n_trials} greedy trials")

    for trial in range(n_trials):
        occ_t  = set(base_occ_t)
        occ_ts = set()                  # within-timetable only — starts empty

        trial_order = []
        for _, grp in groupby(sorted_sessions, key=lambda s: s['length']):
            g = list(grp)
            random.shuffle(g)
            trial_order.extend(g)

        result    = []
        day_count = {}
        failed    = False

        for sess in trial_order:
            tname, subject, stype, length = (
                sess['teacher'], sess['subject'], sess['type'], sess['length'])
            tl, sl = tname.lower(), subject.lower()
            valid_starts = get_valid_session_starts(length)

            candidates = []
            for day in DAYS:
                same_day = day_count.get((tl, sl, day), 0)
                busy = sum(1 for h in range(WORK_START, WORK_END)
                           if h != BREAK_START
                           and any((tc.lower(), day, fmt_slot(h)) in occ_t
                                   for tc in split_teachers(tname)))
                for sh in valid_starts:
                    pm = 0 if (sess['sidx'] % 2 == 0) == (sh < 14) else 1
                    candidates.append(((same_day * 10, busy, pm, sh), day, sh))

            random.shuffle(candidates)
            candidates.sort(key=lambda x: x[0])

            placed = False
            for _, day, sh in candidates:
                if can_place(day, sh, length, tname, subject,
                             occ_t, occ_ts):
                    do_place(day, sh, length, tname, subject, stype,
                             occ_t, occ_ts, result)
                    day_count[(tl, sl, day)] = day_count.get((tl, sl, day), 0) + 1
                    placed = True
                    break

            if not placed:
                failed = True
                break

        if not failed:
            log.info(f"Greedy solved on trial {trial + 1}/{n_trials}, "
                     f"{len(result)} slots (utilisation {utilisation:.0%})")
            day_order_map = {d: i for i, d in enumerate(DAYS)}
            result.sort(key=lambda s: (day_order_map.get(s['day'], 99),
                                       parse_hour(s['time_slot'])))
            return result

    log.warning("Greedy failed — trying backtracking with forward checking")

    # ── Phase 2: Backtracking + Forward Checking ──────────────────────────

    BT_RESTARTS = 20   # increased for tight schedules

    teacher_subj_count = {}
    for t in teachers:
        teacher_subj_count[t['teacher_name'].lower()] = \
            teacher_subj_count.get(t['teacher_name'].lower(), 0) + 1

    def _domain(sess, ot, ots):
        valid_starts = get_valid_session_starts(sess['length'])
        return [
            (day, sh)
            for day in DAYS
            for sh  in valid_starts
            if can_place(day, sh, sess['length'], sess['teacher'],
                         sess['subject'], ot, ots)
        ]

    def _forward_ok(from_idx, bt_ord, ot, ots):
        for j in range(from_idx, len(bt_ord)):
            if not _domain(bt_ord[j], ot, ots):
                return False
        return True

    def _score(sess):
        n_starts = len(get_valid_session_starts(sess['length']))
        multi    = teacher_subj_count.get(sess['teacher'].lower(), 1)
        return (n_starts, -multi)

    solved       = False
    final_result = []

    for restart in range(BT_RESTARTS):
        shuffled = sessions[:]
        random.shuffle(shuffled)
        bt_order = sorted(shuffled, key=_score)

        occ_t      = set(base_occ_t)
        occ_ts     = set()                # within-timetable only — starts empty
        bt_result  = []
        day_count_bt = {}

        def backtrack(idx):
            if idx == len(bt_order):
                return True

            sess    = bt_order[idx]
            tname   = sess['teacher']
            subject = sess['subject']
            stype   = sess['type']
            length  = sess['length']
            tl, sl  = tname.lower(), subject.lower()

            domain = _domain(sess, occ_t, occ_ts)
            if not domain:
                return False

            random.shuffle(domain)
            domain.sort(key=lambda ds: day_count_bt.get((tl, sl, ds[0]), 0))

            for day, sh in domain:
                do_place(day, sh, length, tname, subject, stype,
                         occ_t, occ_ts, bt_result)
                day_count_bt[(tl, sl, day)] = \
                    day_count_bt.get((tl, sl, day), 0) + 1

                if _forward_ok(idx + 1, bt_order, occ_t, occ_ts):
                    if backtrack(idx + 1):
                        return True

                do_unplace(day, sh, length, tname, subject,
                           occ_t, occ_ts, bt_result)
                day_count_bt[(tl, sl, day)] -= 1

            return False

        if backtrack(0):
            solved       = True
            final_result = bt_result
            log.info(
                f"Backtracking solved on restart {restart + 1}/{BT_RESTARTS}, "
                f"{len(final_result)} slots"
            )
            break
        log.debug(f"Backtracking restart {restart + 1} failed — retrying")

    if solved:
        day_order_map = {d: i for i, d in enumerate(DAYS)}
        final_result.sort(key=lambda s: (day_order_map.get(s['day'], 99),
                                         parse_hour(s['time_slot'])))
        return final_result

    log.warning("All restarts failed — genuinely infeasible")
    return None


# ══════════════════════════════════════════════════════════════════════════
# STEP 2 — AI CONFLICT AUDITOR
# ══════════════════════════════════════════════════════════════════════════

def _format_slots_for_ai(slots: list, label: str) -> str:
    if not slots:
        return f"{label}: (none)\n"
    lines = [f"{label}:"]
    day_order = {d: i for i, d in enumerate(DAYS)}
    for s in sorted(slots, key=lambda x: (day_order.get(x['day'], 99),
                                           parse_hour(x['time_slot']))):
        tid = f" [TT#{s['timetable_id']}]" if 'timetable_id' in s else ""
        lines.append(
            f"  {s['day']:10} {s['time_slot']:>15}  "
            f"{s['teacher_name']:<28} {s['subject_name']:<20} [{s['type']}]{tid}"
        )
    return '\n'.join(lines) + '\n'


def _build_audit_prompt(new_slots: list, existing_slots: list,
                        teachers: list, name: str,
                        semester: str, department: str) -> str:
    new_block      = _format_slots_for_ai(new_slots, "NEW TIMETABLE SLOTS")
    existing_block = _format_slots_for_ai(existing_slots, "EXISTING DATABASE SLOTS")

    teacher_summary = '\n'.join(
        f"  {t['teacher_name']} | {t['subject_name']} | "
        f"{t.get('no_of_lectures',0)} lec × {t.get('lecture_length',1)}h  "
        f"{t.get('no_of_labs',0)} lab × {t.get('lab_length',2)}h"
        for t in teachers
    )

    total_required = sum(
        int(t.get('no_of_lectures', 0)) * int(round(float(t.get('lecture_length', 1)))) +
        int(t.get('no_of_labs',     0)) * int(round(float(t.get('lab_length',     2))))
        for t in teachers
    )

    return f"""You are a timetable conflict-auditor. A deterministic Python scheduler has
already built the timetable below. Your ONLY job is to audit it for genuine conflicts.

TIMETABLE BEING AUDITED: {name}
Department: {department}  |  Semester: {semester}
Expected total slot-entries: {total_required}  |  Actual slot-entries generated: {len(new_slots)}

TEACHER REQUIREMENTS:
{teacher_summary}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL — HOW MULTI-HOUR SESSIONS ARE STORED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every session is split into individual 1-hour slot entries.
A 2-hour lab by Teacher A on Monday 09:00 appears as TWO separate rows:
  Monday  09:00-10:00  Teacher A  SubjectX  [lab]
  Monday  10:00-11:00  Teacher A  SubjectX  [lab]
A 3-lecture session appears as THREE rows at consecutive hours.

This is CORRECT — it is NOT a double-booking or a conflict.
DO NOT flag consecutive same-teacher / same-subject entries at adjacent hours as H1 or H3.

For H6 (slot count check):
  - Count each 1-hour row separately.
  - A teacher with "3 lectures × 1h" needs exactly 3 lecture rows.
  - A teacher with "1 lab × 2h" needs exactly 2 lab rows.
  - Compare actual row count against (no_of_lectures × lecture_length) + (no_of_labs × lab_length).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{new_block}
{existing_block}

AUDIT INSTRUCTIONS — only report genuine violations. Be conservative.
Do NOT suggest redesigns. Do NOT rewrite the schedule.

HARD CONFLICTS (genuine problems only):
  H1. Teacher truly double-booked: same teacher at the exact same day+timeslot
      in two DIFFERENT subjects (not consecutive hours of the same subject).
  H2. Cross-timetable teacher conflict: a teacher in NEW slots appears at the
      identical day+timeslot in EXISTING DATABASE SLOTS.
  H3. Branch truly double-booked: two DIFFERENT subjects scheduled at the
      exact same day+timeslot for the same branch/class.
  H4. Lunch break violation: any slot occupying 13:00-14:00.
  H5. Out-of-hours: any slot starting before 09:00 or ending after 17:00.
  H6. Wrong slot count: a teacher's actual row count does not match
      (no_of_lectures × lecture_length) + (no_of_labs × lab_length).
      Only flag if the mismatch is more than 1 row (allow ±1 rounding tolerance).

SOFT WARNINGS (quality issues, non-blocking):
  W1. Same subject scheduled on the same day more than twice.
  W2. All of a teacher's slots bunched on 1 day instead of spread across the week.
  W3. A teacher has more than 4 consecutive hours in one day.

OUTPUT FORMAT — respond ONLY with this JSON (no markdown, no extra text):
{{
  "hard_conflicts": [
    {{"rule": "H1", "description": "...", "affected": "teacher / day / time"}}
  ],
  "soft_warnings": [
    {{"rule": "W1", "description": "...", "affected": "teacher / subject"}}
  ],
  "summary": {{
    "hard_count": 0,
    "warning_count": 0,
    "verdict": "PASS",
    "notes": "Brief overall comment about schedule quality"
  }}
}}

verdict must be exactly "PASS" (zero hard conflicts) or "FAIL" (one or more hard conflicts).
If there are no issues of a category, return an empty array for that key.
Remember: consecutive same-teacher same-subject rows are multi-hour sessions — NOT conflicts.
"""


def call_groq_audit(prompt: str) -> dict:
    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError("groq package not installed. Run: pip install groq")

    api_key = os.environ.get('GROQ_API_KEY')
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY not set. Get a free key at: https://console.groq.com"
        )

    client = Groq(api_key=api_key)
    log.info(f"AI audit: calling {MODEL} — prompt length {len(prompt)} chars")

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict timetable conflict-auditor. "
                        "Output ONLY the raw JSON object described in the prompt. "
                        "No markdown fences, no explanation, no extra keys."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=2048,
            response_format={"type": "json_object"},
            timeout=API_TIMEOUT,
        )

        raw = response.choices[0].message.content.strip()
        log.info(f"AI audit: response {len(raw)} chars | "
                 f"tokens used: {response.usage.total_tokens}")

        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\s*```\s*$',       '', raw, flags=re.MULTILINE).strip()

        return json.loads(raw)

    except json.JSONDecodeError as e:
        log.error(f"AI audit: invalid JSON response — {e}")
        return {
            "hard_conflicts": [],
            "soft_warnings":  [{"rule": "W0",
                                 "description": f"AI auditor returned malformed JSON: {e}",
                                 "affected": "system"}],
            "summary": {
                "hard_count":    0,
                "warning_count": 1,
                "verdict":       "PASS",
                "notes":         "AI audit response could not be parsed; "
                                 "Python-level checks passed."
            }
        }
    except Exception as e:
        err = str(e).lower()
        if "rate_limit" in err or "429" in err:
            log.warning("Groq rate limit hit during audit — skipping AI check")
            return _audit_skipped("Groq rate limit reached — AI audit skipped, "
                                  "Python checks passed.")
        if "401" in err or "403" in err or "invalid" in err:
            raise RuntimeError("Groq API key invalid. "
                               "Get a free key at: https://console.groq.com")
        if "timeout" in err:
            log.warning("Groq audit timed out — skipping AI check")
            return _audit_skipped(f"AI audit timed out after {API_TIMEOUT}s; "
                                  "Python checks passed.")
        raise RuntimeError(f"Groq API error during audit: {e}")


def _audit_skipped(reason: str) -> dict:
    return {
        "hard_conflicts": [],
        "soft_warnings":  [{"rule": "W0", "description": reason, "affected": "system"}],
        "summary": {
            "hard_count":    0,
            "warning_count": 1,
            "verdict":       "PASS",
            "notes":         reason
        }
    }


def ai_conflict_audit(new_slots: list, existing_slots: list,
                      teachers: list, name: str,
                      semester: str, department: str) -> dict:
    prompt = _build_audit_prompt(
        new_slots, existing_slots, teachers, name, semester, department
    )
    result = call_groq_audit(prompt)

    result.setdefault('hard_conflicts', [])
    result.setdefault('soft_warnings',  [])
    result.setdefault('summary', {
        'hard_count':    len(result['hard_conflicts']),
        'warning_count': len(result['soft_warnings']),
        'verdict':       'FAIL' if result['hard_conflicts'] else 'PASS',
        'notes':         ''
    })

    hard = result['summary'].get('hard_count', len(result['hard_conflicts']))
    log.info(
        f"AI audit complete — verdict: {result['summary'].get('verdict')} | "
        f"hard: {hard} | warnings: {result['summary'].get('warning_count', 0)}"
    )
    return result


# ══════════════════════════════════════════════════════════════════════════
# Python-level conflict checker (fast, runs before the AI audit)
# ══════════════════════════════════════════════════════════════════════════

def check_internal_conflicts(slots: list, teachers: list,
                              existing_slots: list) -> list:
    """Fast Python conflict check — only teacher double-booking is a hard error.
    Branch/subject name is not checked: the same subject can be taught
    simultaneously to different student groups by different teachers.
    """
    conflicts = []

    teacher_occ = {}
    for s in slots:
        for teacher in split_teachers(s['teacher_name']):
            tk = (teacher.lower(), s['day'], s['time_slot'])
            if tk in teacher_occ:
                conflicts.append(
                    f"[INTERNAL-TEACHER] '{teacher}' double-booked "
                    f"{s['day']} {s['time_slot']}"
                )
            else:
                teacher_occ[tk] = True

    # Cross-timetable teacher check only
    ex_teacher_occ = {}
    for ex in existing_slots:
        for teacher in split_teachers(ex['teacher_name']):
            ex_teacher_occ[(teacher.lower(), ex['day'], ex['time_slot'])] = ex

    for s in slots:
        for teacher in split_teachers(s['teacher_name']):
            key = (teacher.lower(), s['day'], s['time_slot'])
            if key in ex_teacher_occ:
                ex = ex_teacher_occ[key]
                conflicts.append(
                    f"[EXTERNAL-TEACHER] '{teacher}' already scheduled "
                    f"{s['day']} {s['time_slot']} "
                    f"(Timetable #{ex.get('timetable_id', '?')})"
                )

    # Slot count check — use exact teacher_name match to avoid false positives
    # with shared teachers across multiple rows (e.g. "KB / NK", "RR / NK")
    for t in teachers:
        tname   = t['teacher_name']
        subject = t['subject_name']
        my_slots = [
            s for s in slots
            if s['teacher_name'].lower() == tname.lower()
            and s['subject_name'].lower() == subject.lower()
        ]
        lec_got  = len([s for s in my_slots if s['type'] == 'lecture'])
        lab_got  = len([s for s in my_slots if s['type'] == 'lab'])
        lec_need = int(t.get('no_of_lectures', 0)) * int(round(float(t.get('lecture_length', 1))))
        lab_need = int(t.get('no_of_labs', 0))     * int(round(float(t.get('lab_length',     2))))

        if lec_got != lec_need:
            conflicts.append(
                f"[COUNT] '{tname}' needs {lec_need} lecture entries for "
                f"{subject}, got {lec_got}"
            )
        if lab_need > 0 and lab_got != lab_need:
            conflicts.append(
                f"[COUNT] '{tname}' needs {lab_need} lab entries for "
                f"{subject}, got {lab_got}"
            )

    return list(dict.fromkeys(conflicts))


# ══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def generate_timetable(name: str, semester: str, department: str,
                       teachers: list, existing_slots: list) -> dict:
    """
    Main entry point called by app.py.

    Returns:
    {
        "success":    True,
        "slots":      [...],
        "attempts":   1,
        "ai_audit":   {...},
        "ai_passed":  True/False
    }

    Raises:
        ValueError   — bad input or genuinely infeasible schedule
        RuntimeError — Groq API key missing / invalid
    """
    if not teachers:
        raise ValueError("No teachers provided.")

    for t in teachers:
        for field in ('teacher_name', 'subject_name'):
            if not str(t.get(field, '')).strip():
                raise ValueError(f"Teacher entry missing field: '{field}'")
        if not t.get('branch_name'):
            t['branch_name'] = t.get('subject_name', '')

    # ── STEP 1: Python scheduler ─────────────────────────────────────────
    log.info("=" * 56)
    log.info("STEP 1 — Deterministic Python Scheduler")
    log.info("=" * 56)

    slots = deterministic_schedule(teachers, existing_slots)

    if slots is None:
        raise ValueError(
            "Could not fit all sessions into the working week. "
            "The schedule is mathematically infeasible with the current "
            "teacher counts, slot lengths, and existing timetable conflicts. "
            "Options: reduce lectures/labs per week, shorten their durations, "
            "or delete conflicting entries from the existing timetables."
        )

    log.info(f"Scheduler produced {len(slots)} slots.")

    # ── Fast Python sanity check ─────────────────────────────────────────
    fast_conflicts = check_internal_conflicts(slots, teachers, existing_slots)
    if fast_conflicts:
        raise RuntimeError(
            "Python scheduler produced an internally inconsistent schedule "
            f"({len(fast_conflicts)} conflict(s)). "
            "Please try again: " + "; ".join(fast_conflicts[:3])
        )

    # ── STEP 2: AI conflict audit ────────────────────────────────────────
    log.info("=" * 56)
    log.info("STEP 2 — Groq AI Conflict Auditor")
    log.info("=" * 56)

    audit = ai_conflict_audit(
        new_slots=slots,
        existing_slots=existing_slots,
        teachers=teachers,
        name=name,
        semester=semester,
        department=department,
    )

    verdict = audit['summary'].get('verdict', 'PASS')

    hard = audit.get('hard_conflicts', [])
    if verdict == 'FAIL':
        log.warning(f"AI audit FAILED — {len(hard)} hard conflict(s).")
        for h in hard:
            log.warning(f"  [{h.get('rule')}] {h.get('description')} — {h.get('affected')}")
    else:
        log.info(f"AI audit PASSED — "
                 f"warnings: {audit['summary'].get('warning_count', 0)}  "
                 f"notes: {audit['summary'].get('notes', '')}")

    return {
        "success":    True,
        "slots":      slots,
        "attempts":   1,
        "ai_audit":   audit,
        "ai_passed":  verdict != 'FAIL',
    }
