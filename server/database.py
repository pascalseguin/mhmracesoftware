"""Server-side SQLite database. Receives synced data from clients."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "mhm_server.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY, name TEXT, time_limit_minutes INTEGER,
            overtime_penalty_per_minute INTEGER, foodbank_bonus_points INTEGER
        );
        CREATE TABLE IF NOT EXISTS control_points (
            id INTEGER PRIMARY KEY, course_id INTEGER, si_code INTEGER,
            name TEXT, points INTEGER, is_mandatory INTEGER, mandatory_order INTEGER
        );
        CREATE TABLE IF NOT EXISTS racers (
            id INTEGER PRIMARY KEY, name TEXT, si_chip INTEGER UNIQUE,
            team TEXT, phone TEXT, emergency_contact TEXT, foodbank_donated INTEGER
        );
        CREATE TABLE IF NOT EXISTS course_entries (
            id INTEGER PRIMARY KEY, racer_id INTEGER, course_id INTEGER,
            start_time TEXT, finish_time TEXT, status TEXT,
            manual_adjustment_points INTEGER, manual_adjustment_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS punches (
            id INTEGER PRIMARY KEY, entry_id INTEGER,
            si_code INTEGER, punch_time TEXT, is_manual INTEGER
        );
        """)


def apply_sync(payload: dict) -> dict:
    """Upsert all synced records and return acked IDs per table."""
    acked: dict[str, list[int]] = {}

    with db() as conn:
        for r in payload.get("racers", []):
            conn.execute("""
                INSERT INTO racers (id,name,si_chip,team,phone,emergency_contact,foodbank_donated)
                VALUES (:id,:name,:si_chip,:team,:phone,:emergency_contact,:foodbank_donated)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, team=excluded.team, phone=excluded.phone,
                  emergency_contact=excluded.emergency_contact,
                  foodbank_donated=excluded.foodbank_donated""", dict(r))
            acked.setdefault("racers", []).append(r["id"])

        for c in payload.get("courses", []):
            conn.execute("""
                INSERT INTO courses (id,name,time_limit_minutes,overtime_penalty_per_minute,foodbank_bonus_points)
                VALUES (:id,:name,:time_limit_minutes,:overtime_penalty_per_minute,:foodbank_bonus_points)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, time_limit_minutes=excluded.time_limit_minutes,
                  overtime_penalty_per_minute=excluded.overtime_penalty_per_minute,
                  foodbank_bonus_points=excluded.foodbank_bonus_points""", dict(c))
            acked.setdefault("courses", []).append(c["id"])

        for e in payload.get("entries", []):
            conn.execute("""
                INSERT INTO course_entries
                  (id,racer_id,course_id,start_time,finish_time,status,
                   manual_adjustment_points,manual_adjustment_reason)
                VALUES (:id,:racer_id,:course_id,:start_time,:finish_time,:status,
                        :manual_adjustment_points,:manual_adjustment_reason)
                ON CONFLICT(id) DO UPDATE SET
                  start_time=excluded.start_time, finish_time=excluded.finish_time,
                  status=excluded.status,
                  manual_adjustment_points=excluded.manual_adjustment_points,
                  manual_adjustment_reason=excluded.manual_adjustment_reason""", dict(e))
            acked.setdefault("entries", []).append(e["id"])

        for p in payload.get("punches", []):
            conn.execute("""
                INSERT OR IGNORE INTO punches (id,entry_id,si_code,punch_time,is_manual)
                VALUES (:id,:entry_id,:si_code,:punch_time,:is_manual)""", dict(p))
            acked.setdefault("punches", []).append(p["id"])

    return acked


def get_public_results() -> list[dict]:
    from datetime import datetime as dt
    from shared.models import (
        Racer, Course, ControlPoint, CourseEntry, Punch, EntryStatus
    )
    from shared.scoring import score_entry, rank_results

    with db() as conn:
        racers_rows = conn.execute("SELECT * FROM racers").fetchall()
        courses_rows = conn.execute("SELECT * FROM courses").fetchall()
        controls_rows = conn.execute("SELECT * FROM control_points").fetchall()
        entries_rows = conn.execute("SELECT * FROM course_entries").fetchall()
        punches_rows = conn.execute("SELECT * FROM punches").fetchall()

    racer_map = {r["id"]: Racer(
        id=r["id"], name=r["name"], si_chip=r["si_chip"],
        team=r["team"] or "", phone=r["phone"] or "",
        emergency_contact=r["emergency_contact"] or "",
        foodbank_donated=bool(r["foodbank_donated"]),
    ) for r in racers_rows}

    controls_by_course: dict[int, list[ControlPoint]] = {}
    for c in controls_rows:
        controls_by_course.setdefault(c["course_id"], []).append(ControlPoint(
            id=c["id"], course_id=c["course_id"], si_code=c["si_code"],
            name=c["name"] or "", points=c["points"],
            is_mandatory=bool(c["is_mandatory"]),
            mandatory_order=c["mandatory_order"],
        ))

    course_map = {c["id"]: Course(
        id=c["id"], name=c["name"],
        time_limit_minutes=c["time_limit_minutes"],
        overtime_penalty_per_minute=c["overtime_penalty_per_minute"],
        foodbank_bonus_points=c["foodbank_bonus_points"],
        controls=controls_by_course.get(c["id"], []),
    ) for c in courses_rows}

    punches_by_entry: dict[int, list[Punch]] = {}
    for p in punches_rows:
        punches_by_entry.setdefault(p["entry_id"], []).append(Punch(
            si_code=p["si_code"],
            punch_time=dt.fromisoformat(p["punch_time"]),
            is_manual=bool(p["is_manual"]),
        ))

    all_results = []
    for e in entries_rows:
        racer = racer_map.get(e["racer_id"])
        course = course_map.get(e["course_id"])
        if not racer or not course:
            continue
        entry = CourseEntry(
            id=e["id"], racer_id=e["racer_id"], course_id=e["course_id"],
            start_time=dt.fromisoformat(e["start_time"]) if e["start_time"] else None,
            finish_time=dt.fromisoformat(e["finish_time"]) if e["finish_time"] else None,
            status=EntryStatus(e["status"]),
            punches=punches_by_entry.get(e["id"], []),
            manual_adjustment_points=e["manual_adjustment_points"],
            manual_adjustment_reason=e["manual_adjustment_reason"],
        )
        all_results.append(score_entry(entry, course, racer))

    return all_results
