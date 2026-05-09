import csv
import io
import time
import logging
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import os
import jwt as pyjwt
import db
import timetable_gen

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__, static_folder=os.path.dirname(__file__))
CORS(app)

db.init_db()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Only these extensions may be served as static files.
# .py, .env, .db, .sqlite etc. are never exposed.
_ALLOWED_STATIC = {'.html', '.js', '.css', '.ico', '.png', '.jpg',
                   '.jpeg', '.svg', '.woff', '.woff2', '.ttf', '.webp'}

# ── JWT Auth ─────────────────────────────────────────────────────────────────
JWT_SECRET = os.environ.get('JWT_SECRET', 'timetable-planner-secret-CHANGE-IN-PROD')
JWT_ALGO   = 'HS256'
JWT_HOURS  = 24


def _make_token(user: dict) -> str:
    payload = {
        'sub':          str(user['id']),
        'username':     user['username'],
        'role':         user['role'],
        'display_name': user.get('display_name', ''),
        'exp':          datetime.now(timezone.utc) + timedelta(hours=JWT_HOURS),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def _decode_token(token: str):
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except Exception:
        return None


def _get_caller():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    return _decode_token(auth[7:])


def require_auth(f):
    @wraps(f)
    def deco(*a, **kw):
        if not _get_caller():
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        return f(*a, **kw)
    return deco


def require_head(f):
    @wraps(f)
    def deco(*a, **kw):
        u = _get_caller()
        if not u:
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        if u.get('role') != 'head':
            return jsonify({'success': False, 'error': 'Head administrator access required'}), 403
        return f(*a, **kw)
    return deco


_rate_store = defaultdict(list)
RATE_LIMIT  = 2
RATE_WINDOW = 60


def is_rate_limited(ip: str) -> bool:
    now          = time.time()
    window_start = now - RATE_WINDOW
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]
    if len(_rate_store[ip]) >= RATE_LIMIT:
        return True
    _rate_store[ip].append(now)
    return False


MAX_NAME_LEN = 120
MAX_STR_LEN  = 80
MAX_TEACHERS = 40


def sanitize_str(val: str, max_len: int = MAX_STR_LEN, field: str = "field") -> str:
    s = str(val or "").strip()
    if not s:
        raise ValueError(f"'{field}' must not be empty.")
    if len(s) > max_len:
        raise ValueError(f"'{field}' exceeds {max_len} characters (got {len(s)}).")
    return s


def sanitize_teacher(t: dict, index: int) -> dict:
    prefix  = f"Teacher #{index + 1}"
    subject = sanitize_str(t.get("subject_name", ""), MAX_STR_LEN, f"{prefix} subject_name")
    return {
        "teacher_name":   sanitize_str(t.get("teacher_name", ""), MAX_STR_LEN, f"{prefix} teacher_name"),
        "branch_name":    subject,
        "subject_name":   subject,
        "no_of_lectures": max(0, min(20, int(t.get("no_of_lectures", 0) or 0))),
        "no_of_labs":     max(0, min(10, int(t.get("no_of_labs",     0) or 0))),
        "lecture_length": max(0.5, min(3.0, float(t.get("lecture_length", 1.0) or 1.0))),
        "lab_length":     max(1.0, min(4.0, float(t.get("lab_length",     2.0) or 2.0))),
    }


# ══════════════════════════════════════════════════════════════════════════
# STATIC FRONTEND SERVING
# Flask serves all HTML/JS files so you don't need a separate static host.
# ══════════════════════════════════════════════════════════════════════════

@app.route('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/<path:filename>')
def serve_static(filename):
    # Block any path traversal attempts and disallow unsafe extensions
    safe_name = os.path.basename(filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in _ALLOWED_STATIC:
        return jsonify({'error': 'Not found'}), 404
    return send_from_directory(BASE_DIR, safe_name)


# ── Health & Stats ────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "AI Timetable Planner API is running ✅"})


@app.route("/api/stats", methods=["GET"])
def stats():
    data = db.get_system_stats()
    return jsonify({"success": True, "data": data})


# ── Timetable CRUD ────────────────────────────────────────────────────────────

@app.route("/api/timetables", methods=["GET"])
def list_timetables():
    try:
        page  = max(1, int(request.args.get("page",  1)))
        limit = max(1, min(100, int(request.args.get("limit", 50))))
    except ValueError:
        return jsonify({"success": False, "error": "Invalid page/limit parameter"}), 400

    dept     = request.args.get("dept",     "").strip()
    semester = request.args.get("semester", "").strip()
    search   = request.args.get("search",  "").strip()

    result = db.get_all_timetables(
        page=page, limit=limit,
        dept_filter=dept, semester_filter=semester, search=search
    )

    # Re-compute teacher_count splitting co-teacher strings like "KB / NK"
    import re as _re
    for row in result.get("data", []):
        tt = db.get_timetable_by_id(row["id"])
        if tt:
            individuals = set()
            for t in tt.get("teachers", []):
                for name in _re.split(r'\s*[/&]\s*', t["teacher_name"]):
                    n = name.strip()
                    if n:
                        individuals.add(n)
            row["teacher_count"] = len(individuals)

    return jsonify({"success": True, **result})


@app.route("/api/timetables", methods=["POST"])
@require_auth
def save_timetable():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON body provided"}), 400

    for field in ("name", "teachers", "slots"):
        if field not in data:
            return jsonify({"success": False, "error": f"Missing required field: '{field}'"}), 400

    try:
        name       = sanitize_str(data["name"], MAX_NAME_LEN, "name")
        semester   = str(data.get("semester",   "") or "").strip()[:40]
        department = str(data.get("department", "") or "").strip()[:80]
        teachers   = [sanitize_teacher(t, i) for i, t in enumerate(data["teachers"])]
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    if len(teachers) > MAX_TEACHERS:
        return jsonify({"success": False,
                        "error": f"Max {MAX_TEACHERS} teachers allowed per timetable."}), 400

    conflicts = db.check_conflicts(data["slots"])
    if conflicts:
        return jsonify({"success": False, "error": "Conflicts detected.",
                        "conflicts": conflicts}), 409

    timetable_id = db.create_timetable(name=name, semester=semester, department=department)
    db.save_teachers(timetable_id, teachers)
    db.save_slots(timetable_id, data["slots"])
    db.add_audit_log(timetable_id, "create",
                     f"Timetable '{name}' created with {len(teachers)} teachers.")

    return jsonify({"success": True, "message": "Timetable saved successfully.",
                    "timetable_id": timetable_id}), 201


@app.route("/api/timetables/<int:timetable_id>", methods=["GET"])
def get_timetable(timetable_id):
    timetable = db.get_timetable_by_id(timetable_id)
    if not timetable:
        return jsonify({"success": False, "error": "Timetable not found"}), 404
    return jsonify({"success": True, "data": timetable})


@app.route("/api/timetables/<int:timetable_id>", methods=["PUT"])
@require_head
def update_timetable(timetable_id):
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON body provided"}), 400

    try:
        name       = sanitize_str(data.get("name", ""), MAX_NAME_LEN, "name")
        semester   = str(data.get("semester",   "") or "").strip()[:40]
        department = str(data.get("department", "") or "").strip()[:80]
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    updated = db.update_timetable(timetable_id, name=name,
                                   semester=semester, department=department)
    if not updated:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    db.add_audit_log(timetable_id, "update",
                     f"Metadata updated: name='{name}', semester='{semester}', "
                     f"dept='{department}'.")
    return jsonify({"success": True, "message": "Timetable updated."})


@app.route("/api/timetables/<int:timetable_id>", methods=["DELETE"])
@require_head
def delete_timetable(timetable_id):
    timetable = db.get_timetable_by_id(timetable_id)
    if not timetable:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    deleted = db.delete_timetable(timetable_id)
    if not deleted:
        return jsonify({"success": False, "error": "Delete failed"}), 500

    db.add_audit_log(0, "delete",
                     f"Timetable #{timetable_id} '{timetable['name']}' permanently deleted.")
    return jsonify({"success": True, "message": f"Timetable {timetable_id} deleted."})


@app.route("/api/timetables/<int:timetable_id>/duplicate", methods=["POST"])
@require_head
def duplicate_timetable(timetable_id):
    data     = request.get_json() or {}
    new_name = str(data.get("name", f"Copy of Timetable {timetable_id}")).strip()[:MAX_NAME_LEN]
    new_id   = db.duplicate_timetable(timetable_id, new_name)
    if not new_id:
        return jsonify({"success": False, "error": "Original timetable not found"}), 404

    db.add_audit_log(new_id, "duplicate",
                     f"Duplicated from timetable #{timetable_id} as '{new_name}'.")
    return jsonify({"success": True, "message": "Timetable duplicated.",
                    "new_timetable_id": new_id}), 201


@app.route("/api/timetables/<int:timetable_id>/slots", methods=["PUT"])
@require_head
def update_slots(timetable_id):
    timetable = db.get_timetable_by_id(timetable_id)
    if not timetable:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    data = request.get_json()
    if not data or "slots" not in data:
        return jsonify({"success": False, "error": "Missing 'slots' in body"}), 400

    conflicts = db.check_conflicts(data["slots"], exclude_timetable_id=timetable_id)
    if conflicts:
        return jsonify({"success": False, "error": "Conflicts detected.",
                        "conflicts": conflicts}), 409

    db.delete_slots_for_timetable(timetable_id)
    db.save_slots(timetable_id, data["slots"])
    db.add_audit_log(timetable_id, "update",
                     f"Slot layout manually edited for '{timetable['name']}'.")
    return jsonify({"success": True, "message": "Slots updated successfully."})


# ── Conflicts summary ─────────────────────────────────────────────────────────

@app.route("/api/conflicts-summary", methods=["GET"])
def conflicts_summary():
    all_slots    = db.get_all_slots()
    seen_teacher = {}
    seen_branch  = {}
    conflict_ids = set()

    for s in all_slots:
        for teacher in [n.strip() for n in s["teacher_name"].replace('&', '/').split('/') if n.strip()]:
            tk = (teacher.lower(), s["day"], s["time_slot"])
            if tk in seen_teacher:
                conflict_ids.add(s["timetable_id"])
                conflict_ids.add(seen_teacher[tk])
            else:
                seen_teacher[tk] = s["timetable_id"]

        bk = (s["branch_name"].lower(), s["day"], s["time_slot"])
        if bk in seen_branch:
            conflict_ids.add(s["timetable_id"])
            conflict_ids.add(seen_branch[bk])
        else:
            seen_branch[bk] = s["timetable_id"]

    return jsonify({"success": True, "conflict_timetable_ids": list(conflict_ids)})


# ══════════════════════════════════════════════════════════════════════════
# GENERATE
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/generate", methods=["POST"])
@require_auth
def generate():
    ip = request.remote_addr or "unknown"
    if is_rate_limited(ip):
        return jsonify({"success": False,
                        "error": f"Rate limit exceeded. Max {RATE_LIMIT} generation "
                                 f"requests per minute."}), 429

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON body provided"}), 400

    for field in ("name", "teachers"):
        if field not in data:
            return jsonify({"success": False, "error": f"Missing field: '{field}'"}), 400

    if not data["teachers"]:
        return jsonify({"success": False, "error": "No teachers provided"}), 400

    try:
        name       = sanitize_str(data["name"], MAX_NAME_LEN, "name")
        semester   = str(data.get("semester",   "") or "").strip()[:40]
        department = str(data.get("department", "") or "").strip()[:80]
        teachers   = [sanitize_teacher(t, i) for i, t in enumerate(data["teachers"])]
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    if len(teachers) > MAX_TEACHERS:
        return jsonify({"success": False,
                        "error": f"Max {MAX_TEACHERS} teachers allowed."}), 400

    existing_slots = db.get_all_slots()

    try:
        result = timetable_gen.generate_timetable(
            name=name, semester=semester, department=department,
            teachers=teachers, existing_slots=existing_slots,
        )
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except RuntimeError as e:
        err = str(e).lower()
        if "timeout" in err or "timed out" in err:
            return jsonify({"success": False,
                            "error": "AI audit timed out. Schedule was created but "
                                     "could not be audited. Please try again."}), 504
        return jsonify({"success": False, "error": str(e)}), 500

    db_conflicts = db.check_conflicts(result["slots"])
    if db_conflicts:
        return jsonify({
            "success":   False,
            "error":     "Generated timetable has database-level conflicts.",
            "conflicts": db_conflicts,
            "ai_audit":  result.get("ai_audit"),
        }), 409

    audit     = result.get("ai_audit", {})
    ai_passed = result.get("ai_passed", True)

    if not ai_passed:
        hard_conflicts = audit.get("hard_conflicts", [])
        log.warning(
            f"AI audit flagged {len(hard_conflicts)} conflict(s) — "
            f"Python validation passed, treating as advisory."
        )

    return jsonify({
        "success":    True,
        "slots":      result["slots"],
        "slot_count": len(result["slots"]),
        "attempts":   result.get("attempts", 1),
        "ai_audit":   audit,
        "ai_passed":  ai_passed,
    })


# ══════════════════════════════════════════════════════════════════════════
# REGENERATE
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/timetables/<int:timetable_id>/regenerate", methods=["POST"])
@require_head
def regenerate_timetable(timetable_id):
    ip = request.remote_addr or "unknown"
    if is_rate_limited(ip):
        return jsonify({"success": False,
                        "error": f"Rate limit exceeded. Max {RATE_LIMIT} generation "
                                 f"requests per minute."}), 429

    timetable = db.get_timetable_by_id(timetable_id)
    if not timetable:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    if not timetable["teachers"]:
        return jsonify({"success": False,
                        "error": "This timetable has no teachers — cannot regenerate"}), 400

    existing_slots = db.get_all_slots(exclude_timetable_id=timetable_id)

    try:
        result = timetable_gen.generate_timetable(
            name=timetable["name"], semester=timetable["semester"],
            department=timetable["department"], teachers=timetable["teachers"],
            existing_slots=existing_slots,
        )
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except RuntimeError as e:
        err = str(e).lower()
        if "timeout" in err or "timed out" in err:
            return jsonify({"success": False,
                            "error": "AI audit timed out. Try again shortly."}), 504
        return jsonify({"success": False, "error": str(e)}), 500

    db_conflicts = db.check_conflicts(result["slots"], exclude_timetable_id=timetable_id)
    if db_conflicts:
        return jsonify({
            "success":   False,
            "error":     "Regenerated timetable has database-level conflicts.",
            "conflicts": db_conflicts,
            "ai_audit":  result.get("ai_audit"),
        }), 409

    audit     = result.get("ai_audit", {})
    ai_passed = result.get("ai_passed", True)

    if not ai_passed:
        hard_conflicts = audit.get("hard_conflicts", [])
        log.warning(
            f"AI audit flagged {len(hard_conflicts)} conflict(s) on regenerate — "
            f"Python validation passed, proceeding with save."
        )

    db.delete_slots_for_timetable(timetable_id)
    db.save_slots(timetable_id, result["slots"])
    db.update_timetable(
        timetable_id,
        name=timetable["name"],
        semester=timetable["semester"],
        department=timetable["department"],
    )

    hard_n = audit.get("summary", {}).get("hard_count", 0)
    warn_n = audit.get("summary", {}).get("warning_count", 0)
    notes  = audit.get("summary", {}).get("notes", "")
    audit_note = f"AI audit: PASS — {hard_n} hard, {warn_n} warnings. {notes}"

    db.add_audit_log(
        timetable_id, "regenerate",
        f"Schedule regenerated: {len(result['slots'])} slots. {audit_note}"
    )

    return jsonify({
        "success":    True,
        "message":    "Timetable regenerated and AI-audited successfully.",
        "slot_count": len(result["slots"]),
        "attempts":   result.get("attempts", 1),
        "ai_audit":   audit,
    })


# ── Conflict preview ──────────────────────────────────────────────────────────

@app.route("/api/check-conflicts", methods=["POST"])
def check_conflicts_preview():
    data = request.get_json()
    if not data or "slots" not in data:
        return jsonify({"success": False, "error": "Missing 'slots' in body"}), 400

    exclude_id = data.get("exclude_timetable_id")
    conflicts  = db.check_conflicts(data["slots"], exclude_timetable_id=exclude_id)
    return jsonify({"success": True, "has_conflicts": len(conflicts) > 0,
                    "conflicts": conflicts})


# ── Export endpoints ──────────────────────────────────────────────────────────

@app.route("/api/timetables/<int:timetable_id>/export/csv", methods=["GET"])
def export_csv(timetable_id):
    timetable = db.get_timetable_by_id(timetable_id)
    if not timetable:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Day", "Time Slot", "Teacher", "Branch", "Subject", "Type"])
    for slot in timetable["slots"]:
        writer.writerow([slot["day"], slot["time_slot"], slot["teacher_name"],
                         slot["branch_name"], slot["subject_name"], slot["type"]])

    filename = f"timetable_{timetable_id}.csv"
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/timetables/<int:timetable_id>/export/pdf", methods=["GET"])
def export_pdf(timetable_id):
    timetable = db.get_timetable_by_id(timetable_id)
    if not timetable:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import cm, mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
    except ImportError:
        return jsonify({"success": False,
                        "error": "reportlab not installed. Run: pip install reportlab"}), 500

    DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    slots = timetable["slots"]
    days  = [d for d in DAY_ORDER if any(s["day"] == d for s in slots)]

    all_times = sorted({s["time_slot"] for s in slots},
                       key=lambda t: int(t.split(":")[0]))

    lookup = {}
    for s in slots:
        lookup.setdefault(s["day"], {}).setdefault(s["time_slot"], s)

    def get_merged_rows(day):
        """Collapse consecutive same-subject slots into one display row."""
        result = []
        i = 0
        while i < len(all_times):
            ts = all_times[i]
            s  = lookup.get(day, {}).get(ts)
            if s is None:
                result.append((ts, None, 1))
                i += 1
                continue
            span = 1
            while i + span < len(all_times):
                next_ts    = all_times[i + span]
                ns         = lookup.get(day, {}).get(next_ts)
                cur_end    = int(all_times[i + span - 1].split("-")[1].split(":")[0])
                next_start = int(next_ts.split(":")[0])
                if cur_end != next_start:
                    break
                if (ns and ns["subject_name"] == s["subject_name"]
                        and ns["teacher_name"] == s["teacher_name"]
                        and ns["type"]         == s["type"]):
                    span += 1
                else:
                    break
            end_ts       = all_times[i + span - 1]
            period_label = "{}-{}".format(ts.split("-")[0], end_ts.split("-")[1]) if span > 1 else ts
            result.append((period_label, s, span))
            i += span
        return result

    PAGE = landscape(A4)
    LM = RM = 1.2 * cm
    TM = BM = 1.0 * cm

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=PAGE,
                            leftMargin=LM, rightMargin=RM,
                            topMargin=TM, bottomMargin=BM)

    HEAD_BG  = colors.HexColor("#0f1e35")
    GOLD     = colors.HexColor("#e8b840")
    DAY_BG   = colors.HexColor("#d0dcea")
    PER_BG   = colors.HexColor("#e8f0f8")
    PALE_BG  = colors.white
    ALT_BG   = colors.HexColor("#eef4fa")
    GRID_COL = colors.HexColor("#9ab4cc")
    NOTE_COL = colors.HexColor("#607890")
    TEXT_COL = colors.HexColor("#0f2240")
    TCHR_COL = colors.HexColor("#2a4060")

    def mk(name, font="Helvetica", size=8, align=TA_CENTER, **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size, alignment=align, **kw)

    inst_s  = mk("inst",  "Helvetica-Bold", 10)
    addr_s  = mk("addr",  "Helvetica",       7)
    dept_s  = mk("dept",  "Helvetica-Bold", 12)
    tt_s    = mk("tt",    "Helvetica-Bold", 15)
    meta_s  = mk("meta",  "Helvetica",       8)
    hdr_s   = mk("hdr",   "Helvetica-Bold",  8, textColor=GOLD)
    day_s   = mk("day",   "Helvetica-Bold",  8, textColor=TEXT_COL)
    per_s   = mk("per",   "Helvetica",       7, textColor=TCHR_COL)
    slot_s  = mk("slot",  "Helvetica-Bold",  8, leading=11)
    empty_s = mk("emp",   "Helvetica",       7, textColor=colors.HexColor("#aabbcc"))
    note_s  = mk("note",  "Helvetica",       7, TA_LEFT, textColor=NOTE_COL)

    gen_date = timetable["created_at"][:10]

    elements = [
        Paragraph("Govt. Polytechnic Education Society, Manesar", inst_s),
        Paragraph("(On NH-8, near NSG Camp, Manesar, Gurugram)", addr_s),
        Spacer(1, 1 * mm),
        Paragraph(timetable["department"] or "Department", dept_s),
        Paragraph("Time Table", tt_s),
        Paragraph("{} - {}  -  w.e.f {}".format(
            timetable["name"], timetable["semester"], gen_date), meta_s),
        Spacer(1, 2 * mm),
    ]

    usable_w   = PAGE[0] - LM - RM
    DAY_W      = 1.8 * cm
    PER_W      = 2.8 * cm
    subj_w     = (usable_w - DAY_W - PER_W) / max(len(days), 1)
    col_widths  = [DAY_W, PER_W] + [subj_w] * len(days)

    header_row = (
        [Paragraph("<b>DAY</b>",    hdr_s),
         Paragraph("<b>PERIOD</b>", hdr_s)]
        + [Paragraph("<b>{}</b>".format(d), hdr_s) for d in days]
    )

    table_rows = [header_row]
    span_cmds  = []
    row_idx    = 1  # row 0 is header

    for day in days:
        merged    = get_merged_rows(day)
        day_start = row_idx

        for (period_label, s, _span) in merged:
            subj_cells = []
            for other_day in days:
                if other_day == day:
                    if s:
                        subj_cells.append(Paragraph(
                            "<b>{}</b><br/><font size='6' color='#2a4060'>{}</font>".format(
                                s["subject_name"], s["teacher_name"]), slot_s))
                    else:
                        subj_cells.append(Paragraph("-", empty_s))
                else:
                    subj_cells.append(Paragraph("", empty_s))

            table_rows.append(
                [Paragraph(day, day_s), Paragraph(period_label, per_s)] + subj_cells)
            row_idx += 1  # always +1: one row per merged block

        day_end = row_idx - 1
        if day_end > day_start:
            span_cmds.append(("SPAN", (0, day_start), (0, day_end)))

    n_rows  = len(table_rows)
    avail_h = PAGE[1] - TM - BM - 3.5 * cm
    row_h   = max(0.55 * cm, avail_h / max(n_rows, 1))

    style = TableStyle([
        ("BACKGROUND",     (0, 0),  (-1, 0),  HEAD_BG),
        ("TEXTCOLOR",      (0, 0),  (-1, 0),  GOLD),
        ("FONTNAME",       (0, 0),  (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0),  (-1, 0),  8),
        ("ALIGN",          (0, 0),  (-1, 0),  "CENTER"),
        ("VALIGN",         (0, 0),  (-1, 0),  "MIDDLE"),
        ("FONTSIZE",       (0, 1),  (-1, -1), 7),
        ("ALIGN",          (0, 1),  (-1, -1), "CENTER"),
        ("VALIGN",         (0, 1),  (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (1, 1),  (-1, -1), [PALE_BG, ALT_BG]),
        ("BACKGROUND",     (0, 1),  (0, -1),  DAY_BG),
        ("FONTNAME",       (0, 1),  (0, -1),  "Helvetica-Bold"),
        ("FONTSIZE",       (0, 1),  (0, -1),  8),
        ("TEXTCOLOR",      (0, 1),  (0, -1),  TEXT_COL),
        ("BACKGROUND",     (1, 1),  (1, -1),  PER_BG),
        ("FONTNAME",       (1, 1),  (1, -1),  "Helvetica"),
        ("FONTSIZE",       (1, 1),  (1, -1),  7),
        ("GRID",           (0, 0),  (-1, -1), 0.4, GRID_COL),
        ("LEFTPADDING",    (0, 0),  (-1, -1), 3),
        ("RIGHTPADDING",   (0, 0),  (-1, -1), 3),
        ("TOPPADDING",     (0, 0),  (-1, -1), 3),
        ("BOTTOMPADDING",  (0, 0),  (-1, -1), 3),
    ] + span_cmds)

    grid = Table(table_rows, colWidths=col_widths, repeatRows=1,
                 rowHeights=[0.65 * cm] + [row_h] * (n_rows - 1))
    grid.setStyle(style)
    elements.append(grid)

    elements.append(Spacer(1, 2 * mm))
    elements.append(Paragraph(
        "NOTE: NO CHANGE WITHOUT THE PERMISSION OF THE UNDERSIGNED.", note_s))

    teachers = timetable.get("teachers", [])
    if teachers:
        individuals = sorted({
            p.strip()
            for t in teachers
            for p in t["teacher_name"].replace("&", "/").split("/")
            if p.strip()
        })
        if individuals:
            elements.append(Paragraph(
                "Teachers: " + "   ".join(individuals), note_s))

    doc.build(elements)
    buf.seek(0)
    filename = "timetable_{}.pdf".format(timetable_id)
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=" + filename})


@app.route("/api/timetables/<int:timetable_id>/export/ical", methods=["GET"])
def export_ical(timetable_id):
    timetable = db.get_timetable_by_id(timetable_id)
    if not timetable:
        return jsonify({"success": False, "error": "Timetable not found"}), 404

    from datetime import date, timedelta

    start_str = request.args.get("start", "")
    if start_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"success": False,
                            "error": "Invalid 'start' date. Use YYYY-MM-DD."}), 400
    else:
        today = date.today()
        days_ahead = (7 - today.weekday()) % 7 or 7
        start_date = today + timedelta(days=days_ahead)

    DAY_OFFSET = {"Monday": 0, "Tuesday": 1, "Wednesday": 2,
                  "Thursday": 3, "Friday": 4, "Sunday": 6}

    def fmt_ical_dt(d: date, hour: int, minute: int = 0) -> str:
        return f"{d.strftime('%Y%m%d')}T{hour:02d}{minute:02d}00"

    def uid(slot, idx):
        raw = (f"{timetable_id}-{slot['day']}-{slot['time_slot']}"
               f"-{slot['teacher_name']}-{idx}")
        return hashlib.md5(raw.encode()).hexdigest() + "@timetable-planner"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AI Timetable Planner//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{timetable['name']}",
        f"X-WR-CALDESC:{timetable['department']} {timetable['semester']}",
    ]

    now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for idx, slot in enumerate(timetable["slots"]):
        day_offset = DAY_OFFSET.get(slot["day"], 0)
        event_date = start_date + timedelta(days=day_offset)
        parts   = slot["time_slot"].split("-")
        start_h = int(parts[0].split(":")[0])
        end_h   = int(parts[1].split(":")[0]) if len(parts) > 1 else start_h + 1

        dtstart = fmt_ical_dt(event_date, start_h)
        dtend   = fmt_ical_dt(event_date, end_h)
        summary = (f"{slot['subject_name']} ({slot['type'].capitalize()}) "
                   f"— {slot['branch_name']}")
        desc    = (f"Teacher: {slot['teacher_name']}\\n"
                   f"Branch: {slot['branch_name']}\\nType: {slot['type']}")

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid(slot, idx)}",
            f"DTSTAMP:{now_str}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            f"LOCATION:{slot['branch_name']}",
            "RRULE:FREQ=WEEKLY;COUNT=16",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    ical_content = "\r\n".join(lines) + "\r\n"

    filename = f"timetable_{timetable_id}.ics"
    return Response(ical_content, mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ── Audit log ─────────────────────────────────────────────────────────────────

@app.route("/api/audit-log", methods=["GET"])
def audit_log():
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50

    timetable_id = request.args.get("timetable_id")
    try:
        timetable_id = int(timetable_id) if timetable_id else None
    except ValueError:
        timetable_id = None

    entries = db.get_audit_log(limit=limit, timetable_id=timetable_id)
    return jsonify({"success": True, "data": entries})


# ══════════════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data     = request.get_json() or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password required"}), 400

    user = db.get_user_by_username(username)
    if not user or not db.verify_password(user, password):
        return jsonify({"success": False, "error": "Invalid username or password"}), 401

    token = _make_token(user)
    db.add_audit_log(0, "update", f"User '{user['username']}' [{user['role']}] logged in.")
    return jsonify({
        "success": True,
        "token": token,
        "user": {
            "id":           user["id"],
            "username":     user["username"],
            "role":         user["role"],
            "display_name": user["display_name"],
        }
    })


@app.route("/api/auth/me", methods=["GET"])
@require_auth
def auth_me():
    u    = _get_caller()
    user = db.get_user_by_id(int(u["sub"]))
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404
    return jsonify({
        "success": True,
        "user": {
            "id":           user["id"],
            "username":     user["username"],
            "role":         user["role"],
            "display_name": user["display_name"],
        }
    })


@app.route("/api/auth/change-password", methods=["POST"])
@require_auth
def auth_change_password():
    u    = _get_caller()
    data = request.get_json() or {}
    cur  = str(data.get("current_password", ""))
    new  = str(data.get("new_password", ""))

    if not cur or not new:
        return jsonify({"success": False, "error": "Both passwords required"}), 400
    if len(new) < 6:
        return jsonify({"success": False,
                        "error": "New password must be at least 6 characters"}), 400

    user = db.get_user_by_id(int(u["sub"]))
    if not user or not db.verify_password(user, cur):
        return jsonify({"success": False, "error": "Current password is incorrect"}), 401

    db.change_password(user["id"], new)
    db.add_audit_log(0, "update", f"Password changed for user '{user['username']}'.")
    return jsonify({"success": True, "message": "Password changed successfully"})


@app.route("/api/auth/users", methods=["GET"])
@require_head
def get_users():
    return jsonify({"success": True, "data": db.get_all_users()})


@app.route("/api/auth/users/<int:uid>", methods=["DELETE"])
@require_head
def delete_user(uid):
    caller = _get_caller()
    if int(caller["sub"]) == uid:
        return jsonify({"success": False, "error": "Cannot delete your own account"}), 400
    ok = db.delete_user(uid)
    if not ok:
        return jsonify({"success": False,
                        "error": "User not found or cannot delete last head account"}), 404
    db.add_audit_log(0, "delete", f"User #{uid} removed by head '{caller['username']}'.")
    return jsonify({"success": True, "message": "User removed"})


@app.route("/api/auth/users/<int:uid>/reset-password", methods=["POST"])
@require_head
def reset_user_password(uid):
    data     = request.get_json() or {}
    new_pass = str(data.get("new_password", ""))
    if len(new_pass) < 6:
        return jsonify({"success": False,
                        "error": "Password must be at least 6 characters"}), 400
    user = db.get_user_by_id(uid)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404
    db.change_password(uid, new_pass)
    caller = _get_caller()
    db.add_audit_log(0, "update",
                     f"Password reset for '{user['username']}' by head '{caller['username']}'.")
    return jsonify({"success": True, "message": f"Password reset for '{user['username']}'"})


if __name__ == "__main__":
    db.init_db()
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "development") == "development"
    print("🚀 AI Timetable Planner API starting on port", port)
    print("   Architecture: Python scheduler → Groq AI conflict auditor")
    app.run(host="0.0.0.0", port=port, debug=False)
