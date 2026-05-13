"""
Local SQLite database — single source of truth for the MHM client.

Schema overview:
  courses        — Race courses (e.g. "4-Hour", "8-Hour")
  control_points — SI timing controls belonging to a course (one row per control per course)
  classes        — Competition categories (e.g. "Open", "Competitive")
  racers         — Teams (confusingly called "racers" but represent whole teams)
  course_entries — One team's enrolment in one course (chip assignment + times)
  punches        — Individual SI punch records downloaded from a chip
  entry_adjustments — Manual bonuses/penalties applied by officials
  users          — Web UI login credentials

Design decisions:
  - WAL journal mode: allows readers and one writer to operate concurrently without
    blocking each other. Important because the SI reader thread writes punches while
    the web server is serving result pages.
  - Foreign keys ON: enforces referential integrity (e.g. can't add a punch for a
    non-existent entry). SQLite requires this to be set per-connection.
  - synced column: every table that gets pushed to the remote server has a `synced`
    flag (0 = pending, 1 = confirmed by server). This lets get_unsynced() collect
    only new/changed records efficiently.
  - No ORM: plain sqlite3 with Row factory for dict-like access. Keeps the code
    simple and avoids a heavy dependency.
"""
import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from client.utils import data_path

# Database file lives in the writable data directory (next to .exe when frozen).
DB_PATH = data_path("mhm_local.db")


def get_connection() -> sqlite3.Connection:
    """
    Open a database connection with WAL mode and foreign key enforcement.

    Row factory is set to sqlite3.Row so result columns can be accessed by name
    (e.g. row["name"]) instead of by index.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # concurrent read+write
    conn.execute("PRAGMA foreign_keys=ON")       # enforce FK constraints
    return conn


@contextmanager
def db():
    """
    Context manager that provides a connection, commits on success, rolls back on error.

    Usage:
        with db() as conn:
            conn.execute(...)
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema initialisation ─────────────────────────────────────────────────────

def init_db():
    """
    Create all tables if they don't exist, run any pending migrations, and seed admin user.

    Called once at app startup (in the FastAPI lifespan handler).
    Safe to call on an existing database — CREATE TABLE IF NOT EXISTS is idempotent.
    """
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS courses (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            name                        TEXT NOT NULL,
            time_limit_minutes          INTEGER NOT NULL DEFAULT 480,
            overtime_penalty_per_minute INTEGER NOT NULL DEFAULT 0,
            start_time                  TEXT,
            end_time                    TEXT,
            cutoff_time                 TEXT,
            use_si_start                INTEGER NOT NULL DEFAULT 0,
            synced                      INTEGER NOT NULL DEFAULT 0
        );

        -- One control point per row. The same SI station can appear in multiple
        -- courses with different point values (e.g. a shared gate on two routes).
        CREATE TABLE IF NOT EXISTS control_points (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id              INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            si_code                INTEGER NOT NULL,
            name                   TEXT NOT NULL DEFAULT '',
            points                 INTEGER NOT NULL DEFAULT 0,
            is_mandatory           INTEGER NOT NULL DEFAULT 0,
            mandatory_order        INTEGER,
            mandatory_miss_penalty INTEGER NOT NULL DEFAULT 0,
            circuit_group          TEXT NOT NULL DEFAULT '',
            circuit_miss_penalty   INTEGER NOT NULL DEFAULT 0
        );

        -- Race categories for separate class rankings (e.g. "Open", "Elite").
        -- final_course_id: when a chip is read for this course the team is auto-marked FINISHED.
        CREATE TABLE IF NOT EXISTS classes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            final_course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS racers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            bib_number  TEXT NOT NULL DEFAULT '',
            class_id    INTEGER REFERENCES classes(id),
            synced      INTEGER NOT NULL DEFAULT 0
        );

        -- One row per team per course. UNIQUE(racer_id, course_id) prevents
        -- a team being enrolled in the same course twice.
        CREATE TABLE IF NOT EXISTS course_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            racer_id    INTEGER NOT NULL REFERENCES racers(id) ON DELETE CASCADE,
            course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            si_chip     INTEGER,
            start_time  TEXT,
            finish_time TEXT,
            status      TEXT NOT NULL DEFAULT 'SIGNED_UP',
            synced      INTEGER NOT NULL DEFAULT 0,
            UNIQUE(racer_id, course_id)
        );

        -- Individual SI punch records. INSERT OR IGNORE prevents duplicate
        -- punches if the same chip is read twice (same entry_id + si_code + time).
        CREATE TABLE IF NOT EXISTS punches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id    INTEGER NOT NULL REFERENCES course_entries(id) ON DELETE CASCADE,
            si_code     INTEGER NOT NULL,
            punch_time  TEXT NOT NULL,
            is_manual   INTEGER NOT NULL DEFAULT 0,
            synced      INTEGER NOT NULL DEFAULT 0
        );

        -- Reusable bonus/penalty templates — race directors create these once and
        -- then apply them to individual entries via the Adjustments page.
        -- default_points is always a positive magnitude; category determines sign when applied.
        CREATE TABLE IF NOT EXISTS adjustment_types (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL UNIQUE,
            category       TEXT NOT NULL DEFAULT 'bonus' CHECK(category IN ('bonus','penalty')),
            default_points INTEGER NOT NULL DEFAULT 0,
            description    TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS entry_adjustments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id    INTEGER NOT NULL REFERENCES course_entries(id) ON DELETE CASCADE,
            description TEXT NOT NULL DEFAULT '',
            points      INTEGER NOT NULL DEFAULT 0,
            -- type_id links back to adjustment_types when a predefined type was applied;
            -- NULL means the adjustment was entered free-form (manual).
            type_id     INTEGER,
            -- 'bonus', 'penalty', or 'manual' — used to split the leaderboard columns.
            -- Existing rows without this column get 'manual' as the migration default.
            category    TEXT NOT NULL DEFAULT 'manual',
            synced      INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        );

        -- Maps which courses belong to each class so new racers are auto-enrolled
        -- when a class is assigned to them.
        CREATE TABLE IF NOT EXISTS class_courses (
            class_id  INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
            course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
            PRIMARY KEY (class_id, course_id)
        );

        -- Named chip library — maps a human-readable chip name (e.g. "Red 01") to
        -- its SI code. Pre-loaded before race day; used for bulk chip assignment.
        CREATE TABLE IF NOT EXISTS chips (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL UNIQUE,
            si_code INTEGER NOT NULL UNIQUE
        );
        """)

        # Migration: add timing columns to courses.
        for _col, _dflt in [
            ("start_time",  "TEXT"),
            ("end_time",    "TEXT"),
            ("cutoff_time", "TEXT"),
            ("use_si_start","INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE courses ADD COLUMN {_col} {_dflt}")
            except Exception:
                pass

        # Migration: add final_course_id to classes.
        try:
            conn.execute(
                "ALTER TABLE classes ADD COLUMN final_course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL"
            )
        except Exception:
            pass

        # Migration: add class_id to racers for databases created before classes were added.
        # Wrapped in try/except because ALTER TABLE fails if the column already exists.
        try:
            conn.execute("ALTER TABLE racers ADD COLUMN class_id INTEGER REFERENCES classes(id)")
        except Exception:
            pass    # column already exists — nothing to do

        # Migration: add type_id and category to entry_adjustments.
        # Existing rows get type_id=NULL (manual) and category='manual' via the DEFAULT.
        try:
            conn.execute("ALTER TABLE entry_adjustments ADD COLUMN type_id INTEGER")
        except Exception:
            pass
        try:
            conn.execute(
                "ALTER TABLE entry_adjustments ADD COLUMN category TEXT NOT NULL DEFAULT 'manual'"
            )
        except Exception:
            pass

        try:
            conn.execute(
                "ALTER TABLE control_points ADD COLUMN mandatory_miss_penalty INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        try:
            conn.execute(
                "ALTER TABLE control_points ADD COLUMN circuit_group TEXT NOT NULL DEFAULT ''"
            )
        except Exception:
            pass

        try:
            conn.execute(
                "ALTER TABLE control_points ADD COLUMN circuit_miss_penalty INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        _seed_admin(conn)


# ── Password hashing ──────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """
    Hash a password using PBKDF2-HMAC-SHA256 with a random salt.

    Stored format: "<salt_hex>:<dk_hex>"
    200,000 iterations matches OWASP minimum recommendation.
    """
    salt = secrets.token_hex(16)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify a plaintext password against a stored _hash_password() value."""
    try:
        salt, dk_hex = stored.split(":", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        # compare_digest prevents timing attacks.
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _seed_admin(conn: sqlite3.Connection):
    """Create the default admin/admin user on first run if it doesn't exist."""
    exists = conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO users (username, password) VALUES ('admin', ?)",
            (_hash_password("admin"),),
        )


# ── Classes ───────────────────────────────────────────────────────────────────

def get_all_classes() -> list[sqlite3.Row]:
    """Return all race classes ordered alphabetically."""
    with db() as conn:
        return conn.execute("SELECT * FROM classes ORDER BY name").fetchall()


def get_or_create_class(name: str) -> int:
    """Return the ID of the class with this name, creating it if it doesn't exist."""
    with db() as conn:
        row = conn.execute("SELECT id FROM classes WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute("INSERT INTO classes (name) VALUES (?)", (name,))
        return cur.lastrowid


def get_class(class_id: int) -> sqlite3.Row | None:
    """Return a single class row by ID, or None if not found."""
    with db() as conn:
        return conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()


def upsert_class(
    name: str,
    class_id: int | None = None,
    final_course_id: int | None = None,
) -> int:
    """
    Insert a new class or rename/configure an existing one.

    final_course_id — when a chip is read for this course the team is automatically
                      marked FINISHED. Pass -1 to explicitly clear it.
    Returns the class ID (new or existing).
    """
    with db() as conn:
        if class_id:
            fc = None if final_course_id == -1 else final_course_id
            conn.execute(
                "UPDATE classes SET name=?, final_course_id=? WHERE id=?",
                (name, fc, class_id),
            )
            return class_id
        cur = conn.execute(
            "INSERT INTO classes (name, final_course_id) VALUES (?,?)",
            (name, final_course_id),
        )
        return cur.lastrowid


def delete_class(class_id: int):
    """
    Delete a class. Any racers in that class are unassigned (set to NULL) first.

    We unassign rather than cascade-delete the racers themselves — teams keep
    their registration even if their class is removed.
    """
    with db() as conn:
        conn.execute("UPDATE racers SET class_id=NULL WHERE class_id=?", (class_id,))
        conn.execute("DELETE FROM classes WHERE id=?", (class_id,))


def get_courses_for_class(class_id: int) -> list[int]:
    """Return course IDs associated with a class (for auto-enrollment)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT course_id FROM class_courses WHERE class_id=?", (class_id,)
        ).fetchall()
        return [r["course_id"] for r in rows]


def set_class_courses(class_id: int, course_ids: list[int]):
    """Replace the full set of course associations for a class."""
    with db() as conn:
        conn.execute("DELETE FROM class_courses WHERE class_id=?", (class_id,))
        for cid in course_ids:
            conn.execute(
                "INSERT OR IGNORE INTO class_courses (class_id, course_id) VALUES (?,?)",
                (class_id, cid),
            )


def get_all_class_courses() -> dict[int, list[int]]:
    """Return {class_id: [course_id, ...]} for all classes."""
    with db() as conn:
        rows = conn.execute("SELECT class_id, course_id FROM class_courses").fetchall()
    result: dict[int, list[int]] = {}
    for r in rows:
        result.setdefault(r["class_id"], []).append(r["course_id"])
    return result


# ── Chip library ──────────────────────────────────────────────────────────────

def get_all_chips() -> list[sqlite3.Row]:
    """Return all chips in the library ordered by name."""
    with db() as conn:
        return conn.execute("SELECT * FROM chips ORDER BY name").fetchall()


def upsert_chip(name: str, si_code: int, chip_id: int | None = None) -> int:
    """Insert a new chip or update an existing one. Returns the chip ID."""
    with db() as conn:
        if chip_id:
            conn.execute(
                "UPDATE chips SET name=?, si_code=? WHERE id=?",
                (name, si_code, chip_id),
            )
            return chip_id
        cur = conn.execute(
            "INSERT INTO chips (name, si_code) VALUES (?, ?)", (name, si_code)
        )
        return cur.lastrowid


def delete_chip(chip_id: int):
    """Delete a chip from the library by ID."""
    with db() as conn:
        conn.execute("DELETE FROM chips WHERE id=?", (chip_id,))


def get_chip_by_si(si_code: int) -> sqlite3.Row | None:
    """Return the chip library entry for a given SI code, or None."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM chips WHERE si_code=?", (si_code,)
        ).fetchone()


def clear_chip_assignments(course_id: int | None = None):
    """
    Set si_chip=NULL on all course entries, or only entries for one course.

    Called from the Chips page "Clear Assignments" button before re-running
    auto-assign, or to reset a specific course's chip mapping.
    """
    with db() as conn:
        if course_id:
            conn.execute(
                "UPDATE course_entries SET si_chip=NULL, synced=0 WHERE course_id=?",
                (course_id,),
            )
        else:
            conn.execute("UPDATE course_entries SET si_chip=NULL, synced=0")


def get_all_entries_overview() -> list[sqlite3.Row]:
    """
    Return all course entries with racer name, bib, course name, and chip number.

    Used by the chip bulk-assignment page and the dashboard unknown-reads panel.
    Ordered by course name then racer bib/name.
    """
    with db() as conn:
        return conn.execute(
            """SELECT ce.id, ce.racer_id, ce.course_id, ce.si_chip, ce.status,
                      r.name as racer_name, r.bib_number,
                      c.name as course_name
               FROM course_entries ce
               JOIN racers  r ON ce.racer_id  = r.id
               JOIN courses c ON ce.course_id = c.id
               ORDER BY c.name, r.bib_number, r.name"""
        ).fetchall()


def get_racers_by_class(class_id: int) -> list[sqlite3.Row]:
    """Return all racers assigned to a specific class, ordered by bib then name."""
    with db() as conn:
        return conn.execute(
            """SELECT r.*, c.name as class_name
               FROM racers r LEFT JOIN classes c ON r.class_id=c.id
               WHERE r.class_id=? ORDER BY r.bib_number, r.name""",
            (class_id,),
        ).fetchall()


def get_unclassified_racers() -> list[sqlite3.Row]:
    """Return racers with no class assigned — shown separately in class results."""
    with db() as conn:
        return conn.execute(
            "SELECT *, '' as class_name FROM racers WHERE class_id IS NULL ORDER BY bib_number, name"
        ).fetchall()


# ── Users ─────────────────────────────────────────────────────────────────────

def verify_user(username: str, password: str) -> bool:
    """Return True if the username/password pair is valid."""
    with db() as conn:
        row = conn.execute(
            "SELECT password FROM users WHERE username=?", (username,)
        ).fetchone()
    return bool(row and _verify_password(password, row["password"]))


def get_all_users() -> list[sqlite3.Row]:
    """Return all users (id + username only — no passwords)."""
    with db() as conn:
        return conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()


def create_user(username: str, password: str):
    """Create a new user. Raises sqlite3.IntegrityError if username already exists."""
    with db() as conn:
        conn.execute(
            "INSERT INTO users (username, password) VALUES (?, ?)",
            (username, _hash_password(password)),
        )


def update_password(username: str, new_password: str):
    """Update an existing user's password."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET password=? WHERE username=?",
            (_hash_password(new_password), username),
        )


def delete_user(user_id: int):
    """
    Delete a user by ID. The admin account cannot be deleted.

    The AND username!='admin' guard prevents accidentally locking out the system
    even if the admin account's ID is somehow passed here.
    """
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id=? AND username!='admin'", (user_id,))


# ── Racers ────────────────────────────────────────────────────────────────────

def upsert_racer(
    name: str,
    bib_number: str,
    class_id: int | None = None,
    racer_id: int | None = None,
) -> int:
    """
    Insert a new racer or update an existing one.

    If racer_id is provided, updates that record. Otherwise inserts a new row.
    Returns the racer ID (new or existing).
    Marks synced=0 so the record is pushed to the remote server on next sync.
    """
    with db() as conn:
        if racer_id:
            conn.execute(
                "UPDATE racers SET name=?, bib_number=?, class_id=?, synced=0 WHERE id=?",
                (name, bib_number, class_id, racer_id),
            )
            return racer_id
        cur = conn.execute(
            "INSERT INTO racers (name, bib_number, class_id) VALUES (?, ?, ?)",
            (name, bib_number, class_id),
        )
        return cur.lastrowid


def get_racer_by_name(name: str) -> sqlite3.Row | None:
    """Return the first racer row whose name matches exactly, or None."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM racers WHERE name=? LIMIT 1", (name,)
        ).fetchone()


def get_all_racers() -> list[sqlite3.Row]:
    """Return all racers with their class name joined in, ordered by bib then name."""
    with db() as conn:
        return conn.execute(
            """SELECT r.*, c.name as class_name
               FROM racers r LEFT JOIN classes c ON r.class_id=c.id
               ORDER BY r.bib_number, r.name"""
        ).fetchall()


def get_racer(racer_id: int) -> sqlite3.Row | None:
    """Return a single racer by ID, or None if not found."""
    with db() as conn:
        return conn.execute("SELECT * FROM racers WHERE id=?", (racer_id,)).fetchone()


def delete_racer(racer_id: int):
    """
    Delete a racer and all their course entries (cascaded by FK ON DELETE CASCADE).

    Punches and adjustments on those entries are also cascade-deleted.
    """
    with db() as conn:
        conn.execute("DELETE FROM racers WHERE id=?", (racer_id,))


def merge_racer(source_id: int, target_id: int) -> tuple[int, int]:
    """
    Merge source racer into target racer then delete the source.

    For each course entry on the source:
      - If target has no entry for that course, reassign the entry to target.
      - If target already has an entry for that course, drop the source entry
        (the target's existing data takes precedence).

    Returns (moved, dropped) counts.
    """
    moved = dropped = 0
    with db() as conn:
        src_entries = conn.execute(
            "SELECT id, course_id FROM course_entries WHERE racer_id=?", (source_id,)
        ).fetchall()
        for entry in src_entries:
            conflict = conn.execute(
                "SELECT id FROM course_entries WHERE racer_id=? AND course_id=?",
                (target_id, entry["course_id"]),
            ).fetchone()
            if conflict:
                conn.execute("DELETE FROM course_entries WHERE id=?", (entry["id"],))
                dropped += 1
            else:
                conn.execute(
                    "UPDATE course_entries SET racer_id=? WHERE id=?",
                    (target_id, entry["id"]),
                )
                moved += 1
        conn.execute("DELETE FROM racers WHERE id=?", (source_id,))
    return moved, dropped


# ── Courses ───────────────────────────────────────────────────────────────────

def get_all_courses() -> list[sqlite3.Row]:
    """Return all courses ordered alphabetically."""
    with db() as conn:
        return conn.execute("SELECT * FROM courses ORDER BY name").fetchall()


def get_course(course_id: int) -> sqlite3.Row | None:
    """Return a single course by ID."""
    with db() as conn:
        return conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()


def upsert_course(
    name: str,
    time_limit_minutes: int,
    overtime_penalty: int,
    start_time: str | None = None,
    end_time: str | None = None,
    cutoff_time: str | None = None,
    use_si_start: bool = False,
    course_id: int | None = None,
) -> int:
    """Insert a new course or update an existing one. Returns the course ID."""
    with db() as conn:
        if course_id:
            conn.execute(
                """UPDATE courses
                   SET name=?, time_limit_minutes=?, overtime_penalty_per_minute=?,
                       start_time=?, end_time=?, cutoff_time=?, use_si_start=?, synced=0
                   WHERE id=?""",
                (name, time_limit_minutes, overtime_penalty,
                 start_time, end_time, cutoff_time, int(use_si_start), course_id),
            )
            return course_id
        cur = conn.execute(
            """INSERT INTO courses
               (name, time_limit_minutes, overtime_penalty_per_minute,
                start_time, end_time, cutoff_time, use_si_start)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, time_limit_minutes, overtime_penalty,
             start_time, end_time, cutoff_time, int(use_si_start)),
        )
        return cur.lastrowid


def delete_course(course_id: int):
    """Delete a course. All its control_points and course_entries are cascade-deleted."""
    with db() as conn:
        conn.execute("DELETE FROM courses WHERE id=?", (course_id,))


def get_controls_for_course(course_id: int) -> list[sqlite3.Row]:
    """Return all control points for a course, ordered by mandatory_order then si_code."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM control_points WHERE course_id=? ORDER BY mandatory_order, si_code",
            (course_id,),
        ).fetchall()


def upsert_control(
    course_id: int,
    si_code: int,
    name: str,
    points: int,
    is_mandatory: bool,
    mandatory_order: int | None,
    control_id: int | None = None,
    mandatory_miss_penalty: int = 0,
    circuit_group: str = "",
    circuit_miss_penalty: int = 0,
) -> int:
    """Insert a new control point or update an existing one. Returns the control ID."""
    with db() as conn:
        if control_id:
            conn.execute(
                """UPDATE control_points
                   SET si_code=?, name=?, points=?, is_mandatory=?, mandatory_order=?,
                       mandatory_miss_penalty=?, circuit_group=?, circuit_miss_penalty=?
                   WHERE id=?""",
                (si_code, name, points, int(is_mandatory), mandatory_order,
                 mandatory_miss_penalty, circuit_group, circuit_miss_penalty, control_id),
            )
            return control_id
        cur = conn.execute(
            """INSERT INTO control_points
               (course_id, si_code, name, points, is_mandatory, mandatory_order,
                mandatory_miss_penalty, circuit_group, circuit_miss_penalty)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (course_id, si_code, name, points, int(is_mandatory), mandatory_order,
             mandatory_miss_penalty, circuit_group, circuit_miss_penalty),
        )
        return cur.lastrowid


def delete_control(control_id: int):
    """Delete a single control point by ID."""
    with db() as conn:
        conn.execute("DELETE FROM control_points WHERE id=?", (control_id,))


# ── Course entries ─────────────────────────────────────────────────────────────

def get_entries_for_racer(racer_id: int) -> list[sqlite3.Row]:
    """Return all course entries for a racer, with course name joined in."""
    with db() as conn:
        return conn.execute(
            """SELECT ce.*, c.name as course_name
               FROM course_entries ce JOIN courses c ON ce.course_id=c.id
               WHERE ce.racer_id=? ORDER BY c.name""",
            (racer_id,),
        ).fetchall()


def get_entry_by_id(entry_id: int) -> sqlite3.Row | None:
    """Return a single course_entry row by primary key."""
    with db() as conn:
        return conn.execute(
            "SELECT ce.*, c.name as course_name FROM course_entries ce"
            " JOIN courses c ON ce.course_id=c.id WHERE ce.id=?",
            (entry_id,),
        ).fetchone()


def get_entry_by_chip(si_chip: int) -> sqlite3.Row | None:
    """Return the first entry with this chip number (legacy single-chip lookup)."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM course_entries WHERE si_chip=?", (si_chip,)
        ).fetchone()


def get_entries_by_chip(si_chip: int) -> list[sqlite3.Row]:
    """
    Return ALL entries assigned to this chip number (one per course).

    A team can have the same chip assigned across multiple courses if they're
    running all courses with one chip. Returns a list so all matching entries
    get scored when the chip is read.
    """
    with db() as conn:
        return conn.execute(
            "SELECT * FROM course_entries WHERE si_chip=?", (si_chip,)
        ).fetchall()


def set_entry_start_if_unset(entry_id: int, start_time: datetime):
    """
    Set the start time for an entry, but only if it hasn't been set yet.

    The WHERE start_time IS NULL guard prevents overwriting an earlier start time
    if the chip is read multiple times at the finish.
    """
    with db() as conn:
        conn.execute(
            "UPDATE course_entries SET start_time=?, synced=0 WHERE id=? AND start_time IS NULL",
            (start_time.isoformat(), entry_id),
        )


def update_finish_time_if_later(entry_id: int, finish_time: datetime):
    """
    Update the finish time only if the new time is LATER than the stored one.

    This handles re-reads: if a chip is read twice at the finish, we keep the
    later time as the authoritative finish (avoids accidentally improving a score
    by using an earlier intermediate read as the finish time).
    """
    with db() as conn:
        row = conn.execute(
            "SELECT finish_time FROM course_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if row and row["finish_time"]:
            existing = datetime.fromisoformat(row["finish_time"])
            if finish_time <= existing:
                return   # new time is not later — keep existing
        conn.execute(
            "UPDATE course_entries SET finish_time=?, synced=0 WHERE id=?",
            (finish_time.isoformat(), entry_id),
        )


def enroll_racer(racer_id: int, course_id: int, si_chip: int | None = None) -> int:
    """
    Enrol a racer in a course (create a course_entry row).

    If the racer is already enrolled (UNIQUE constraint fires), updates the chip
    number if one was provided, then returns the existing entry ID.
    Returns the entry ID.
    """
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO course_entries (racer_id, course_id, si_chip) VALUES (?, ?, ?)",
                (racer_id, course_id, si_chip),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Already enrolled — update chip if a new one was provided.
            if si_chip is not None:
                conn.execute(
                    "UPDATE course_entries SET si_chip=?, synced=0 WHERE racer_id=? AND course_id=?",
                    (si_chip, racer_id, course_id),
                )
            return conn.execute(
                "SELECT id FROM course_entries WHERE racer_id=? AND course_id=?",
                (racer_id, course_id),
            ).fetchone()[0]


def set_entry_chip(entry_id: int, si_chip: int | None):
    """Update the SI chip number assigned to an entry."""
    with db() as conn:
        conn.execute(
            "UPDATE course_entries SET si_chip=?, synced=0 WHERE id=?",
            (si_chip, entry_id),
        )


def set_entry_status(entry_id: int, status: str):
    """Set the status for a single course entry."""
    with db() as conn:
        conn.execute(
            "UPDATE course_entries SET status=?, synced=0 WHERE id=?", (status, entry_id)
        )


def set_racer_status(racer_id: int, status: str):
    """Set the status for ALL course entries belonging to a racer at once."""
    with db() as conn:
        conn.execute(
            "UPDATE course_entries SET status=?, synced=0 WHERE racer_id=?",
            (status, racer_id),
        )


def set_entry_times(
    entry_id: int,
    start_time: datetime | None,
    finish_time: datetime | None,
):
    """Manually override the start and/or finish times for an entry."""
    with db() as conn:
        conn.execute(
            "UPDATE course_entries SET start_time=?, finish_time=?, synced=0 WHERE id=?",
            (
                start_time.isoformat()  if start_time  else None,
                finish_time.isoformat() if finish_time else None,
                entry_id,
            ),
        )


def unenroll_racer(entry_id: int):
    """Remove a course entry (and cascade-delete its punches and adjustments)."""
    with db() as conn:
        conn.execute("DELETE FROM course_entries WHERE id=?", (entry_id,))


# ── Punches ───────────────────────────────────────────────────────────────────

def add_punches(
    entry_id: int,
    punches: list[tuple[int, datetime]],
    is_manual: bool = False,
):
    """
    Bulk-insert punch records for an entry.

    INSERT OR IGNORE prevents duplicates if the same chip is read multiple times —
    the UNIQUE-like behaviour comes from inserting matching (entry_id, si_code, punch_time)
    combinations only once.
    """
    with db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO punches (entry_id, si_code, punch_time, is_manual) VALUES (?,?,?,?)",
            [(entry_id, code, t.isoformat(), int(is_manual)) for code, t in punches],
        )


def get_punches_for_entry(entry_id: int) -> list[sqlite3.Row]:
    """Return all punches for an entry ordered chronologically."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM punches WHERE entry_id=? ORDER BY punch_time", (entry_id,)
        ).fetchall()


def delete_punch(punch_id: int):
    """Delete a single punch record (used in the Adjustments page)."""
    with db() as conn:
        conn.execute("DELETE FROM punches WHERE id=?", (punch_id,))


def move_chip_read(from_entry_id: int, to_entry_id: int) -> None:
    """
    Move all punch data, finish time, and start time from one course entry to another.

    Used to fix wrong-chip-on-wrong-course situations. The source entry is reset
    to SIGNED_UP with no punches or finish time; the target entry gains the data.
    """
    with db() as conn:
        src = conn.execute(
            "SELECT start_time, finish_time FROM course_entries WHERE id=?",
            (from_entry_id,),
        ).fetchone()
        punches = conn.execute(
            "SELECT si_code, punch_time, is_manual FROM punches WHERE entry_id=?",
            (from_entry_id,),
        ).fetchall()

        # Move punches to target entry
        conn.executemany(
            "INSERT INTO punches (entry_id, si_code, punch_time, is_manual) VALUES (?,?,?,?)",
            [(to_entry_id, p["si_code"], p["punch_time"], p["is_manual"]) for p in punches],
        )

        # Copy finish time to target (keep target's if it's already later)
        if src and src["finish_time"]:
            conn.execute(
                """UPDATE course_entries SET finish_time=?, synced=0
                   WHERE id=? AND (finish_time IS NULL OR finish_time < ?)""",
                (src["finish_time"], to_entry_id, src["finish_time"]),
            )
        # Copy start time to target only if target has none
        if src and src["start_time"]:
            conn.execute(
                "UPDATE course_entries SET start_time=? WHERE id=? AND start_time IS NULL",
                (src["start_time"], to_entry_id),
            )

        # Clear source entry — remove punches and reset timing/status
        conn.execute("DELETE FROM punches WHERE entry_id=?", (from_entry_id,))
        conn.execute(
            "UPDATE course_entries SET finish_time=NULL, status='SIGNED_UP', synced=0 WHERE id=?",
            (from_entry_id,),
        )


# ── Adjustment types ──────────────────────────────────────────────────────────
# Predefined bonus/penalty templates that can be quickly applied to any entry.

def get_all_adjustment_types() -> list[sqlite3.Row]:
    """Return all adjustment type templates ordered by category then name."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM adjustment_types ORDER BY category, name"
        ).fetchall()


def get_adjustment_type(type_id: int) -> sqlite3.Row | None:
    """Return a single adjustment type by ID, or None if not found."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM adjustment_types WHERE id=?", (type_id,)
        ).fetchone()


def upsert_adjustment_type(
    name: str,
    category: str,
    default_points: int,
    description: str = "",
    type_id: int | None = None,
) -> int:
    """
    Create or update a bonus/penalty type template.

    default_points is always a positive magnitude; the category ('bonus'/'penalty')
    determines whether it is added or subtracted when applied to an entry.
    Returns the type ID.
    """
    with db() as conn:
        if type_id:
            conn.execute(
                "UPDATE adjustment_types SET name=?, category=?, default_points=?, description=? WHERE id=?",
                (name, category, abs(default_points), description, type_id),
            )
            return type_id
        cur = conn.execute(
            "INSERT INTO adjustment_types (name, category, default_points, description) VALUES (?,?,?,?)",
            (name, category, abs(default_points), description),
        )
        return cur.lastrowid


def delete_adjustment_type(type_id: int):
    """
    Delete a bonus/penalty type template.

    Any entry_adjustments that referenced this type have their type_id nulled out
    first so they remain visible in the UI as manual adjustments rather than
    disappearing silently.
    """
    with db() as conn:
        conn.execute(
            "UPDATE entry_adjustments SET type_id=NULL, category='manual' WHERE type_id=?",
            (type_id,),
        )
        conn.execute("DELETE FROM adjustment_types WHERE id=?", (type_id,))


# ── Adjustments ───────────────────────────────────────────────────────────────

def get_adjustments_for_entry(entry_id: int) -> list[sqlite3.Row]:
    """
    Return all adjustments for an entry in insertion order.

    Joins with adjustment_types to include the template name for display,
    even though the applied description may have been overridden per-instance.
    """
    with db() as conn:
        return conn.execute(
            """SELECT ea.*, at.name as type_name
               FROM entry_adjustments ea
               LEFT JOIN adjustment_types at ON ea.type_id = at.id
               WHERE ea.entry_id=? ORDER BY ea.id""",
            (entry_id,),
        ).fetchall()


def add_adjustment(
    entry_id: int,
    description: str,
    points: int,
    type_id: int | None = None,
    category: str = "manual",
) -> int:
    """
    Add a bonus, penalty, or manual adjustment to an entry.

    type_id  — links to adjustment_types if a predefined template was used; None for free-form.
    category — 'bonus' (positive), 'penalty' (negative), or 'manual' (either sign, free-form).
               Used to split the leaderboard into separate Bonuses / Penalties / Manual columns.
    Returns the new adjustment ID.
    """
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO entry_adjustments (entry_id, description, points, type_id, category) VALUES (?,?,?,?,?)",
            (entry_id, description, points, type_id, category),
        )
        return cur.lastrowid


def delete_adjustment(adjustment_id: int):
    """Delete an adjustment by ID."""
    with db() as conn:
        conn.execute("DELETE FROM entry_adjustments WHERE id=?", (adjustment_id,))


# ── Bulk import ───────────────────────────────────────────────────────────────

def bulk_import_racers(rows: list[tuple[str, str]]) -> tuple[int, int]:
    """
    Import a list of (bib_number, name) rows from CSV paste.

    Returns (inserted, skipped) counts.
    Skips rows with empty names.
    """
    inserted = skipped = 0
    with db() as conn:
        for bib, name in rows:
            name = name.strip()
            bib  = bib.strip()
            if not name:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO racers (name, bib_number) VALUES (?, ?)",
                (name, bib),
            )
            inserted += 1
    return inserted, skipped


def bulk_import_controls(
    course_id: int,
    rows: list[tuple],   # (si_code[, points[, name[, is_mandatory[, order[, miss_penalty]]]]])
) -> tuple[int, int]:
    """
    Upsert controls by si_code within a course from CSV paste.

    If a control with the same si_code already exists in this course, its points
    and name are updated. Otherwise a new row is inserted.
    Returns (inserted, updated) counts.
    """
    inserted = updated = 0
    with db() as conn:
        for row in rows:
            si_code      = int(row[0])
            points       = int(row[1])  if len(row) > 1 else 0
            name         = str(row[2]).strip() if len(row) > 2 else ""
            is_mand      = bool(int(row[3])) if len(row) > 3 else False
            mand_ord     = int(row[4]) if len(row) > 4 and str(row[4]).strip() else None
            miss_penalty = int(row[5]) if len(row) > 5 and str(row[5]).strip() else 0

            existing = conn.execute(
                "SELECT id FROM control_points WHERE course_id=? AND si_code=?",
                (course_id, si_code),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE control_points
                       SET points=?, name=?, is_mandatory=?, mandatory_order=?, mandatory_miss_penalty=?
                       WHERE id=?""",
                    (points, name, int(is_mand), mand_ord, miss_penalty, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO control_points
                       (course_id, si_code, points, name, is_mandatory, mandatory_order, mandatory_miss_penalty)
                       VALUES (?,?,?,?,?,?,?)""",
                    (course_id, si_code, points, name, int(is_mand), mand_ord, miss_penalty),
                )
                inserted += 1
    return inserted, updated


def clear_all_results():
    """
    Reset all race results without deleting team/course setup.

    Deletes all punches and adjustments, and resets all course entries to the
    ACTIVE state with no times. Used at the start of a new race day or to
    re-run a practice session.
    """
    with db() as conn:
        conn.execute("DELETE FROM punches")
        conn.execute("DELETE FROM entry_adjustments")
        conn.execute(
            "UPDATE course_entries SET start_time=NULL, finish_time=NULL, status='ACTIVE', synced=0"
        )


def clear_all_racers():
    """
    Delete all racers and everything that depends on them.

    Cascades to course_entries, punches, and entry_adjustments via FK ON DELETE CASCADE.
    Courses and controls are not touched.
    """
    with db() as conn:
        conn.execute("DELETE FROM racers")


# ── Sync helpers ──────────────────────────────────────────────────────────────

def get_unsynced() -> dict:
    """
    Return all records where synced=0, grouped by table name.

    Called by SyncWorker before each push to the remote server.
    Returns a dict of {table_name: [row_dicts]} with only tables that have
    pending records (empty tables still appear as empty lists).
    """
    with db() as conn:
        return {
            "racers":      [dict(r) for r in conn.execute("SELECT * FROM racers WHERE synced=0").fetchall()],
            "courses":     [dict(r) for r in conn.execute("SELECT * FROM courses WHERE synced=0").fetchall()],
            "entries":     [dict(r) for r in conn.execute("SELECT * FROM course_entries WHERE synced=0").fetchall()],
            "punches":     [dict(r) for r in conn.execute("SELECT * FROM punches WHERE synced=0").fetchall()],
            "adjustments": [dict(r) for r in conn.execute("SELECT * FROM entry_adjustments WHERE synced=0").fetchall()],
        }


def mark_synced(table: str, ids: list[int]):
    """
    Mark specific rows as synced=1 after the remote server confirms receipt.

    Only marks the exact IDs the server acknowledged — any records added between
    the push and the acknowledgement remain at synced=0 for the next cycle.
    """
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with db() as conn:
        conn.execute(f"UPDATE {table} SET synced=1 WHERE id IN ({placeholders})", ids)


# ── Full result assembly ───────────────────────────────────────────────────────

def load_all_results() -> list:
    """
    Load every course entry from the DB and compute a ScoredResult for each one.

    This is the most expensive DB operation — it reads every table and scores
    every entry from scratch. It's called each time the results page loads and
    after every chip read. Because scoring is pure Python (no DB writes), the
    cost is just the reads, which SQLite handles efficiently.

    Returns a flat list of ScoredResult objects ready for the templates.
    """
    from datetime import datetime as dt
    from shared.models import Racer, Course, ControlPoint, CourseEntry, Punch, Adjustment, EntryStatus
    from shared.scoring import score_entry

    with db() as conn:
        racers_rows   = conn.execute(
            "SELECT r.*, c.name as class_name FROM racers r LEFT JOIN classes c ON r.class_id=c.id"
        ).fetchall()
        courses_rows  = conn.execute("SELECT * FROM courses").fetchall()
        controls_rows = conn.execute("SELECT * FROM control_points").fetchall()
        entries_rows  = conn.execute("SELECT * FROM course_entries").fetchall()
        punches_rows  = conn.execute("SELECT * FROM punches").fetchall()
        adj_rows      = conn.execute("SELECT * FROM entry_adjustments").fetchall()

    # Build in-memory lookups to avoid N+1 queries.
    racer_map = {
        r["id"]: Racer(
            id=r["id"], name=r["name"], bib_number=r["bib_number"],
            class_id=r["class_id"], class_name=r["class_name"] or "",
        )
        for r in racers_rows
    }

    # Group controls by course_id for fast lookup when building Course objects.
    controls_by_course: dict[int, list[ControlPoint]] = {}
    for c in controls_rows:
        ck = c.keys()
        controls_by_course.setdefault(c["course_id"], []).append(ControlPoint(
            id=c["id"], course_id=c["course_id"], si_code=c["si_code"],
            name=c["name"], points=c["points"],
            is_mandatory=bool(c["is_mandatory"]),
            mandatory_order=c["mandatory_order"],
            mandatory_miss_penalty=c["mandatory_miss_penalty"] if "mandatory_miss_penalty" in ck else 0,
            circuit_group=c["circuit_group"] if "circuit_group" in ck else "",
            circuit_miss_penalty=c["circuit_miss_penalty"] if "circuit_miss_penalty" in ck else 0,
        ))

    course_map = {
        c["id"]: Course(
            id=c["id"], name=c["name"],
            time_limit_minutes=c["time_limit_minutes"],
            overtime_penalty_per_minute=c["overtime_penalty_per_minute"],
            controls=controls_by_course.get(c["id"], []),
        )
        for c in courses_rows
    }

    # Group punches and adjustments by entry_id.
    punches_by_entry: dict[int, list[Punch]] = {}
    for p in punches_rows:
        punches_by_entry.setdefault(p["entry_id"], []).append(Punch(
            si_code=p["si_code"],
            punch_time=dt.fromisoformat(p["punch_time"]),
            is_manual=bool(p["is_manual"]),
        ))

    adj_by_entry: dict[int, list[Adjustment]] = {}
    for a in adj_rows:
        a_d = dict(a)   # convert Row → dict so .get() works for migrated columns
        adj_by_entry.setdefault(a_d["entry_id"], []).append(Adjustment(
            id=a_d["id"], entry_id=a_d["entry_id"],
            description=a_d["description"], points=a_d["points"],
            category=a_d.get("category", "manual"),
            type_id=a_d.get("type_id"),
        ))

    # Score each entry and collect results.
    results = []
    for e in entries_rows:
        racer  = racer_map.get(e["racer_id"])
        course = course_map.get(e["course_id"])
        # Skip orphaned entries (racer or course was deleted).
        if not racer or not course:
            continue
        entry = CourseEntry(
            id=e["id"], racer_id=e["racer_id"], course_id=e["course_id"],
            si_chip=e["si_chip"],
            start_time=dt.fromisoformat(e["start_time"])   if e["start_time"]  else None,
            finish_time=dt.fromisoformat(e["finish_time"]) if e["finish_time"] else None,
            status=EntryStatus(e["status"]),
            punches=punches_by_entry.get(e["id"], []),
            adjustments=adj_by_entry.get(e["id"], []),
        )
        results.append(score_entry(entry, course, racer))

    return results
