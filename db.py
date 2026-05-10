import os
import math
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import psycopg2.extras

# Produces the same string format as SQLite's datetime('now'): 'YYYY-MM-DD HH:MM:SS'
_NOW = "TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')"


def get_connection():
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    return conn


def _cur(conn):
    """Return a cursor whose rows behave as plain Python dicts."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ── Schema init ─────────────────────────────────────────────────────────────

def init_db():
    conn = get_connection()
    c = _cur(conn)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS timetables (
            id          SERIAL PRIMARY KEY,
            name        TEXT   NOT NULL,
            semester    TEXT   NOT NULL DEFAULT '',
            department  TEXT   NOT NULL DEFAULT '',
            created_at  TEXT   NOT NULL DEFAULT ({_NOW}),
            updated_at  TEXT   NOT NULL DEFAULT ({_NOW})
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS teachers (
            id              SERIAL  PRIMARY KEY,
            timetable_id    INTEGER NOT NULL,
            teacher_name    TEXT    NOT NULL,
            branch_name     TEXT    NOT NULL,
            subject_name    TEXT    NOT NULL,
            no_of_lectures  INTEGER NOT NULL DEFAULT 0,
            no_of_labs      INTEGER NOT NULL DEFAULT 0,
            lecture_length  REAL    NOT NULL DEFAULT 1.0,
            lab_length      REAL    NOT NULL DEFAULT 2.0,
            FOREIGN KEY (timetable_id) REFERENCES timetables(id) ON DELETE CASCADE
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS slots (
            id              SERIAL  PRIMARY KEY,
            timetable_id    INTEGER NOT NULL,
            day             TEXT    NOT NULL,
            time_slot       TEXT    NOT NULL,
            teacher_name    TEXT    NOT NULL,
            branch_name     TEXT    NOT NULL,
            subject_name    TEXT    NOT NULL,
            type            TEXT    NOT NULL CHECK(type IN ('lecture', 'lab')),
            FOREIGN KEY (timetable_id) REFERENCES timetables(id) ON DELETE CASCADE
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS audit_log (
            id           SERIAL  PRIMARY KEY,
            timetable_id INTEGER NOT NULL DEFAULT 0,
            action       TEXT    NOT NULL,
            detail       TEXT    NOT NULL DEFAULT '',
            created_at   TEXT    NOT NULL DEFAULT ({_NOW})
        )
    """)

    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_slots_timetable    ON slots(timetable_id)",
        "CREATE INDEX IF NOT EXISTS idx_teachers_timetable ON teachers(timetable_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_timetable    ON audit_log(timetable_id)",
        "CREATE INDEX IF NOT EXISTS idx_timetables_dept    ON timetables(department)",
        "CREATE INDEX IF NOT EXISTS idx_timetables_sem     ON timetables(semester)",
    ]:
        c.execute(stmt)

    conn.commit()
    conn.close()
    print("Database initialized.")
    init_auth_db()


# ── Timetable CRUD ───────────────────────────────────────────────────────────

def create_timetable(name: str, semester: str, department: str) -> int:
    conn = get_connection()
    c = _cur(conn)
    c.execute(
        "INSERT INTO timetables (name, semester, department) VALUES (%s, %s, %s) RETURNING id",
        (name, semester, department)
    )
    timetable_id = c.fetchone()['id']
    conn.commit()
    conn.close()
    return timetable_id


def get_all_timetables(page=1, limit=50, dept_filter="", semester_filter="", search="") -> dict:
    conn = get_connection()
    c = _cur(conn)

    where_parts = []
    params = []

    if dept_filter:
        where_parts.append("LOWER(t.department) = LOWER(%s)")
        params.append(dept_filter)
    if semester_filter:
        where_parts.append("LOWER(t.semester) = LOWER(%s)")
        params.append(semester_filter)
    if search:
        where_parts.append(
            "(LOWER(t.name) LIKE LOWER(%s) OR LOWER(t.department) LIKE LOWER(%s)"
            " OR LOWER(t.semester) LIKE LOWER(%s))"
        )
        like = f"%{search}%"
        params.extend([like, like, like])

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    c.execute(f"SELECT COUNT(*) AS cnt FROM timetables t {where_sql}", params)
    total = c.fetchone()['cnt']

    offset = (page - 1) * limit
    c.execute(f"""
        SELECT
            t.id, t.name, t.semester, t.department, t.created_at, t.updated_at,
            COUNT(DISTINCT tc.teacher_name)                             AS teacher_count,
            COUNT(DISTINCT s.id)                                        AS slot_count,
            COUNT(DISTINCT CASE WHEN s.type = 'lecture' THEN s.id END) AS lecture_count,
            COUNT(DISTINCT CASE WHEN s.type = 'lab'     THEN s.id END) AS lab_count,
            COUNT(DISTINCT s.day)                                       AS day_count
        FROM timetables t
        LEFT JOIN teachers tc ON tc.timetable_id = t.id
        LEFT JOIN slots     s  ON s.timetable_id  = t.id
        {where_sql}
        GROUP BY t.id
        ORDER BY t.created_at DESC
        LIMIT %s OFFSET %s
    """, params + [limit, offset])

    rows = c.fetchall()
    conn.close()
    return {
        "data":        [dict(row) for row in rows],
        "total":       total,
        "page":        page,
        "limit":       limit,
        "total_pages": max(1, math.ceil(total / limit)),
    }


def get_timetable_by_id(timetable_id: int):
    conn = get_connection()
    c = _cur(conn)

    c.execute("SELECT * FROM timetables WHERE id = %s", (timetable_id,))
    timetable = c.fetchone()
    if not timetable:
        conn.close()
        return None

    c.execute("SELECT * FROM teachers WHERE timetable_id = %s", (timetable_id,))
    teachers = [dict(r) for r in c.fetchall()]

    c.execute(
        "SELECT * FROM slots WHERE timetable_id = %s ORDER BY day, time_slot",
        (timetable_id,)
    )
    slots = [dict(r) for r in c.fetchall()]

    conn.close()
    return {**dict(timetable), "teachers": teachers, "slots": slots}


def update_timetable(timetable_id: int, name: str, semester: str, department: str) -> bool:
    conn = get_connection()
    c = _cur(conn)
    c.execute(
        f"UPDATE timetables SET name=%s, semester=%s, department=%s, updated_at={_NOW} WHERE id=%s",
        (name, semester, department, timetable_id)
    )
    updated = c.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_timetable(timetable_id: int) -> bool:
    conn = get_connection()
    c = _cur(conn)
    c.execute("DELETE FROM timetables WHERE id = %s", (timetable_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def duplicate_timetable(timetable_id: int, new_name: str):
    original = get_timetable_by_id(timetable_id)
    if not original:
        return None

    new_id = create_timetable(new_name, original["semester"], original["department"])
    conn = get_connection()
    c = _cur(conn)

    for t in original["teachers"]:
        c.execute(
            """INSERT INTO teachers
               (timetable_id, teacher_name, branch_name, subject_name,
                no_of_lectures, no_of_labs, lecture_length, lab_length)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (new_id, t["teacher_name"], t["branch_name"], t["subject_name"],
             t["no_of_lectures"], t["no_of_labs"], t["lecture_length"], t["lab_length"])
        )
    for s in original["slots"]:
        c.execute(
            """INSERT INTO slots
               (timetable_id, day, time_slot, teacher_name, branch_name, subject_name, type)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (new_id, s["day"], s["time_slot"], s["teacher_name"],
             s["branch_name"], s["subject_name"], s["type"])
        )

    conn.commit()
    conn.close()
    return new_id


def save_teachers(timetable_id: int, teachers: list) -> None:
    conn = get_connection()
    c = _cur(conn)
    for t in teachers:
        c.execute(
            """INSERT INTO teachers
               (timetable_id, teacher_name, branch_name, subject_name,
                no_of_lectures, no_of_labs, lecture_length, lab_length)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (timetable_id, t["teacher_name"], t["branch_name"], t["subject_name"],
             t.get("no_of_lectures", 0), t.get("no_of_labs", 0),
             t.get("lecture_length", 1.0), t.get("lab_length", 2.0))
        )
    conn.commit()
    conn.close()


def save_slots(timetable_id: int, slots: list) -> None:
    conn = get_connection()
    c = _cur(conn)
    for s in slots:
        c.execute(
            """INSERT INTO slots
               (timetable_id, day, time_slot, teacher_name, branch_name, subject_name, type)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (timetable_id, s["day"], s["time_slot"], s["teacher_name"],
             s["branch_name"], s["subject_name"], s["type"])
        )
    c.execute(
        f"UPDATE timetables SET updated_at = {_NOW} WHERE id = %s",
        (timetable_id,)
    )
    conn.commit()
    conn.close()


def delete_slots_for_timetable(timetable_id: int) -> None:
    conn = get_connection()
    c = _cur(conn)
    c.execute("DELETE FROM slots WHERE timetable_id = %s", (timetable_id,))
    conn.commit()
    conn.close()


def get_all_slots(exclude_timetable_id=None) -> list:
    conn = get_connection()
    c = _cur(conn)
    if exclude_timetable_id:
        c.execute("SELECT * FROM slots WHERE timetable_id != %s", (exclude_timetable_id,))
    else:
        c.execute("SELECT * FROM slots")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def check_conflicts(new_slots: list, exclude_timetable_id=None) -> list:
    """Check for teacher double-booking across timetables.

    Branch/subject name conflicts are intentionally NOT checked here because
    the same subject (e.g. SCA) can legitimately be taught to different student
    groups by different teachers at the same time.  Only teacher availability
    is a hard constraint.
    """
    existing_slots = get_all_slots(exclude_timetable_id=exclude_timetable_id)

    teacher_lookup = {}

    for s in existing_slots:
        for teacher in [n.strip() for n in s["teacher_name"].replace('&', '/').split('/') if n.strip()]:
            tk = (teacher.lower(), s["day"], s["time_slot"])
            teacher_lookup[tk] = (s["timetable_id"], s["subject_name"])

    conflicts = []
    for ns in new_slots:
        for teacher in [n.strip() for n in ns["teacher_name"].replace('&', '/').split('/') if n.strip()]:
            tk = (teacher.lower(), ns["day"], ns["time_slot"])
            if tk in teacher_lookup:
                tid, subj = teacher_lookup[tk]
                conflicts.append(
                    f"Teacher conflict: '{teacher}' already scheduled "
                    f"on {ns['day']} at {ns['time_slot']} (Timetable #{tid} - {subj})"
                )

    return list(dict.fromkeys(conflicts))


def get_system_stats() -> dict:
    conn = get_connection()
    c = _cur(conn)
    c.execute("""
        SELECT
            COUNT(DISTINCT t.id)                                        AS total_timetables,
            COUNT(DISTINCT tc.teacher_name)                             AS total_teachers,
            COUNT(DISTINCT s.id)                                        AS total_slots,
            COUNT(DISTINCT CASE WHEN s.type = 'lecture' THEN s.id END) AS total_lectures,
            COUNT(DISTINCT CASE WHEN s.type = 'lab'     THEN s.id END) AS total_labs,
            COUNT(DISTINCT t.department)                                AS total_departments,
            COUNT(DISTINCT t.semester)                                  AS total_semesters
        FROM timetables t
        LEFT JOIN teachers tc ON tc.timetable_id = t.id
        LEFT JOIN slots     s  ON s.timetable_id  = t.id
    """)
    row = c.fetchone()
    conn.close()
    return dict(row) if row else {}


def add_audit_log(timetable_id: int, action: str, detail: str = "") -> None:
    try:
        conn = get_connection()
        c = _cur(conn)
        c.execute(
            "INSERT INTO audit_log (timetable_id, action, detail) VALUES (%s, %s, %s)",
            (timetable_id, action, detail)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_audit_log(limit=50, timetable_id=None) -> list:
    conn = get_connection()
    c = _cur(conn)
    if timetable_id is not None:
        c.execute(
            "SELECT * FROM audit_log WHERE timetable_id = %s ORDER BY created_at DESC LIMIT %s",
            (timetable_id, limit)
        )
    else:
        c.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT %s",
            (limit,)
        )
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ══════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════

def init_auth_db():
    conn = get_connection()
    c = _cur(conn)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            username      TEXT   NOT NULL UNIQUE,
            password_hash TEXT   NOT NULL,
            role          TEXT   NOT NULL CHECK(role IN ('head', 'teacher')),
            display_name  TEXT   NOT NULL DEFAULT '',
            created_at    TEXT   NOT NULL DEFAULT ({_NOW})
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS predefined_teachers (
            id                  SERIAL  PRIMARY KEY,
            name                TEXT    NOT NULL UNIQUE,
            is_registered       INTEGER NOT NULL DEFAULT 0,
            registered_username TEXT    DEFAULT NULL,
            added_at            TEXT    NOT NULL DEFAULT ({_NOW})
        )
    """)

    c.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_role     ON users(role)")
    conn.commit()

    # Seed default head account
    c.execute("SELECT id FROM users WHERE role = 'head' LIMIT 1")
    if not c.fetchone():
        ph = generate_password_hash('admin123')
        c.execute(
            "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,%s,%s)",
            ('admin', ph, 'head', 'Admin')
        )
        conn.commit()
        print("⚠  Admin account created — login: admin / admin123")
        print("   Change this password via Admin Panel immediately.")

    # Seed default teacher account
    c.execute("SELECT id FROM users WHERE username = 'user' LIMIT 1")
    if not c.fetchone():
        ph = generate_password_hash('teacher123')
        c.execute(
            "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,%s,%s)",
            ('user', ph, 'teacher', 'User')
        )
        conn.commit()
        print("⚠  User account created — login: user / teacher123")
        print("   Change this password via Admin Panel.")

    conn.close()


def get_user_by_username(username: str):
    conn = get_connection()
    c = _cur(conn)
    c.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int):
    conn = get_connection()
    c = _cur(conn)
    c.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def create_user(username: str, password: str, role: str, display_name: str) -> int:
    conn = get_connection()
    c = _cur(conn)
    ph = generate_password_hash(password)
    c.execute(
        "INSERT INTO users (username, password_hash, role, display_name) VALUES (%s,%s,%s,%s) RETURNING id",
        (username.strip().lower(), ph, role, display_name)
    )
    uid = c.fetchone()['id']
    conn.commit()
    conn.close()
    return uid


def verify_password(user: dict, password: str) -> bool:
    return check_password_hash(user['password_hash'], password)


def change_password(user_id: int, new_password: str) -> bool:
    conn = get_connection()
    c = _cur(conn)
    ph = generate_password_hash(new_password)
    c.execute("UPDATE users SET password_hash = %s WHERE id = %s", (ph, user_id))
    updated = c.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_all_users() -> list:
    conn = get_connection()
    c = _cur(conn)
    c.execute(
        "SELECT id, username, role, display_name, created_at FROM users ORDER BY role, created_at"
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_user(user_id: int) -> bool:
    conn = get_connection()
    c = _cur(conn)
    # Protect the last head account
    c.execute("SELECT COUNT(*) AS cnt FROM users WHERE role='head'")
    head_count = c.fetchone()['cnt']
    c.execute("SELECT role FROM users WHERE id=%s", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    if row['role'] == 'head' and head_count <= 1:
        conn.close()
        return False
    c.execute("DELETE FROM users WHERE id=%s", (user_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# ── Predefined teacher list ──────────────────────────────────────────────────

def get_predefined_teachers() -> list:
    conn = get_connection()
    c = _cur(conn)
    c.execute("SELECT * FROM predefined_teachers ORDER BY name")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_predefined_teacher(name: str) -> bool:
    try:
        conn = get_connection()
        c = _cur(conn)
        c.execute("INSERT INTO predefined_teachers (name) VALUES (%s)", (name.strip(),))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def remove_predefined_teacher(teacher_id: int) -> bool:
    conn = get_connection()
    c = _cur(conn)
    c.execute(
        "DELETE FROM predefined_teachers WHERE id = %s AND is_registered = 0",
        (teacher_id,)
    )
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def is_teacher_available(name: str) -> bool:
    conn = get_connection()
    c = _cur(conn)
    c.execute(
        "SELECT id FROM predefined_teachers WHERE LOWER(name) = LOWER(%s) AND is_registered = 0",
        (name,)
    )
    row = c.fetchone()
    conn.close()
    return row is not None


def mark_teacher_registered(name: str, username: str) -> None:
    conn = get_connection()
    c = _cur(conn)
    c.execute(
        "UPDATE predefined_teachers SET is_registered=1, registered_username=%s WHERE LOWER(name)=LOWER(%s)",
        (username, name)
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
