"""
MHM Race Client — FastAPI web application.

Architecture overview:
  - Single-process Python app bundled with PyInstaller for Windows deployment.
  - FastAPI + Uvicorn serve the web UI on localhost:8000.
  - SQLite (WAL mode) stores all race data locally in mhm.db.
  - Two background daemon threads run alongside the web server:
      SIReader  — talks to the SportIdent chip readout station via USB/serial.
      SyncWorker — periodically POSTs unsynced results to the remote results server.
  - Jinja2 renders HTML templates; no JavaScript framework.
  - Auth is session-cookie-based (no JWT). All non-public routes require login.
  - Config is loaded from config.json on startup and mutated in memory during the
    session; routes that change settings call cfg_mod.save() to persist them.

Request flow for a chip read:
  1. SIReader background thread: reads chip → calls on_chip_read(si_chip, punches, finish_time)
  2. on_chip_read: applies CN offset, writes punches/times to DB, scores the entry,
     prints a receipt, and logs events to the dashboard feed.
  3. /results (browser) shows updated scores on next page load.

MeOS import flow (two-step preview/confirm):
  1. POST /import/meos/preview — parse the .meosxml file, store parsed data in
     _meos_pending (keyed by a random token), render the preview template.
  2. POST /import/meos/confirm — read selected IDs from the form, create courses/
     controls/teams/enrollments in the DB.
"""
import subprocess
import sys
import os

# Prevent console windows from flashing when spawning subprocesses from a GUI app.
# CREATE_NO_WINDOW is Windows-only; the getattr fallback keeps the code portable.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Ensure the repo root is on sys.path when running from the client/ subdirectory.
# PyInstaller bundles everything so this is a no-op when running from the .exe,
# but it's needed for `python -m client.app` during development.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import collections
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import jinja2
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import client.database as db
from client import config as cfg_mod
from client.meos_import import parse_meos_xml
from client.printer import print_receipt, build_receipt_lines
from client.si_reader import SIReader, list_ports
from client.sync import SyncWorker
from client.utils import resource_path
from shared.scoring import score_entry, rank_results

from client.utils import data_path as _data_path


# ── Logging ───────────────────────────────────────────────────────────────────
# Dual-handler: StreamHandler for the terminal (visible in dev/debug) and
# FileHandler for mhm.log (persisted across restarts, readable via /api/si/log).
# The log file sits in the writable data directory (next to the .exe in prod,
# next to the repo in dev) so it survives app updates without being lost.

_LOG_PATH = _data_path("mhm.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_LOG_PATH), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Global state ──────────────────────────────────────────────────────────────

# Ring buffer of the 50 most recent SI reader events.
# Shown live on the dashboard without requiring a page reload.
# appendleft() keeps newest first so the template can iterate straight through.
_si_events: collections.deque = collections.deque(maxlen=50)

# Active configuration dict — loaded from config.json (or defaults) at startup.
# Mutated in memory by save_settings; persisted via cfg_mod.save().
# Injected into every Jinja2 template via _jinja_env.globals so routes don't
# need to pass it explicitly (though some do so for clarity).
CFG = cfg_mod.load()

# The live SIReader daemon thread — None when no port is connected.
_si_reader: Optional[SIReader] = None

# The COM port the currently-running SIReader is using (e.g. "COM4").
# Stored separately so the dashboard can show the active port without
# needing to reach into the thread object.
_si_port_active: Optional[str] = None

# The live SyncWorker daemon thread — None when server_url/api_key aren't set.
_sync_worker: Optional[SyncWorker] = None

# In-memory staging area for MeOS preview→confirm two-step import.
# Keys are random hex tokens stored in the user's session between the two requests.
# Capped at 10 entries to prevent unbounded growth in long-running sessions.
_meos_pending: dict = {}
_teams_pending: dict = {}
_chip_import_pending: dict = {}

# SI chip reads where no matching entry was found.
# Keyed by si_chip (int) so a second read of the same unknown chip overwrites
# the first rather than accumulating duplicates.
# Each value is {"si_chip", "punches", "finish_time", "chip_start_time", "time"}.
_unknown_reads: dict[int, dict] = {}

# SI chip reads that matched an entry and are awaiting race-director review.
# Keyed by si_chip (int). Value: {si_chip, entries, punches, finish_time,
# chip_start_time, received_at}. Nothing is written to the DB until the
# race director accepts the read via /chip-review/<chip>/save.
_pending_reads: dict[int, dict] = {}


# ── Jinja2 template engine ────────────────────────────────────────────────────
# FileSystemLoader reads templates from the bundled templates/ directory.
# resource_path() returns the correct path whether running from source or
# from a PyInstaller .exe (where templates are extracted to a temp directory).
# cache_size=0 disables template caching so edits during development take effect
# immediately without restarting the server.

import re as _re

def _natural_sort_key(s) -> list:
    """Split a string into alternating text/integer chunks for natural ordering.

    "LR02" → ["lr", 2, ""]   so LR02 sorts after LR01 and before LR10.
    "4"    → ["", 4, ""]     so 4 sorts before 39.
    None/empty → ["", 0, ""] sorts to the front.
    """
    return [int(c) if c.isdigit() else c.lower()
            for c in _re.split(r'(\d+)', str(s or ''))]


def _natsort_filter(lst, attr: str | None = None):
    """Jinja2 filter: naturally sort a list of dicts/objects by an attribute."""
    def key(x):
        try:
            val = x[attr] if attr else x
        except (TypeError, KeyError):
            val = getattr(x, attr, '') if attr else x
        return _natural_sort_key(val)
    return sorted(lst, key=key)


_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(resource_path("templates"))),
    autoescape=jinja2.select_autoescape(["html"]),
    cache_size=0,
)
templates = Jinja2Templates(env=_jinja_env)
_jinja_env.filters['natsort'] = _natsort_filter

# Human-readable labels and badge colours for every EntryStatus value.
# Registered as Jinja2 globals so templates don't need to pass them explicitly.
_STATUS_LABELS: dict[str, str] = {
    'SIGNED_UP':  'Signed Up',
    'REGISTERED': 'Registered',
    'ON_COURSE':  'On Course',
    'FINISHED':   'Finished',
    'OK':         'Finished',
    'ACTIVE':     'On Course',
    'DNS':        'DNS',
    'DNF':        'DNF',
    'DSQ':        'DSQ',
}
_STATUS_COLORS: dict[str, str] = {
    'SIGNED_UP':  '#6c757d',   # gray
    'REGISTERED': '#0d6efd',   # blue
    'ON_COURSE':  '#fd7e14',   # orange
    'FINISHED':   '#198754',   # green
    'OK':         '#198754',   # green
    'ACTIVE':     '#fd7e14',   # orange  (legacy = on course)
    'DNS':        '#adb5bd',   # light gray
    'DNF':        '#e76f00',   # amber
    'DSQ':        '#dc3545',   # red
}
_jinja_env.globals['STATUS_LABELS'] = _STATUS_LABELS
_jinja_env.globals['STATUS_COLORS'] = _STATUS_COLORS
# All valid status values for dropdown menus.
_ALL_STATUSES = ['SIGNED_UP', 'REGISTERED', 'ON_COURSE', 'FINISHED', 'DNF', 'DNS', 'DSQ']
_jinja_env.globals['ALL_STATUSES'] = _ALL_STATUSES

# Make CFG available in every template as `cfg` without explicit context passing.
# Any route that calls templates.TemplateResponse() gets cfg for free.
_jinja_env.globals["cfg"] = CFG


# ── Public paths (no login required) ─────────────────────────────────────────
# These prefixes are checked in auth_middleware. Anything else redirects to /login.
# /results and /class-results are public so spectators can view live results
# without logging in. /api/si/status is public so external displays can poll
# the reader status without a session cookie.

_PUBLIC_PREFIXES = (
    "/results",
    "/class-results",
    "/export",        # export hub + /export/results
    "/login",
    "/logout",
    "/static",
    "/api/results",
    "/api/si/status",
)

# Paths that start with a public prefix but still require login.
# auth_middleware checks this set BEFORE applying the prefix shortcut.
_PROTECTED_OVERRIDES = {
    "/results/manage",
    "/results/manage/bulk",
}


# ── SI event helpers ──────────────────────────────────────────────────────────

def _si_event(msg: str):
    """
    Append a timestamped status message to the dashboard event ring buffer.

    Called both from on_chip_read (in the main thread, via FastAPI route invocations)
    and from the SIReader background thread via the on_event callback.
    deque is thread-safe for appends so no lock is needed.
    """
    _si_events.appendleft({"time": datetime.now().strftime("%H:%M:%S"), "msg": msg})


# ── Chip read preview helper ─────────────────────────────────────────────────

def _build_chip_preview(
    entry_row,
    punches: list[tuple[int, datetime]],
    finish_time: datetime | None,
    chip_start_time: datetime | None,
    extra_si_codes: list[int] | None = None,
):
    """
    Build an in-memory ScoredResult for a chip read without touching the database.

    Used to render the review page before the race director commits the read.
    Returns (ScoredResult, Course) or None if required rows are missing.
    """
    from shared.models import (
        Course, ControlPoint, CourseEntry, Punch, Racer, EntryStatus
    )

    course_row  = db.get_course(entry_row["course_id"])
    racer_row   = db.get_racer(entry_row["racer_id"])
    ctrl_rows   = db.get_controls_for_course(entry_row["course_id"])
    if not course_row or not racer_row:
        return None

    controls = []
    for c in ctrl_rows:
        ck = c.keys()
        controls.append(ControlPoint(
            id=c["id"], course_id=c["course_id"], si_code=c["si_code"],
            name=c["name"] or "", points=c["points"],
            is_mandatory=bool(c["is_mandatory"]),
            mandatory_order=c["mandatory_order"],
            mandatory_miss_penalty=c["mandatory_miss_penalty"] if "mandatory_miss_penalty" in ck else 0,
            circuit_group=c["circuit_group"] if "circuit_group" in ck else "",
            circuit_miss_penalty=c["circuit_miss_penalty"] if "circuit_miss_penalty" in ck else 0,
        ))

    course = Course(
        id=course_row["id"], name=course_row["name"],
        time_limit_minutes=course_row["time_limit_minutes"],
        overtime_penalty_per_minute=course_row["overtime_penalty_per_minute"],
        controls=controls,
    )

    use_si = bool(course_row["use_si_start"])
    if use_si and chip_start_time:
        start_time = chip_start_time
    elif course_row["start_time"]:
        start_time = datetime.fromisoformat(course_row["start_time"])
    else:
        start_time = None

    racer = Racer(
        id=racer_row["id"], name=racer_row["name"],
        bib_number=racer_row["bib_number"] or "",
        class_id=racer_row["class_id"],
        class_name=racer_row["class_name"] if "class_name" in racer_row.keys() else "",
    )

    punch_list = [Punch(si_code=code, punch_time=t) for code, t in punches]
    if extra_si_codes:
        pt = finish_time or datetime.now()
        for si_code in extra_si_codes:
            punch_list.append(Punch(si_code=si_code, punch_time=pt, is_manual=True))

    entry = CourseEntry(
        id=entry_row["id"], racer_id=entry_row["racer_id"],
        course_id=entry_row["course_id"], si_chip=entry_row["si_chip"],
        start_time=start_time, finish_time=finish_time,
        status=EntryStatus.ON_COURSE, punches=punch_list, adjustments=[],
    )

    return score_entry(entry, course, racer), course


# ── SI chip read callback ─────────────────────────────────────────────────────

def on_chip_read(
    si_chip: int,
    punches: list[tuple[int, datetime]],
    finish_time: datetime | None,
    chip_start_time: datetime | None = None,
):
    """
    Process a completed chip read from the SI station.

    Called by SIReader._process() on the background thread after a chip is fully
    downloaded. All DB writes here are safe because SQLite WAL mode allows
    concurrent reads from the FastAPI request handlers while we write.

    Steps:
      1. Look up all entries assigned to this chip number.
      2. If a "selected course" filter is active on the dashboard, narrow to that course.
      3. Apply the CN offset to correct for the SportIdent library's 8-bit truncation
         (stations programmed in the 256–511 range return code−256 without the offset).
      4. Write punches, start time (if not already set), and finish time to the DB.
      5. Mark the entry status as "OK".
      6. Re-score all results for the course, compute rank, and print a receipt.
    """
    _si_event(f"Chip {si_chip} read — {len(punches)} punches, finish={finish_time}")

    # Find all course entries assigned to this chip.
    entries = db.get_entries_by_chip(si_chip)
    if not entries:
        msg = f"Chip {si_chip} not assigned to any entry — queued for manual assignment"
        log.warning(msg)
        _si_event(f"WARNING: {msg}")
        _unknown_reads[si_chip] = {
            "si_chip": si_chip,
            "punches": punches,
            "finish_time": finish_time,
            "chip_start_time": chip_start_time,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        return

    # If the race director has selected a specific active course on the dashboard,
    # only process the entry for that course. This prevents accidental double-scoring
    # when a chip is enrolled in multiple courses.
    selected_course_id = CFG.get("selected_course_id")
    if selected_course_id:
        entries = [e for e in entries if e["course_id"] == int(selected_course_id)]
        if not entries:
            log.warning(
                "Chip %s has no entry in selected course %s", si_chip, selected_course_id
            )
            return

    # Apply the Code Number offset to correct 8-bit truncation by the sportident library.
    # The library reads only 8 bits of the 12-bit CN field, so stations numbered
    # 256–511 come back as (actual − 256). The default offset of 256 corrects that.
    #
    # Mixed-range fix: if a punch's raw code directly matches a configured control
    # (station is in the 1–255 range), use it as-is. Only apply the offset when
    # the raw code does NOT match — which is the case for truncated 256–511 stations.
    # This lets a single race use controls in both ranges simultaneously.
    cn_offset = int(
        CFG.get("si_cn_offset") if CFG.get("si_cn_offset") is not None else 256
    )
    if cn_offset and punches:
        # Collect every SI code that's configured as a control across all of this
        # chip's courses so we can do per-punch raw-vs-offset matching.
        known_codes: set[int] = set()
        for entry_row in entries:
            for ctrl in db.get_controls_for_course(entry_row["course_id"]):
                known_codes.add(ctrl["si_code"])

        corrected: list[tuple[int, datetime]] = []
        for code, t in punches:
            if known_codes and code in known_codes:
                # Raw code matches a configured control → station is in 1–255 range,
                # no offset needed.
                corrected.append((code, t))
            else:
                # Raw code doesn't match → assume it's a truncated 256–511 station,
                # apply the offset. Unmatched punches also land here and will show
                # as unmatched in the Adjustments page as before.
                corrected.append((code + cn_offset, t))
        punches = corrected

    # Queue the read for race-director review instead of writing to DB immediately.
    # The operator visits /chip-review/<si_chip> to preview scoring, make quick
    # adjustments, then accept or discard the read.
    racer_names = []
    course_names = []
    for entry_row in entries:
        rr = db.get_racer(entry_row["racer_id"])
        cr = db.get_course(entry_row["course_id"])
        if rr:
            racer_names.append(rr["name"])
        if cr:
            course_names.append(cr["name"])

    _pending_reads[si_chip] = {
        "si_chip":        si_chip,
        "entries":        entries,
        "punches":        punches,
        "finish_time":    finish_time,
        "chip_start_time": chip_start_time,
        "received_at":    datetime.now().strftime("%H:%M:%S"),
        "racer_names":    racer_names,
        "course_names":   course_names,
    }
    _si_event(
        f"Chip {si_chip} PENDING REVIEW — "
        + ", ".join(f"{r} / {c}" for r, c in zip(racer_names, course_names))
        + f" — open /chip-review/{si_chip}"
    )


# ── SI reader lifecycle helpers ───────────────────────────────────────────────

def _start_si_reader(port: str):
    """
    Stop any existing SIReader thread and start a new one on the given port.

    join(timeout=2) gives the old thread 2 seconds to finish its current poll
    cycle before we proceed. The new thread is a daemon so it dies with the process.
    The active port is persisted to config.json so the app auto-reconnects on restart.
    """
    global _si_reader, _si_port_active
    if _si_reader and _si_reader.is_alive():
        _si_reader.stop()
        _si_reader.join(timeout=2)

    _si_reader = SIReader(port, on_chip_read, on_event=_si_event)
    _si_reader.start()
    _si_port_active = port

    # Persist the chosen port so the reader auto-starts on the next app launch.
    CFG["si_port"] = port
    cfg_mod.save(CFG)
    log.info("SI reader started on %s", port)


def _stop_si_reader():
    """
    Signal the SIReader thread to exit and clear the active-port state.

    Blanks out si_port in config.json so the reader does NOT auto-start on restart.
    """
    global _si_reader, _si_port_active
    if _si_reader and _si_reader.is_alive():
        _si_reader.stop()
        _si_reader.join(timeout=2)
    _si_reader = None
    _si_port_active = None

    CFG["si_port"] = ""
    cfg_mod.save(CFG)
    log.info("SI reader stopped")


def _win32print_available() -> bool:
    """
    Return True — printing uses ctypes + winspool.drv which are always present
    on Windows. Kept as a function so the dashboard template call is unchanged.
    """
    return True


def list_windows_printers() -> list[str]:
    """
    Return installed Windows printer names, trying three methods in order.

    1. win32print (pywin32) — most reliable; direct Win32 API call.
    2. PowerShell Get-Printer — works on Windows 10/11 when WMIC is restricted.
    3. WMIC — legacy fallback that still works on most installs.

    Returns a sorted list of printer name strings, or an empty list if all
    three methods fail (e.g. non-Windows environment, no printers installed).
    """
    # Method 1: pywin32 (pip install pywin32) — direct Win32 EnumPrinters call.
    try:
        import win32print  # type: ignore
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        # EnumPrinters returns tuples; index 2 is the printer name.
        names = sorted(p[2] for p in printers if p[2])
        if names:
            return names
    except ImportError:
        pass   # pywin32 not installed — try next method
    except Exception:
        pass

    # Method 2: PowerShell Get-Printer — more reliable than WMIC on Windows 10/11.
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Printer | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=8,
            creationflags=_NO_WINDOW,
        )
        names = sorted(ln.strip() for ln in result.stdout.splitlines() if ln.strip())
        if names:
            return names
    except Exception:
        pass

    # Method 3: WMIC — deprecated in Windows 11 but still present on most installs.
    try:
        result = subprocess.run(
            ["wmic", "printer", "get", "name"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW,
        )
        names = sorted(
            ln.strip()
            for ln in result.stdout.splitlines()
            if ln.strip() and ln.strip() != "Name"
        )
        if names:
            return names
    except Exception:
        pass

    return []


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager — runs startup logic before yield,
    shutdown logic after yield.

    Startup:
      - Initialise the SQLite database (create tables, run migrations, seed
        default user if the users table is empty).
      - Auto-start the SI reader on the last-used port (stored in config.json).
      - Auto-start the sync worker if server_url and api_key are configured.

    Shutdown:
      - Signal both background threads to exit cleanly.
      - We don't join() them here because FastAPI's shutdown can be abrupt;
        the threads are daemons so the OS reclaims them if they don't exit fast enough.
    """
    global _sync_worker

    db.init_db()

    # Auto-reconnect to the SI station on the port from the last session.
    si_port = CFG.get("si_port", "")
    if si_port:
        _start_si_reader(si_port)

    # Auto-start syncing if a remote server is configured.
    server_url = CFG.get("server_url", "")
    api_key    = CFG.get("api_key", "")
    if server_url and api_key:
        _sync_worker = SyncWorker(
            server_url, api_key,
            interval_seconds=CFG.get("sync_interval_seconds", 30),
        )
        _sync_worker.start()

    yield  # app runs here

    if _si_reader:
        _si_reader.stop()
    if _sync_worker:
        _sync_worker.stop()


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="MHM Race Client", lifespan=lifespan)

# Serve CSS/JS/images from the bundled static/ directory.
# resource_path() resolves to _MEIPASS in a PyInstaller .exe, or the repo root
# in development — see client/utils.py for the full explanation.
app.mount(
    "/static",
    StaticFiles(directory=str(resource_path("static"))),
    name="static",
)


# ── Middleware ────────────────────────────────────────────────────────────────
# IMPORTANT — ordering matters in Starlette/FastAPI middleware:
#   add_middleware() wraps the app in the reverse of declaration order,
#   so the LAST add_middleware() call becomes the OUTERMOST (first to run).
#
# We need SessionMiddleware to be outermost so it parses the session cookie
# BEFORE auth_middleware tries to read request.session["user"].
#
# Correct order:
#   1. Define auth_middleware with @app.middleware("http")  ← runs second
#   2. Call app.add_middleware(SessionMiddleware)           ← runs first (outermost)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Redirect unauthenticated requests to /login, except for public prefixes.

    Checks request.session["user"] which is populated by SessionMiddleware
    (outermost layer). Public prefixes include the results display, login/logout,
    static files, and the SI status API so external displays can poll without auth.
    """
    path = request.url.path
    if path not in _PROTECTED_OVERRIDES and any(
        path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES
    ):
        return await call_next(request)
    if not request.session.get("user"):
        return RedirectResponse(f"/login?next={path}", status_code=302)
    return await call_next(request)


# SessionMiddleware must be added AFTER (i.e. wrap around) auth_middleware so it
# runs first and populates request.session before auth_middleware reads it.
# secret_key is randomised per process — sessions don't survive restarts,
# which is acceptable for a single-event race management app.
app.add_middleware(
    SessionMiddleware,
    secret_key=secrets.token_hex(32),
    session_cookie="mhm_session",
    max_age=86400 * 7,   # session lasts 7 days (survives closing the browser)
)


# ── Login / logout ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = ""):
    """Render the login form. `next` is passed through so redirect-after-login works."""
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    """
    Verify credentials (bcrypt hash comparison via db.verify_user) and set session.

    `next` must start with "/" to prevent open-redirect attacks — an attacker
    could craft ?next=https://evil.com to redirect after a successful login.
    """
    if db.verify_user(username, password):
        request.session["user"] = username
        # Validate next to prevent open-redirect: only allow local paths.
        return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next, "error": "Invalid username or password."},
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, msg: str = ""):
    """
    Main dashboard — shows summary counts, SI connection controls, and the live
    SI event feed. msg is a flash message from a preceding redirect (e.g. after
    connecting a new SI port).
    """
    racers      = db.get_all_racers()
    courses     = db.get_all_courses()
    all_results = db.load_all_results()
    entries     = db.get_all_entries_overview()
    return templates.TemplateResponse(request, "dashboard.html", {
        "racers":          racers,
        "courses":         courses,
        "total_results":   len(all_results),
        "msg":             msg,
        "cfg":             CFG,
        "si_ports":        list_ports(),
        "si_port_active":  _si_port_active,
        "printers":        list_windows_printers(),
        "escpos_available": _win32print_available(),
        "unknown_reads":   list(_unknown_reads.values()),
        "entries":         entries,
    })


# ── SI reader controls ────────────────────────────────────────────────────────

@app.post("/si/connect")
async def si_connect(port: str = Form(...)):
    """Start the SI reader on the selected COM port and persist the choice."""
    if not port:
        return RedirectResponse("/?msg=No+port+selected", status_code=303)
    _start_si_reader(port)
    return RedirectResponse(f"/?msg=Connected+to+{port}", status_code=303)


@app.post("/si/disconnect")
async def si_disconnect():
    """Stop the SI reader and clear the persisted port so it won't auto-restart."""
    _stop_si_reader()
    return RedirectResponse("/?msg=SI+reader+disconnected", status_code=303)


@app.post("/si/course")
async def si_set_course(course_id: str = Form("")):
    """
    Set (or clear) the active course filter for the SI reader.

    When set, only entries enrolled in this course are processed on chip read.
    Useful at a multi-course event where finish lines share a single SI station.
    Empty string = no filter (all courses).
    """
    CFG["selected_course_id"] = int(course_id) if course_id else None
    cfg_mod.save(CFG)
    return RedirectResponse("/", status_code=303)


# ── Printer controls ─────────────────────────────────────────────────────────

@app.post("/printer/set")
async def set_printer(
    printer_name:  str = Form(""),
    print_on_read: str = Form(""),   # checkbox — empty string when unchecked
):
    """
    Save the receipt printer selection and auto-print toggle from the dashboard.

    printer_name — exact Windows printer name (blank = disabled / save to file).
    print_on_read — "1" when checked, "" when unchecked (HTML checkbox behaviour).
    Both values are persisted to config.json immediately.
    """
    CFG["printer_name"]  = printer_name
    CFG["print_on_read"] = bool(print_on_read)   # "" → False, "1" → True
    cfg_mod.save(CFG)
    status = "enabled" if CFG["print_on_read"] and printer_name else "disabled"
    return RedirectResponse(f"/?msg=Printer+settings+saved+({status})", status_code=303)


@app.post("/api/test-print")
async def api_test_print():
    """
    Send a short test page to the configured ESC/POS thermal printer.

    Returns JSON {ok, msg|error} so the dashboard can show inline feedback
    without a page reload. The test page prints the race name, timestamp,
    and a "Printer OK" confirmation line, then cuts the paper.
    """
    printer_name = CFG.get("printer_name", "")
    if not printer_name:
        return JSONResponse({"ok": False, "error": "No printer configured — select one and save first."})

    try:
        from client.printer import _win32_print
        W    = 42
        name = CFG.get("race_name", "MHM RACE").upper()
        lines = [
            "=" * W,
            name.center(W),
            "TEST PRINT".center(W),
            "=" * W,
            datetime.now().strftime("%Y-%m-%d  %H:%M:%S").center(W),
            "",
            "Printer connection OK".center(W),
            "=" * W,
            "",
            "",
        ]
        _win32_print(printer_name, "\n".join(lines) + "\n")
        log.info("Test print sent to %s", printer_name)
        return JSONResponse({"ok": True, "msg": f"Sent to {printer_name}"})

    except Exception as exc:
        log.warning("Test print failed: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)})


# ── SI API endpoints ──────────────────────────────────────────────────────────

@app.get("/api/si/status")
async def si_status():
    """
    Public JSON endpoint — returns reader connection state.

    Used by external scoreboards or status displays that don't have a session cookie.
    Listed in _PUBLIC_PREFIXES so it bypasses auth_middleware.
    """
    return JSONResponse({
        "connected":       _si_port_active is not None,
        "port":            _si_port_active,
        "available_ports": list_ports(),
    })


@app.get("/api/si/events")
async def si_events():
    """
    Return the recent SI event ring buffer as JSON.

    Polled every few seconds by the dashboard's JavaScript for the live feed.
    Returns list newest-first (deque was built with appendleft).
    """
    return JSONResponse(list(_si_events))


@app.get("/api/pending-reads")
async def api_pending_reads():
    """Return summary of chip reads awaiting race-director review."""
    return JSONResponse([
        {
            "si_chip":     v["si_chip"],
            "racer_names": v["racer_names"],
            "course_names": v["course_names"],
            "received_at": v["received_at"],
            "punch_count": len(v["punches"]),
        }
        for v in _pending_reads.values()
    ])


@app.get("/chip-review/{si_chip}", response_class=HTMLResponse)
async def chip_review_get(request: Request, si_chip: int):
    """Preview page for a pending chip read — shown before any DB writes."""
    pending = _pending_reads.get(si_chip)
    if not pending:
        return RedirectResponse("/?msg=No+pending+read+for+that+chip", status_code=303)

    previews = []
    for entry_row in pending["entries"]:
        result_pair = _build_chip_preview(
            entry_row, pending["punches"],
            pending["finish_time"], pending["chip_start_time"],
        )
        if result_pair is None:
            continue
        result, course = result_pair
        controls_visited_codes = {c.si_code for c in result.controls_visited}
        unvisited = [c for c in course.controls if c.si_code not in controls_visited_codes]
        previews.append({
            "entry":             entry_row,
            "result":            result,
            "course":            course,
            "unvisited_controls": unvisited,
        })

    return templates.TemplateResponse(request, "chip_review.html", {
        "pending":  pending,
        "previews": previews,
        "si_chip":  si_chip,
    })


@app.post("/chip-review/{si_chip}/save")
async def chip_review_save(si_chip: int, request: Request):
    """Commit a pending chip read to the database with any quick adjustments."""
    pending = _pending_reads.pop(si_chip, None)
    if not pending:
        return RedirectResponse("/", status_code=303)

    form              = await request.form()
    manual_si_codes   = [int(v) for v in form.getlist("manual_punch") if v.strip().isdigit()]
    overtime_waive    = form.get("overtime_waive") == "1"
    overtime_adjust_s = form.get("overtime_adjust", "").strip()

    entries        = pending["entries"]
    punches        = pending["punches"]
    finish_time    = pending["finish_time"]
    chip_start_time = pending["chip_start_time"]

    for entry_row in entries:
        entry_id   = entry_row["id"]
        course_row = db.get_course(entry_row["course_id"])
        use_si     = course_row and bool(course_row["use_si_start"])

        if use_si and chip_start_time:
            db.set_entry_start_if_unset(entry_id, chip_start_time)
        elif course_row and course_row["start_time"]:
            db.set_entry_start_if_unset(
                entry_id, datetime.fromisoformat(course_row["start_time"])
            )

        if punches:
            db.add_punches(entry_id, punches)
        if manual_si_codes and finish_time:
            db.add_punches(
                entry_id,
                [(code, finish_time) for code in manual_si_codes],
                is_manual=True,
            )
        if finish_time:
            db.update_finish_time_if_later(entry_id, finish_time)

        racer_row       = db.get_racer(entry_row["racer_id"])
        is_final_course = False
        if racer_row and racer_row["class_id"]:
            cls_row = db.get_class(racer_row["class_id"])
            if cls_row and cls_row["final_course_id"] == entry_row["course_id"]:
                is_final_course = True

        new_status = "FINISHED" if is_final_course else "ON_COURSE"
        if finish_time and course_row and course_row["cutoff_time"]:
            try:
                if finish_time > datetime.fromisoformat(course_row["cutoff_time"]):
                    new_status = "DSQ"
            except (ValueError, TypeError):
                pass
        db.set_entry_status(entry_id, new_status)

        # Overtime adjustment — either waive entirely or apply a custom signed pts value.
        # Build the preview score to know the calculated overtime.
        if overtime_waive or overtime_adjust_s:
            preview_pair = _build_chip_preview(
                entry_row, punches, finish_time, chip_start_time,
                extra_si_codes=manual_si_codes if manual_si_codes else None,
            )
            if preview_pair:
                preview_result, _ = preview_pair
                if overtime_waive and preview_result.overtime_penalty:
                    db.add_adjustment(
                        entry_id,
                        description=f"Overtime waived at finish ({preview_result.overtime_minutes}min)",
                        points=preview_result.overtime_penalty,
                        category="manual",
                    )
                elif overtime_adjust_s:
                    try:
                        adj_pts = int(overtime_adjust_s)
                        if adj_pts != 0:
                            db.add_adjustment(
                                entry_id,
                                description="Overtime adjustment at finish",
                                points=adj_pts,
                                category="manual",
                            )
                    except ValueError:
                        pass

    # Score and print receipts.
    all_results = db.load_all_results()
    for entry_row in entries:
        entry_id = entry_row["id"]
        for result in all_results:
            if result.entry.id != entry_id:
                continue
            if result.unmatched_punch_codes and not result.controls_visited:
                _si_event(
                    f"WARNING: Chip {si_chip} — {len(result.unmatched_punch_codes)} punches "
                    f"but 0 controls matched on '{result.course.name}' for "
                    f"{result.racer.name} — possible wrong chip or wrong course."
                )
            course_results = [r for r in all_results if r.course.id == result.course.id]
            ranked = rank_results(course_results)
            rank   = next((pos for pos, r in ranked if r.entry.id == entry_id), None)
            _si_event(f"Saved: {result.racer.name} — {result.total_points}pts (rank #{rank})")
            if CFG.get("print_on_read", True) and CFG.get("printer_name"):
                print_receipt(
                    result, rank=rank,
                    printer_name=CFG.get("printer_name"),
                    race_name=CFG.get("race_name", "Medicine Hat Massacre").upper(),
                )
            break

    return RedirectResponse("/", status_code=303)


@app.post("/chip-review/{si_chip}/discard")
async def chip_review_discard(si_chip: int):
    """Discard a pending chip read without writing anything to the database."""
    _pending_reads.pop(si_chip, None)
    _si_event(f"Chip {si_chip} read discarded by operator")
    return RedirectResponse("/", status_code=303)


@app.get("/api/si/log")
async def si_log():
    """
    Return the last 200 lines of mhm.log as JSON.

    Accessible via the dashboard "Full log" link. Useful for diagnosing reader
    errors that scrolled off the live event feed.
    """
    try:
        with open(str(_LOG_PATH), encoding="utf-8") as f:
            lines = f.readlines()
        return JSONResponse({"lines": lines[-200:]})
    except FileNotFoundError:
        return JSONResponse({"lines": []})


# ── Racers ────────────────────────────────────────────────────────────────────

@app.get("/racers", response_class=HTMLResponse)
async def racers_list(request: Request, msg: str = "", racer_id: Optional[int] = None):
    """List all teams with a detail panel for the selected racer."""
    racers  = sorted(db.get_all_racers(),
                     key=lambda r: (_natural_sort_key(r['bib_number']), _natural_sort_key(r['name'])))
    courses = sorted(db.get_all_courses(), key=lambda c: _natural_sort_key(c['name']))
    classes = db.get_all_classes()

    selected_racer   = None
    selected_entries = []
    selected_results: dict = {}

    if racer_id:
        selected_racer   = db.get_racer(racer_id)
        selected_entries = db.get_entries_for_racer(racer_id)
        all_results      = db.load_all_results()
        selected_results = {r.course.id: r for r in all_results if r.racer.id == racer_id}

    return templates.TemplateResponse(request, "racers.html", {
        "racers":           racers,
        "courses":          courses,
        "classes":          classes,
        "selected_racer":   selected_racer,
        "selected_entries": selected_entries,
        "selected_results": selected_results,
        "selected_racer_id": racer_id,
        "msg":              msg,
    })


@app.post("/racers/add")
async def add_racer(
    name:      str = Form(...),
    bib_number: str = Form(""),
    class_id:  str = Form(""),
):
    """Create a new team. class_id is optional — teams can exist without a class."""
    cid      = int(class_id) if class_id.strip() else None
    racer_id = db.upsert_racer(name, bib_number, class_id=cid)
    if cid:
        for course_id in db.get_courses_for_class(cid):
            db.enroll_racer(racer_id, course_id)
    return RedirectResponse(f"/racers?msg=Team+{name}+added", status_code=303)


@app.post("/racers/{racer_id}/edit")
async def edit_racer(
    racer_id:  int,
    name:      str = Form(...),
    bib_number: str = Form(""),
    class_id:  str = Form(""),
):
    """Update an existing team's name, bib, or class. Auto-enrolls in class courses."""
    cid = int(class_id) if class_id.strip() else None
    db.upsert_racer(name, bib_number, class_id=cid, racer_id=racer_id)
    if cid:
        for course_id in db.get_courses_for_class(cid):
            db.enroll_racer(racer_id, course_id)
    return RedirectResponse(f"/racers?racer_id={racer_id}&msg=Team+updated", status_code=303)


@app.post("/racers/{racer_id}/delete")
async def delete_racer(racer_id: int):
    """Delete a team and all their entries/punches (cascaded in the DB schema)."""
    db.delete_racer(racer_id)
    return RedirectResponse("/racers?msg=Team+deleted", status_code=303)


@app.post("/racers/{racer_id}/merge")
async def merge_racer(racer_id: int, target_id: int = Form(...)):
    """Merge racer_id into target_id — moves entries then deletes the source."""
    moved, dropped = db.merge_racer(racer_id, target_id)
    msg = f"Merged+into+target.+{moved}+entries+moved"
    if dropped:
        msg += f",+{dropped}+dropped+(conflict)"
    return RedirectResponse(f"/racers?racer_id={target_id}&msg={msg}", status_code=303)


@app.post("/racers/{racer_id}/enroll")
async def enroll(racer_id: int, course_id: int = Form(...), si_chip: str = Form("")):
    """
    Enroll a team in a course, optionally assigning their SI chip number.

    A team can be enrolled in multiple courses (e.g. if they try both distances).
    si_chip is stored as an integer; empty string means no chip assigned yet.
    """
    chip = int(si_chip) if si_chip.strip() else None
    db.enroll_racer(racer_id, course_id, chip)
    return RedirectResponse(f"/racers?racer_id={racer_id}&msg=Enrolled", status_code=303)


@app.post("/racers/{racer_id}/entries/{entry_id}/chip")
async def set_chip(racer_id: int, entry_id: int, si_chip: str = Form("")):
    """Assign or clear the SI chip number for a specific course entry."""
    chip = int(si_chip) if si_chip.strip() else None
    db.set_entry_chip(entry_id, chip)
    return RedirectResponse(f"/racers?racer_id={racer_id}&msg=Chip+updated", status_code=303)


@app.post("/racers/{racer_id}/entries/{entry_id}/unenroll")
async def unenroll(racer_id: int, entry_id: int):
    """Remove a team from a course. Their racer record is kept; only the entry is deleted."""
    db.unenroll_racer(entry_id)
    return RedirectResponse(f"/racers?racer_id={racer_id}&msg=Unenrolled", status_code=303)


@app.post("/racers/{racer_id}/entries/{entry_id}/move-chip-read")
async def move_chip_read_route(
    racer_id: int, entry_id: int, to_entry_id: int = Form(...)
):
    """
    Move punch data, finish time, and start time from one course entry to another.

    Called when a racer used the wrong chip and their results need to be reassigned
    from the chip's enrolled course to the course they actually ran.
    After moving, sets the target entry's status based on whether it is the class's
    final course.
    """
    db.move_chip_read(entry_id, to_entry_id)

    # Set the target entry's status now that it has chip data.
    to_entry = db.get_entry_by_id(to_entry_id)
    if to_entry:
        racer_row = db.get_racer(to_entry["racer_id"])
        new_status = "ON_COURSE"
        if racer_row and racer_row["class_id"]:
            cls_row = db.get_class(racer_row["class_id"])
            if cls_row and cls_row["final_course_id"] == to_entry["course_id"]:
                new_status = "FINISHED"
        db.set_entry_status(to_entry_id, new_status)

    return RedirectResponse(
        f"/racers?racer_id={racer_id}&msg=Chip+read+moved", status_code=303
    )


@app.post("/racers/{racer_id}/status")
async def set_racer_status(racer_id: int, status: str = Form(...)):
    """Set status on ALL course entries for a racer at once (racer-level, not per-entry)."""
    db.set_racer_status(racer_id, status)
    return RedirectResponse(f"/racers?racer_id={racer_id}&msg=Status+updated", status_code=303)


@app.post("/racers/bulk-edit")
async def bulk_edit_racers(request: Request):
    """
    Bulk-assign a class and/or enroll multiple teams in courses at once.

    Sets class on all selected racers if class_id is provided, then enrolls each
    in that class's default courses plus any explicitly chosen courses.
    Enrollment is idempotent — already-enrolled racers are not duplicated.
    """
    form       = await request.form()
    racer_ids  = [int(v) for v in form.getlist("racer_ids")]
    class_id_s = (form.get("class_id") or "").strip()
    course_ids = [int(v) for v in form.getlist("course_ids")]
    status_s   = (form.get("status") or "").strip()

    cid = int(class_id_s) if class_id_s else None

    for rid in racer_ids:
        racer = db.get_racer(rid)
        if racer is None:
            continue
        if cid is not None:
            db.upsert_racer(racer["name"], racer["bib_number"], class_id=cid, racer_id=rid)
            for course_id in db.get_courses_for_class(cid):
                db.enroll_racer(rid, course_id)
        for course_id in course_ids:
            db.enroll_racer(rid, course_id)
        if status_s:
            db.set_racer_status(rid, status_s)

    n   = len(racer_ids)
    msg = f"Updated+{n}+team{'s' if n != 1 else ''}"
    return RedirectResponse(f"/racers?msg={msg}", status_code=303)


@app.get("/racers/{racer_id}/export", response_class=HTMLResponse)
async def racer_export(request: Request, racer_id: int):
    """Printable per-racer results card: punches, times, and score breakdown per course."""
    all_results = db.load_all_results()
    results     = sorted(
        [r for r in all_results if r.racer.id == racer_id],
        key=lambda r: r.course.name,
    )
    if not results:
        # Racer has no entries yet — still show the page with racer info
        racer = db.get_racer(racer_id)
        if not racer:
            return RedirectResponse("/racers", status_code=303)
        racer_obj = racer
    else:
        racer_obj = results[0].racer
    race_name = CFG.get("race_name", "Race")
    return templates.TemplateResponse(request, "racer_export.html", {
        "racer":     racer_obj,
        "results":   results,
        "race_name": race_name,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })


# ── Courses ───────────────────────────────────────────────────────────────────

def _build_courses_with_controls():
    """Return the courses_with_controls list used by the courses page and export."""
    courses = sorted(db.get_all_courses(), key=lambda c: _natural_sort_key(c['name']))
    result = []
    for c in courses:
        controls_raw = db.get_controls_for_course(c["id"])
        # Natural sort: mandatory_order first (None → 99999), then name naturally.
        controls = sorted(
            controls_raw,
            key=lambda cp: (
                cp["mandatory_order"] if cp["mandatory_order"] is not None else 99999,
                _natural_sort_key(cp["name"]),
            ),
        )
        circuit_groups: dict[str, dict] = {}
        for cp in controls:
            grp = cp["circuit_group"] if "circuit_group" in cp.keys() else ""
            if not grp:
                continue
            if grp not in circuit_groups:
                circuit_groups[grp] = {"controls": [], "penalty": 0}
            circuit_groups[grp]["controls"].append(cp)
            pen = cp["circuit_miss_penalty"] if "circuit_miss_penalty" in cp.keys() else 0
            if pen:
                circuit_groups[grp]["penalty"] = max(circuit_groups[grp]["penalty"], pen)
        for gd in circuit_groups.values():
            gd["controls"].sort(key=lambda x: _natural_sort_key(x["name"]))
        result.append({
            "course":         c,
            "controls":       controls,
            "circuit_groups": circuit_groups,
        })
    return result


@app.get("/courses", response_class=HTMLResponse)
async def courses_list(request: Request, msg: str = ""):
    """List all courses with their control points. Fetches controls per course in a loop."""
    return templates.TemplateResponse(request, "courses.html", {
        "courses_with_controls": _build_courses_with_controls(),
        "msg": msg,
    })


@app.get("/courses/{course_id}/export.csv")
async def export_course_csv(course_id: int):
    """Download controls for one course as a CSV file."""
    import io, csv as _csv
    from fastapi.responses import Response as _Resp
    course   = db.get_course(course_id)
    controls = db.get_controls_for_course(course_id)
    buf      = io.StringIO()
    w        = _csv.writer(buf)
    w.writerow(["si_code", "name", "points", "is_mandatory", "mandatory_order",
                "mandatory_miss_penalty", "circuit_group", "circuit_miss_penalty"])
    for cp in controls:
        ck = cp.keys()
        w.writerow([
            cp["si_code"], cp["name"], cp["points"], int(cp["is_mandatory"]),
            cp["mandatory_order"] if cp["mandatory_order"] is not None else "",
            cp["mandatory_miss_penalty"] if "mandatory_miss_penalty" in ck else 0,
            cp["circuit_group"]        if "circuit_group"        in ck else "",
            cp["circuit_miss_penalty"] if "circuit_miss_penalty" in ck else 0,
        ])
    safe_name = (course["name"] if course else f"course_{course_id}").replace(" ", "_")
    return _Resp(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_controls.csv"'},
    )


def _time_limit_from_window(start_time: str, end_time: str) -> int:
    from datetime import datetime as _dt
    try:
        return max(0, int((_dt.fromisoformat(end_time) - _dt.fromisoformat(start_time)).total_seconds() / 60))
    except (ValueError, TypeError):
        return 0


@app.post("/courses/add")
async def add_course(
    request: Request,
    name:                        str  = Form(...),
    overtime_penalty_per_minute: int  = Form(0),
    start_time:                  str  = Form(""),
    end_time:                    str  = Form(""),
    cutoff_time:                 str  = Form(""),
    use_si_start:                bool = Form(False),
):
    """Create a new course. Time limit is derived from end_time − start_time."""
    if not use_si_start and not start_time:
        return templates.TemplateResponse(request, "courses.html", {
            "courses_with_controls": _build_courses_with_controls(),
            "error": "Start time is required when using mass start.",
        })
    if not end_time:
        return templates.TemplateResponse(request, "courses.html", {
            "courses_with_controls": _build_courses_with_controls(),
            "error": "End time is required.",
        })
    time_limit_minutes = _time_limit_from_window(start_time, end_time)
    db.upsert_course(
        name, time_limit_minutes, overtime_penalty_per_minute,
        start_time=start_time or None,
        end_time=end_time or None,
        cutoff_time=cutoff_time or None,
        use_si_start=use_si_start,
    )
    return RedirectResponse(f"/courses?msg=Course+{name}+added", status_code=303)


@app.post("/courses/{course_id}/edit")
async def edit_course(
    request: Request,
    course_id:                   int,
    name:                        str  = Form(...),
    overtime_penalty_per_minute: int  = Form(0),
    start_time:                  str  = Form(""),
    end_time:                    str  = Form(""),
    cutoff_time:                 str  = Form(""),
    use_si_start:                bool = Form(False),
):
    if not use_si_start and not start_time:
        return templates.TemplateResponse(request, "courses.html", {
            "courses_with_controls": _build_courses_with_controls(),
            "error": "Start time is required when using mass start.",
        })
    if not end_time:
        return templates.TemplateResponse(request, "courses.html", {
            "courses_with_controls": _build_courses_with_controls(),
            "error": "End time is required.",
        })
    time_limit_minutes = _time_limit_from_window(start_time, end_time)
    db.upsert_course(
        name, time_limit_minutes, overtime_penalty_per_minute,
        start_time=start_time or None,
        end_time=end_time or None,
        cutoff_time=cutoff_time or None,
        use_si_start=use_si_start,
        course_id=course_id,
    )
    return RedirectResponse("/courses?msg=Course+updated", status_code=303)


@app.post("/courses/{course_id}/delete")
async def delete_course(course_id: int):
    """Delete a course and all its controls (cascaded). Enrolled entries are also removed."""
    db.delete_course(course_id)
    return RedirectResponse("/courses?msg=Course+deleted", status_code=303)


@app.post("/courses/{course_id}/controls/add")
async def add_control(
    course_id:              int,
    si_code:                int  = Form(...),
    name:                   str  = Form(""),
    points:                 int  = Form(0),
    is_mandatory:           bool = Form(False),
    mandatory_order:        Optional[int] = Form(None),
    mandatory_miss_penalty: int  = Form(0),
    circuit_group:          str  = Form(""),
    circuit_miss_penalty:   int  = Form(0),
):
    db.upsert_control(
        course_id, si_code, name, points, is_mandatory, mandatory_order,
        mandatory_miss_penalty=mandatory_miss_penalty,
        circuit_group=circuit_group.strip(),
        circuit_miss_penalty=circuit_miss_penalty,
    )
    return RedirectResponse("/courses?msg=Control+added", status_code=303)


@app.post("/courses/{course_id}/controls/{control_id}/edit")
async def edit_control(
    course_id:              int,
    control_id:             int,
    si_code:                int  = Form(...),
    name:                   str  = Form(""),
    points:                 int  = Form(0),
    is_mandatory:           bool = Form(False),
    mandatory_order:        Optional[int] = Form(None),
    mandatory_miss_penalty: int  = Form(0),
    circuit_group:          str  = Form(""),
    circuit_miss_penalty:   int  = Form(0),
):
    db.upsert_control(
        course_id, si_code, name, points, is_mandatory, mandatory_order,
        control_id=control_id,
        mandatory_miss_penalty=mandatory_miss_penalty,
        circuit_group=circuit_group.strip(),
        circuit_miss_penalty=circuit_miss_penalty,
    )
    return RedirectResponse("/courses?msg=Control+updated", status_code=303)


@app.post("/courses/{course_id}/controls/{control_id}/delete")
async def delete_control(course_id: int, control_id: int):
    db.delete_control(control_id)
    return RedirectResponse("/courses?msg=Control+deleted", status_code=303)


@app.post("/courses/{course_id}/controls/bulk-assign")
async def bulk_assign_controls(course_id: int, request: Request):
    """Batch-update mandatory, order, and circuit assignments for all controls in a course."""
    form = await request.form()

    grp_names     = form.getlist("grp_name")
    grp_penalties = form.getlist("grp_penalty")
    group_penalty: dict[str, int] = {}
    for g_name, g_pen in zip(grp_names, grp_penalties):
        g_name = g_name.strip()
        if g_name:
            try:
                group_penalty[g_name] = int(g_pen or 0)
            except ValueError:
                group_penalty[g_name] = 0

    controls = db.get_controls_for_course(course_id)
    for cp in controls:
        ctrl_id  = cp["id"]
        is_mand  = form.get(f"mandatory_{ctrl_id}") == "1"
        miss_s   = form.get(f"miss_pen_{ctrl_id}", "")
        miss_pen = int(miss_s) if miss_s.strip() else 0
        order_s  = form.get(f"order_{ctrl_id}", "")
        order    = int(order_s) if order_s.strip() else None
        circuit  = form.get(f"circuit_{ctrl_id}", "").strip()
        circ_pen = group_penalty.get(circuit, 0)
        db.upsert_control(
            course_id, cp["si_code"], cp["name"], cp["points"],
            is_mand, order,
            control_id=ctrl_id,
            mandatory_miss_penalty=miss_pen,
            circuit_group=circuit,
            circuit_miss_penalty=circ_pen,
        )

    return RedirectResponse("/courses?msg=Assignments+saved", status_code=303)


@app.post("/courses/{course_id}/controls/add-bulk")
async def add_controls_bulk(course_id: int, request: Request):
    """
    Quick-add multiple controls from the Adjustments page's "unmatched punches" form.

    The form submits parallel arrays: si_code[], ctrl_name[], points[].
    We zip them together and call upsert_control for each. Any row with a
    parse error is silently skipped — the race director can add missing ones manually.
    Redirects back to /adjustments so the user sees the updated matched/unmatched status.
    """
    form       = await request.form()
    si_codes   = form.getlist("si_code")
    names      = form.getlist("ctrl_name")
    pts_values = form.getlist("points")

    added = 0
    for si_code, name, pts in zip(si_codes, names, pts_values):
        try:
            db.upsert_control(
                course_id, int(si_code), name.strip(), int(pts or 0), False, None
            )
            added += 1
        except (ValueError, Exception):
            pass  # skip rows with non-numeric codes

    return RedirectResponse(f"/adjustments?msg={added}+control(s)+added", status_code=303)


# ── Results ───────────────────────────────────────────────────────────────────

def _overall_status_val(course_scores: dict) -> str:
    """
    Derive a single overall status from a racer's per-course scored results.

    Priority (highest → lowest):
      FINISHED — chip read on final course, within time limit
      DSQ      — chip read on final course, over time limit (scored, unranked)
      DNF      — manual (scored, unranked)
      ON_COURSE/ACTIVE — any leg chip was read (or manually set)
      REGISTERED — checked in at start line
      SIGNED_UP  — entered but not yet registered
      DNS        — all entries are DNS → excluded from leaderboard entirely
    """
    if not course_scores:
        return "DNS"
    statuses = {sr.entry.status.value for sr in course_scores.values()}
    # Auto-set by chip read on final course — top priority
    if "FINISHED" in statuses:
        return "FINISHED"
    if "DSQ" in statuses:
        return "DSQ"
    if "DNF" in statuses:
        return "DNF"
    # Exclude only when every entry is DNS
    non_dns = statuses - {"DNS"}
    if not non_dns:
        return "DNS"
    # Manual progression — show most advanced
    for s in ("ON_COURSE", "ACTIVE", "OK", "REGISTERED", "SIGNED_UP"):
        if s in non_dns:
            return s
    return "SIGNED_UP"


@app.get("/results", response_class=HTMLResponse)
async def results(request: Request):
    """
    Public class-standings leaderboard — no auth required.

    Aggregates each team's scores across all courses. Teams with all-DNS entries
    are excluded. DNF/DSQ teams are shown at the bottom with no rank number.
    """
    classes     = db.get_all_classes()
    courses     = db.get_all_courses()
    all_results = db.load_all_results()

    by_racer: dict[int, dict] = {}
    for r in all_results:
        by_racer.setdefault(r.racer.id, {})[r.course.id] = r

    def _build_row(racer) -> dict:
        course_scores   = by_racer.get(racer["id"], {})
        overall_status  = _overall_status_val(course_scores)
        pre_adj = {
            cid: sr.raw_points - sr.overtime_penalty
            for cid, sr in course_scores.items()
        }
        all_adj     = [a for sr in course_scores.values() for a in sr.entry.adjustments]
        bonus_pts   = sum(a.points for a in all_adj if a.category == "bonus")
        penalty_pts = sum(a.points for a in all_adj if a.category == "penalty")
        manual_pts  = sum(a.points for a in all_adj if a.category == "manual")
        grand_total = max(0, sum(pre_adj.values()) + bonus_pts + penalty_pts + manual_pts)
        return {
            "racer": racer, "course_scores": course_scores, "pre_adj": pre_adj,
            "bonus_pts": bonus_pts, "penalty_pts": penalty_pts,
            "manual_pts": manual_pts, "total": grand_total,
            "overall_status": overall_status, "rank": None,
        }

    _UNRANKED = {"DSQ", "DNF"}

    def _rank_rows(rows: list[dict]) -> list[dict]:
        competitive = [r for r in rows if r["overall_status"] not in _UNRANKED | {"DNS"}]
        unranked    = [r for r in rows if r["overall_status"] in _UNRANKED]
        # DNS excluded entirely — not added to output
        _sort_key = lambda x: (-x["total"], x["racer"]["bib_number"] or "")
        competitive.sort(key=_sort_key)
        unranked.sort(key=_sort_key)
        for i, row in enumerate(competitive, start=1):
            row["rank"] = i
        return competitive + unranked

    class_data = []
    for cls in classes:
        racers = db.get_racers_by_class(cls["id"])
        rows   = _rank_rows([_build_row(r) for r in racers])
        class_data.append({"cls": cls, "rows": rows})

    unclassified = db.get_unclassified_racers()
    if unclassified:
        rows = _rank_rows([_build_row(r) for r in unclassified])
        class_data.append({"cls": {"id": None, "name": "Unclassified"}, "rows": rows})

    return templates.TemplateResponse(request, "class_results.html", {
        "class_data": class_data,
        "courses":    courses,
    })


# ── Results manage (login-required bulk status editor) ────────────────────────

@app.get("/results/manage", response_class=HTMLResponse)
async def results_manage(request: Request, msg: str = ""):
    """
    Login-protected page for bulk-editing team statuses.

    Shows one row per racer (not per entry). Changing status updates ALL of a
    racer's course entries at once, so the leaderboard reflects the change immediately.
    """
    all_results = db.load_all_results()
    courses     = db.get_all_courses()

    # Aggregate to one row per racer.
    by_racer: dict[int, dict] = {}
    for r in all_results:
        if r.racer.id not in by_racer:
            by_racer[r.racer.id] = {
                "racer":          r.racer,
                "course_scores":  {},
            }
        by_racer[r.racer.id]["course_scores"][r.course.id] = r

    racer_rows = []
    for data in by_racer.values():
        data["overall_status"] = _overall_status_val(data["course_scores"])
        racer_rows.append(data)

    racer_rows.sort(key=lambda x: (
        _natural_sort_key(x["racer"].bib_number or ""),
        _natural_sort_key(x["racer"].name),
    ))

    return templates.TemplateResponse(request, "results_manage.html", {
        "racer_rows": racer_rows,
        "courses":    courses,
        "msg":        msg,
    })


@app.post("/results/manage")
async def results_manage_update(racer_id: int = Form(...), status: str = Form(...)):
    """Change status for all entries of one racer."""
    db.set_racer_status(racer_id, status)
    return RedirectResponse("/results/manage?msg=Status+updated", status_code=303)


@app.post("/results/manage/bulk")
async def results_manage_bulk(request: Request):
    """Bulk-set status for multiple racers at once."""
    form      = await request.form()
    racer_ids = [int(v) for v in form.getlist("racer_ids")]
    status    = (form.get("status") or "").strip()
    if status and racer_ids:
        for rid in racer_ids:
            db.set_racer_status(rid, status)
    n   = len(racer_ids)
    msg = f"{n}+team{'s' if n != 1 else ''}+updated"
    return RedirectResponse(f"/results/manage?msg={msg}", status_code=303)


# ── Results finalize (auth-required pre-export validation) ────────────────────

@app.get("/finalize", response_class=HTMLResponse)
async def finalize(request: Request):
    """
    Pre-export validation page. Checks every entry for potential scoring issues
    and presents a summary before the race director commits to the final export.

    Auth is enforced by the middleware (path does not start with a public prefix).
    """
    from shared.models import EntryStatus
    all_results = db.load_all_results()
    courses     = db.get_all_courses()

    issues:   list[dict] = []  # definite problems (unmatched punches, no finish)
    warnings: list[dict] = []  # advisory items (0 pts, still racing)

    for r in all_results:
        tag = f"#{r.racer.bib_number or '?'} {r.racer.name} ({r.course.name})"

        if r.entry.status == EntryStatus.ACTIVE:
            warnings.append({"msg": f"{tag} — still ACTIVE (chip not read or not assigned)"})

        if r.entry.status == EntryStatus.OK and r.total_points == 0:
            warnings.append({"msg": f"{tag} — finished OK but scored 0 points"})

        if r.unmatched_punch_codes:
            issues.append({
                "msg": f"{tag} — {len(r.unmatched_punch_codes)} unmatched punch(es): "
                       + ", ".join(str(c) for c in r.unmatched_punch_codes)
            })

        if r.controls_missed_mandatory:
            warnings.append({
                "msg": f"{tag} — missed mandatory control(s): "
                       + ", ".join(cp.name or str(cp.si_code) for cp in r.controls_missed_mandatory)
            })

        if r.order_violation:
            warnings.append({"msg": f"{tag} — mandatory control order violation"})

        if r.entry.status not in (EntryStatus.DNS, EntryStatus.ACTIVE) and not r.entry.finish_time:
            issues.append({"msg": f"{tag} — status is {r.entry.status.value} but no finish time recorded"})

        if r.entry.status == EntryStatus.OK and not r.entry.start_time:
            warnings.append({"msg": f"{tag} — status OK but no start time recorded"})

    # Courses with no finished entries at all
    finished_course_ids = {
        r.course.id for r in all_results
        if r.entry.status in (EntryStatus.OK, EntryStatus.DSQ, EntryStatus.DNF)
    }
    for c in courses:
        if c["id"] not in finished_course_ids:
            warnings.append({"msg": f"Course '{c['name']}' has no finished entries"})

    return templates.TemplateResponse(request, "finalize.html", {
        "issues":   issues,
        "warnings": warnings,
        "has_problems": bool(issues),
    })


# ── Offline results export ────────────────────────────────────────────────────

@app.get("/export", response_class=HTMLResponse)
async def export_hub(request: Request, msg: str = ""):
    """Export hub — lists all available exports in one place."""
    courses = db.get_all_courses()
    return templates.TemplateResponse(request, "export.html", {
        "courses": courses,
        "msg":     msg,
    })


@app.get("/export/results", response_class=HTMLResponse)
async def export_results(request: Request):
    """
    Generate a self-contained HTML results export — no login required.

    Produces a single downloadable HTML file with embedded CSS showing every
    class as its own section, racers sorted by grand total within each class.
    The file can be opened in any browser offline and printed to PDF.
    """
    classes     = db.get_all_classes()
    courses     = db.get_all_courses()
    all_results = db.load_all_results()

    by_racer: dict[int, dict] = {}
    for r in all_results:
        by_racer.setdefault(r.racer.id, {})[r.course.id] = r

    def _build_row(racer) -> dict:
        course_scores = by_racer.get(racer["id"], {})
        pre_adj = {cid: sr.raw_points - sr.overtime_penalty for cid, sr in course_scores.items()}
        all_adj     = [a for sr in course_scores.values() for a in sr.entry.adjustments]
        bonus_pts   = sum(a.points for a in all_adj if a.category == "bonus")
        penalty_pts = sum(a.points for a in all_adj if a.category == "penalty")
        manual_pts  = sum(a.points for a in all_adj if a.category == "manual")
        grand_total = max(0, sum(pre_adj.values()) + bonus_pts + penalty_pts + manual_pts)
        return {
            "racer": racer, "course_scores": course_scores, "pre_adj": pre_adj,
            "bonus_pts": bonus_pts, "penalty_pts": penalty_pts,
            "manual_pts": manual_pts, "total": grand_total,
        }

    class_data = []
    for cls in classes:
        racers = db.get_racers_by_class(cls["id"])
        rows   = sorted([_build_row(r) for r in racers], key=lambda x: -x["total"])
        for i, row in enumerate(rows, start=1):
            row["rank"] = i
        class_data.append({"cls": cls, "rows": rows})

    unclassified = db.get_unclassified_racers()
    if unclassified:
        rows = sorted([_build_row(r) for r in unclassified], key=lambda x: -x["total"])
        for i, row in enumerate(rows, start=1):
            row["rank"] = i
        class_data.append({"cls": {"id": None, "name": "Unclassified"}, "rows": rows})

    race_name = CFG.get("race_name", "Race Results")
    race_year = CFG.get("race_year", "")
    generated = datetime.now().strftime("%Y-%m-%d  %H:%M")

    return templates.TemplateResponse(request, "export_results.html", {
        "class_data": class_data,
        "courses":    courses,
        "race_name":  race_name,
        "race_year":  race_year,
        "generated":  generated,
    })


# ── Per-entry receipt print view ──────────────────────────────────────────────

@app.get("/results/{entry_id}/receipt", response_class=HTMLResponse)
async def receipt_print_page(request: Request, entry_id: int):
    """
    Browser-printable receipt for a single entry.

    Covered by the /results public prefix — no login required.
    Opens a standalone page (no nav bar) that auto-triggers window.print() and shows:
      1. The exact receipt text produced by build_receipt_lines() — same format
         as the thermal printer output (42-char wide, monospace).
      2. A full raw punch log: every SI code + timestamp recorded on the chip,
         each labelled as the matched control (with points) or unmatched.
    """
    all_results = db.load_all_results()
    result = next((r for r in all_results if r.entry.id == entry_id), None)
    if not result:
        return RedirectResponse("/results?msg=Entry+not+found", status_code=303)

    # Compute rank within this entry's course for the receipt header.
    course_results = [r for r in all_results if r.course.id == result.course.id]
    ranked         = rank_results(course_results)
    rank           = next((pos for pos, r in ranked if r.entry.id == entry_id), None)

    race_name = CFG.get("race_name", "Medicine Hat Massacre").upper()
    lines     = build_receipt_lines(result, rank=rank, race_name=race_name)

    # Build a map of si_code → matched ControlPoint for the punch annotation table.
    # Only the first punch per control counts (same rule as the scoring engine).
    matched_map = {cp.si_code: cp for cp in result.controls_visited}

    return templates.TemplateResponse(request, "receipt_print.html", {
        "result":      result,
        "rank":        rank,
        "lines":       lines,
        "race_name":   race_name,
        "matched_map": matched_map,
    })


# ── Adjustment types (predefined bonus/penalty templates) ─────────────────────

@app.get("/adjustment-types", response_class=HTMLResponse)
async def adjustment_types_page(request: Request, msg: str = ""):
    """
    Manage predefined bonus/penalty type templates.

    Types are created once (e.g. "Photo Challenge" = +5 pts bonus) and then
    applied to any racer from the Adjustments page. The value can be overridden
    per application, so "Photo Challenge" can be 5 pts for one team and 3 pts
    for another.
    """
    adj_types = db.get_all_adjustment_types()
    return templates.TemplateResponse(request, "adjustment_types.html", {
        "adj_types": adj_types,
        "msg":       msg,
    })


@app.post("/adjustment-types/add")
async def add_adjustment_type(
    name:           str = Form(...),
    category:       str = Form("bonus"),
    default_points: int = Form(0),
    description:    str = Form(""),
):
    """
    Create a new adjustment type template.

    default_points is stored as a positive magnitude regardless of category —
    the category ('bonus'/'penalty') determines the sign when applied to an entry.
    """
    db.upsert_adjustment_type(name, category, default_points, description)
    return RedirectResponse("/adjustment-types?msg=Type+added", status_code=303)


@app.post("/adjustment-types/{type_id}/edit")
async def edit_adjustment_type(
    type_id:        int,
    name:           str = Form(...),
    category:       str = Form("bonus"),
    default_points: int = Form(0),
    description:    str = Form(""),
):
    """Update an existing adjustment type template."""
    db.upsert_adjustment_type(name, category, default_points, description, type_id=type_id)
    return RedirectResponse("/adjustment-types?msg=Type+updated", status_code=303)


@app.post("/adjustment-types/{type_id}/delete")
async def delete_adjustment_type_route(type_id: int):
    """
    Delete an adjustment type template.

    Existing entry_adjustments that used this type are converted to 'manual'
    (type_id nulled) so they remain visible in the UI rather than disappearing.
    """
    db.delete_adjustment_type(type_id)
    return RedirectResponse("/adjustment-types?msg=Type+deleted", status_code=303)


# ── Adjustments ───────────────────────────────────────────────────────────────

@app.get("/adjustments", response_class=HTMLResponse)
async def adjustments(request: Request, entry_id: Optional[int] = None, msg: str = ""):
    """
    Adjustments page — two-panel layout.

    Left panel: searchable list of all enrolled teams with bib and course.
    Right panel: full detail (score, punches, adjustments) for the selected entry.
    Selected entry is carried in the ?entry_id= query parameter so all POST
    actions can redirect back to the same racer.
    """
    all_results = db.load_all_results()
    adj_types   = db.get_all_adjustment_types()

    selected_result = None
    selected_data   = None
    if entry_id:
        for r in all_results:
            if r.entry.id == entry_id:
                selected_result = r
                break
        if selected_result:
            selected_data = {
                "adjustments": db.get_adjustments_for_entry(entry_id),
                "punches":     db.get_punches_for_entry(entry_id),
            }

    return templates.TemplateResponse(request, "adjustments.html", {
        "results":           all_results,
        "selected_result":   selected_result,
        "selected_data":     selected_data,
        "selected_entry_id": entry_id,
        "adj_types":         adj_types,
        "msg":               msg,
    })


@app.post("/adjustments/{entry_id}/status")
async def set_status(entry_id: int, status: str = Form(...)):
    """Manually set entry status (OK, DNS, DNF, DSQ). Overrides the automatic OK from chip read."""
    db.set_entry_status(entry_id, status)
    return RedirectResponse(f"/adjustments?entry_id={entry_id}&msg=Status+updated", status_code=303)


@app.post("/adjustments/{entry_id}/times")
async def set_times(entry_id: int, start_time: str = Form(""), finish_time: str = Form("")):
    """
    Manually set start/finish times for an entry.

    Accepts ISO 8601 datetime strings (e.g. "2026-05-09T08:00:00") from the
    datetime-local HTML input. Empty string = leave that time unchanged.
    Used to correct chip read times or handle teams who forgot to punch start.
    """
    st = datetime.fromisoformat(start_time) if start_time else None
    ft = datetime.fromisoformat(finish_time) if finish_time else None
    db.set_entry_times(entry_id, st, ft)
    return RedirectResponse(f"/adjustments?entry_id={entry_id}&msg=Times+updated", status_code=303)


@app.post("/adjustments/{entry_id}/adjustment/add")
async def add_adjustment(
    entry_id:    int,
    type_id:     str = Form(""),
    description: str = Form(""),
    points:      int = Form(0),
):
    """
    Add a bonus, penalty, or manual adjustment to an entry.

    Two modes:
      Predefined type (type_id set):
        - Looks up the template to get category and default points.
        - points form field = magnitude (always positive); sign is applied by category.
        - description field is optional; defaults to the type name if blank.
      Manual (type_id empty):
        - description and points are used as-is (points is signed by the user).
        - Stored as category='manual'.
    """
    if type_id.strip():
        tid      = int(type_id)
        type_row = db.get_adjustment_type(tid)
        if type_row:
            magnitude   = abs(points) if points != 0 else type_row["default_points"]
            actual_pts  = -magnitude if type_row["category"] == "penalty" else magnitude
            actual_desc = description.strip() or type_row["name"]
            db.add_adjustment(
                entry_id, actual_desc, actual_pts,
                type_id=tid, category=type_row["category"],
            )
    else:
        db.add_adjustment(entry_id, description, points, type_id=None, category="manual")
    return RedirectResponse(f"/adjustments?entry_id={entry_id}&msg=Adjustment+added", status_code=303)


@app.post("/adjustments/{entry_id}/adjustment/{adj_id}/delete")
async def delete_adjustment(entry_id: int, adj_id: int):
    db.delete_adjustment(adj_id)
    return RedirectResponse(f"/adjustments?entry_id={entry_id}&msg=Adjustment+deleted", status_code=303)


@app.post("/adjustments/{entry_id}/punch/add")
async def add_punch(entry_id: int, si_code: int = Form(...), punch_time: str = Form(...)):
    """
    Manually add a punch record for an entry (is_manual=True).

    Used when a team's chip failed to record a control (e.g. dead battery)
    but the race director has paper evidence they visited it. Manual punches
    are excluded from certain analyses but included in scoring.
    """
    db.add_punches(
        entry_id,
        [(si_code, datetime.fromisoformat(punch_time))],
        is_manual=True,
    )
    return RedirectResponse(f"/adjustments?entry_id={entry_id}&msg=Punch+added", status_code=303)


@app.post("/adjustments/{entry_id}/punch/{punch_id}/delete")
async def delete_punch(entry_id: int, punch_id: int):
    """Delete a specific punch record. Used to remove accidental or duplicate punches."""
    db.delete_punch(punch_id)
    return RedirectResponse(f"/adjustments?entry_id={entry_id}&msg=Punch+deleted", status_code=303)


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, msg: str = ""):
    """
    Settings page — race identity, printer, remote server, user management.

    Passes the live list of COM ports and Windows printers so dropdowns are current.
    """
    return templates.TemplateResponse(request, "settings.html", {
        "cfg":      CFG,
        "si_ports": list_ports(),
        "printers": list_windows_printers(),
        "users":    db.get_all_users(),
        "courses":  db.get_all_courses(),
        "msg":      msg,
    })


@app.post("/settings")
async def save_settings(
    race_name:             str = Form("Medicine Hat Massacre"),
    race_year:             str = Form("2026"),
    printer_name:          str = Form(""),
    server_url:            str = Form(""),
    api_key:               str = Form(""),
    sync_interval_seconds: int = Form(30),
    si_cn_offset:          int = Form(0),
):
    """
    Save all settings to config.json.

    All settings come from a single form POST so the entire CFG dict is updated at once.
    Keys not included in this form (e.g. si_port, selected_course_id) must be preserved —
    they are carried in CFG between saves and not submitted by the settings form.
    Using CFG.update() instead of CFG = {...} ensures those unrelated keys survive.
    """
    CFG.update({
        "race_name":             race_name,
        "race_year":             race_year,
        "printer_name":          printer_name,
        "server_url":            server_url,
        "api_key":               api_key,
        "sync_interval_seconds": sync_interval_seconds,
        "si_cn_offset":          si_cn_offset,
    })
    cfg_mod.save(CFG)
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


@app.post("/settings/clear-results")
async def clear_results():
    """
    Wipe all race results (entries, punches, adjustments) from the DB.

    Used at the end of a race day or to reset for testing. The courses, controls,
    and racer records are NOT deleted — only the time/punch/score data.
    """
    db.clear_all_results()
    return RedirectResponse("/settings?msg=All+results+cleared", status_code=303)


@app.post("/settings/clear-racers")
async def clear_racers():
    """
    Delete all racers and their dependent data (entries, punches, adjustments).

    Used to wipe a test import or reset registrations before re-importing from
    Race Roster. Courses and controls are not touched.
    """
    db.clear_all_racers()
    return RedirectResponse("/settings?msg=All+racers+cleared", status_code=303)


@app.post("/settings/users/add")
async def add_user(username: str = Form(...), password: str = Form(...)):
    """
    Create a new login user. Passwords are stored as bcrypt hashes in the DB.

    db.create_user() raises if the username already exists (UNIQUE constraint).
    We catch that and show a friendly message rather than a 500 error.
    """
    if not username or not password:
        return RedirectResponse(
            "/settings?msg=Username+and+password+required", status_code=303
        )
    try:
        db.create_user(username, password)
        return RedirectResponse("/settings?msg=User+added", status_code=303)
    except Exception:
        return RedirectResponse("/settings?msg=Username+already+exists", status_code=303)


@app.post("/settings/users/{user_id}/delete")
async def delete_user(user_id: int):
    db.delete_user(user_id)
    return RedirectResponse("/settings?msg=User+deleted", status_code=303)


@app.post("/settings/users/change-password")
async def change_password(
    request:      Request,
    username:     str = Form(...),
    new_password: str = Form(...),
):
    """Update a user's password. The new password is re-hashed before storage."""
    db.update_password(username, new_password)
    return RedirectResponse("/settings?msg=Password+updated", status_code=303)


# ── Classes ───────────────────────────────────────────────────────────────────
# Classes (categories) group teams for the class-results display.
# E.g. "Elite", "Recreational", "Junior". Teams can exist without a class.

@app.get("/classes", response_class=HTMLResponse)
async def classes_list(request: Request, msg: str = ""):
    classes            = db.get_all_classes()
    courses            = db.get_all_courses()
    class_courses_map  = db.get_all_class_courses()
    return templates.TemplateResponse(request, "classes.html", {
        "classes":           classes,
        "courses":           courses,
        "class_courses_map": class_courses_map,
        "msg":               msg,
    })


@app.post("/classes/add")
async def add_class(name: str = Form(...)):
    db.upsert_class(name)
    return RedirectResponse("/classes?msg=Class+added", status_code=303)


@app.post("/classes/{class_id}/edit")
async def edit_class(
    class_id: int,
    name: str = Form(...),
    final_course_id: str = Form(""),
):
    # Empty string → -1 sentinel which upsert_class converts to NULL (clear).
    fc = int(final_course_id) if final_course_id.strip() else -1
    db.upsert_class(name, class_id=class_id, final_course_id=fc)
    return RedirectResponse("/classes?msg=Class+updated", status_code=303)


@app.post("/classes/{class_id}/delete")
async def delete_class(class_id: int):
    db.delete_class(class_id)
    return RedirectResponse("/classes?msg=Class+deleted", status_code=303)


@app.post("/classes/{class_id}/courses")
async def set_class_courses(class_id: int, request: Request):
    """Set which courses are associated with a class for auto-enrollment."""
    form       = await request.form()
    course_ids = [int(v) for v in form.getlist("course_id")]
    db.set_class_courses(class_id, course_ids)
    return RedirectResponse("/classes?msg=Courses+updated", status_code=303)


# ── Class results ─────────────────────────────────────────────────────────────

@app.get("/class-results")
async def class_results_redirect():
    """Permanent redirect — /results is now the class standings page."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/results", status_code=301)


# ── Bulk import (CSV-style paste) ─────────────────────────────────────────────

@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request, msg: str = ""):
    """Import landing page — shows forms for CSV paste import and MeOS XML upload."""
    courses = db.get_all_courses()
    classes = db.get_all_classes()
    return templates.TemplateResponse(request, "import.html", {
        "courses": courses,
        "classes": classes,
        "msg":     msg,
    })


@app.post("/import/racers")
async def import_racers(request: Request, data: str = Form(...)):
    """
    Bulk import teams from pasted CSV text.

    Expected format: one team per line, "bib,name" or just "name".
    Empty lines and lines with no team name are skipped.
    db.bulk_import_racers() uses INSERT OR IGNORE so re-importing the same list
    is safe — existing teams are not duplicated or overwritten.
    """
    rows:   list[tuple[str, str]] = []
    errors: list[str] = []

    for i, raw in enumerate(data.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) == 2:
            bib, name = parts
        elif len(parts) == 1 and parts[0]:
            bib, name = "", parts[0]
        else:
            errors.append(f"Line {i}: skipped (empty)")
            continue
        if not name:
            errors.append(f"Line {i}: skipped (no team name)")
            continue
        rows.append((bib, name))

    if rows:
        inserted, skipped = db.bulk_import_racers(rows)
        msg = f"Imported {inserted} teams."
        if skipped:
            msg += f" {skipped} skipped."
        if errors:
            msg += " Errors: " + "; ".join(errors)
    else:
        msg = "No valid rows found. " + "; ".join(errors)

    return RedirectResponse(f"/import?msg={msg.replace(' ', '+')}", status_code=303)


@app.post("/import/controls")
async def import_controls(
    request:     Request,
    course_id:   int = Form(...),
    data:        str = Form(...),
    redirect_to: str = Form("/import"),
):
    """
    Bulk import control points for a course from pasted CSV text.

    Expected format: one control per line, "si_code,points[,name]".
    si_code must be numeric. Points must be numeric if provided.
    Rows with parse errors are skipped and reported in the redirect message.
    db.bulk_import_controls() uses upsert so running the same import twice is safe.
    """
    rows:   list[tuple] = []
    errors: list[str]   = []

    for i, raw in enumerate(data.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            errors.append(f"Line {i}: skipped (empty si_code)")
            continue
        try:
            int(parts[0])   # validate si_code is numeric
        except ValueError:
            errors.append(f"Line {i}: si_code is not a number")
            continue
        if len(parts) > 1 and parts[1]:
            try:
                int(parts[1])   # validate points is numeric
            except ValueError:
                errors.append(f"Line {i}: points is not a number")
                continue
        rows.append(tuple(parts))

    if rows:
        inserted, updated = db.bulk_import_controls(course_id, rows)
        msg = f"Controls: {inserted} added, {updated} updated."
        if errors:
            msg += " Errors: " + "; ".join(errors)
    else:
        msg = "No valid rows found. " + "; ".join(errors)

    safe_redirect = redirect_to if redirect_to.startswith("/") else "/import"
    sep = "&" if "?" in safe_redirect else "?"
    return RedirectResponse(f"{safe_redirect}{sep}msg={msg.replace(' ', '+')}", status_code=303)


# ── MeOS import (two-step: preview → confirm) ─────────────────────────────────

@app.post("/import/meos/preview", response_class=HTMLResponse)
async def meos_preview(request: Request, file: UploadFile = File(...)):
    """
    Step 1 of the MeOS import: parse the .meosxml file and show a checkbox preview.

    Reads the uploaded file, decodes it (utf-8-sig handles BOM from Windows editors),
    parses the MeOS XML structure into courses/teams/controls, then:
      - Stores the parsed data as plain dicts in _meos_pending under a random token.
      - Saves the token in the session to link this preview to the confirm POST.
      - Renders meos_preview.html with the full parsed data so the user can
        deselect courses or teams before confirming the import.

    On parse failure, redirects to /import with an error message.
    _meos_pending is capped at 10 entries to prevent unbounded growth.
    """
    raw = await file.read()
    try:
        # utf-8-sig strips the BOM (byte order mark) that Windows applications
        # sometimes prepend to UTF-8 files — MeOS on Windows can produce these.
        content = raw.decode("utf-8-sig", errors="replace")
        result  = parse_meos_xml(content)
    except ValueError as exc:
        msg = str(exc)[:120].replace(" ", "+")
        return RedirectResponse(f"/import?msg={msg}", status_code=303)

    token = secrets.token_hex(8)
    _meos_pending[token] = result.to_session()

    # Evict oldest entry if over the cap. dict preserves insertion order in Python 3.7+
    # so next(iter()) gives us the oldest key.
    while len(_meos_pending) > 10:
        _meos_pending.pop(next(iter(_meos_pending)))

    request.session["meos_token"] = token

    return templates.TemplateResponse(request, "meos_preview.html", {
        "event_name": result.event_name,
        "courses":    result.courses,
        "teams":      result.teams,
        "warnings":   result.warnings,
        "token":      token,
    })


@app.post("/import/meos/confirm")
async def meos_confirm(request: Request):
    """
    Step 2 of the MeOS import: commit the user's selections to the database.

    Reads the session token to find the pending parsed data, then iterates only
    the selected courses and teams (identified by their MeOS IDs in the checkboxes).

    Import order matters:
      1. Courses + their controls must be created first to get local DB IDs.
      2. Teams (racers) are created/updated next.
      3. Enrollments (entries) link teams to courses — they need both IDs to exist.

    meos_to_local_course maps MeOS course IDs → local DB course IDs so that when
    we process team entries we can resolve which local course to enroll them in.
    Entries whose course was not selected (or not found) are silently skipped.

    db.upsert_racer / db.upsert_course / db.upsert_control are all idempotent —
    re-importing the same MeOS file won't duplicate records.
    """
    token   = request.session.get("meos_token", "")
    pending = _meos_pending.pop(token, None)   # consume the token (one-time use)

    if not pending:
        return RedirectResponse(
            "/import?msg=Session+expired.+Please+upload+the+file+again.",
            status_code=303,
        )

    form               = await request.form()
    selected_course_ids = set(form.getlist("course_ids"))
    selected_club_ids   = set(form.getlist("club_ids"))

    meos_to_local_course: dict[str, int] = {}
    courses_imported = controls_imported = teams_imported = enrollments = 0

    # ── Pass 1: courses and their controls ────────────────────────────────────
    for cd in pending["courses"]:
        if cd["course_id"] not in selected_course_ids:
            continue
        local_id = db.upsert_course(
            cd["name"], cd["time_limit_minutes"], cd["overtime_penalty"]
        )
        meos_to_local_course[cd["course_id"]] = local_id
        courses_imported += 1

        for ctrl in cd["controls"]:
            db.upsert_control(
                local_id, ctrl["si_code"], ctrl["name"], ctrl["points"],
                is_mandatory=False, mandatory_order=None,
            )
            controls_imported += 1

    # ── Pass 2: teams and their enrollments ───────────────────────────────────
    for td in pending["teams"]:
        if td["club_id"] not in selected_club_ids:
            continue
        local_racer_id = db.upsert_racer(td["name"], td["bib"])
        teams_imported += 1

        for entry in td["entries"]:
            if entry["course_id"] in meos_to_local_course:
                local_course_id = meos_to_local_course[entry["course_id"]]
                db.enroll_racer(local_racer_id, local_course_id, entry["si_chip"])
                enrollments += 1

    msg = (
        f"Imported+{courses_imported}+courses,+"
        f"{controls_imported}+controls,+"
        f"{teams_imported}+teams,+"
        f"{enrollments}+enrollments."
    )
    return RedirectResponse(f"/import?msg={msg}", status_code=303)


# ── Team bulk import (CSV with fuzzy-match preview) ───────────────────────────

import difflib as _difflib


def _parse_teams_csv(text: str) -> list[dict]:
    """
    Parse CSV text into rows of {bib, name, class_name}.
    Supports 1-column (name), 2-column (bib, name), or 3-column (bib, name, class).
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",", 2)]
        if len(parts) == 1:
            bib, name, class_name = "", parts[0], ""
        elif len(parts) == 2:
            bib, name, class_name = parts[0], parts[1], ""
        else:
            bib, name, class_name = parts[0], parts[1], parts[2]
        if name:
            rows.append({"bib": bib, "name": name, "class_name": class_name})
    return rows


def _analyze_teams(rows: list[dict], existing_racers, existing_classes) -> list[dict]:
    """
    Annotate each CSV row with fuzzy-match suggestions for team name and class.

    Team names:  flags exact duplicates; finds similar existing names (≥75% similarity).
    Class names: auto-corrects to the closest existing class (≥65% similarity).
    """
    racer_map = {r["name"].lower(): {"id": r["id"], "name": r["name"]} for r in existing_racers}
    class_map = {c["name"].lower(): c["name"] for c in existing_classes}

    out = []
    for row in rows:
        nl = row["name"].lower()

        exact_r = racer_map.get(nl)

        fuzzy_teams = []
        if not exact_r:
            for rname_l, r in racer_map.items():
                score = _difflib.SequenceMatcher(None, nl, rname_l).ratio()
                if 0.75 <= score < 1.0:
                    fuzzy_teams.append({"name": r["name"], "id": r["id"], "score": round(score * 100)})
            fuzzy_teams.sort(key=lambda x: -x["score"])
            fuzzy_teams = fuzzy_teams[:3]

        # Class: auto-correct spelling toward nearest existing class.
        cl = row["class_name"]
        cl_lower = cl.lower()
        resolved_class = cl
        class_corrected = False
        if cl:
            if cl_lower in class_map:
                resolved_class = class_map[cl_lower]   # normalise casing
            else:
                matches = _difflib.get_close_matches(cl_lower, list(class_map.keys()), n=1, cutoff=0.65)
                if matches:
                    resolved_class = class_map[matches[0]]
                    class_corrected = True

        out.append({
            "bib":             row["bib"],
            "name":            row["name"],
            "class_name":      cl,
            "resolved_class":  resolved_class,
            "class_corrected": class_corrected,
            "exact_racer_id":  exact_r["id"]   if exact_r else None,
            "exact_racer_name": exact_r["name"] if exact_r else None,
            "fuzzy_teams":     fuzzy_teams,
        })
    return out


@app.post("/import/teams/preview", response_class=HTMLResponse)
async def import_teams_preview(request: Request, data: str = Form("")):
    """
    Step 1: parse the pasted CSV and show a per-row preview with fuzzy-match suggestions.
    Teams already in the DB are flagged; similar names show a merge option.
    """
    rows = _parse_teams_csv(data)
    if not rows:
        return RedirectResponse("/import?msg=No+valid+rows+found+in+CSV.", status_code=303)

    existing_racers  = db.get_all_racers()
    existing_classes = db.get_all_classes()
    analyzed = _analyze_teams(rows, existing_racers, existing_classes)

    token = secrets.token_hex(8)
    _teams_pending[token] = {"rows": analyzed}
    while len(_teams_pending) > 10:
        _teams_pending.pop(next(iter(_teams_pending)))
    request.session["teams_import_token"] = token

    return templates.TemplateResponse(request, "teams_import_preview.html", {
        "rows":     analyzed,
        "token":    token,
        "n_new":    sum(1 for r in analyzed if not r["exact_racer_id"] and not r["fuzzy_teams"]),
        "n_exists": sum(1 for r in analyzed if r["exact_racer_id"]),
        "n_fuzzy":  sum(1 for r in analyzed if r["fuzzy_teams"]),
    })


@app.post("/import/teams/confirm")
async def import_teams_confirm(request: Request):
    """
    Step 2: commit the user-reviewed rows.

    For each included row:
      - resolve=create  → upsert_racer with CSV name/bib/class (new row)
      - resolve=update:{id} → upsert_racer updating the existing team's bib and class
    After saving, enrols the racer in any courses linked to their class.
    """
    token   = request.session.get("teams_import_token", "")
    pending = _teams_pending.pop(token, None)

    if not pending:
        return RedirectResponse("/import?msg=Session+expired.+Upload+again.", status_code=303)

    form = await request.form()

    created = updated = skipped = enrollments = 0

    for i, row in enumerate(pending["rows"]):
        if form.get(f"include_{i}") != "1":
            skipped += 1
            continue

        resolve    = form.get(f"resolve_{i}", "create")
        class_name = (form.get(f"class_{i}") or "").strip()
        class_id   = db.get_or_create_class(class_name) if class_name else None

        if resolve.startswith("update:"):
            try:
                target_id = int(resolve.split(":", 1)[1])
            except ValueError:
                continue
            target = db.get_racer(target_id)
            if target is None:
                continue
            db.upsert_racer(
                target["name"],
                row["bib"] or target["bib_number"],
                class_id=class_id,
                racer_id=target_id,
            )
            racer_id = target_id
            updated += 1
        else:
            racer_id = db.upsert_racer(row["name"], row["bib"], class_id=class_id)
            created += 1

        if class_id:
            for course_id in db.get_courses_for_class(class_id):
                db.enroll_racer(racer_id, course_id)
                enrollments += 1

    parts = []
    if created:    parts.append(f"{created}+created")
    if updated:    parts.append(f"{updated}+updated")
    if skipped:    parts.append(f"{skipped}+skipped")
    if enrollments: parts.append(f"{enrollments}+course+enrollments")
    return RedirectResponse(f"/racers?msg={'+'.join(parts)}", status_code=303)


# ── Server setup ──────────────────────────────────────────────────────────────

@app.get("/server-setup", response_class=HTMLResponse)
async def server_setup(request: Request):
    """
    Static informational page explaining how to configure the remote results server
    (server_url and api_key in Settings). No form submission here — the settings
    are saved via /settings POST.
    """
    return templates.TemplateResponse(request, "server_setup.html", {})


# ── Simulate chip read ────────────────────────────────────────────────────────

@app.post("/api/simulate-read")
async def simulate_read(si_chip: int = Form(...)):
    """
    Trigger a fake chip read for testing without a physical SI station.

    Calls on_chip_read with an empty punch list and no finish time — this exercises
    the full scoring/receipt pipeline. Useful for verifying that a team's entry is
    set up correctly (chip assigned, course configured, controls defined).
    """
    on_chip_read(si_chip, [], None)
    return RedirectResponse(f"/?msg=Simulated+read+chip+{si_chip}", status_code=303)


# ── Chip library ─────────────────────────────────────────────────────────────

@app.get("/chips", response_class=HTMLResponse)
async def chips_page(request: Request, msg: str = ""):
    """Chip library management and bulk chip assignment to race entries."""
    chips   = sorted(db.get_all_chips(), key=lambda c: _natural_sort_key(c['name']))
    entries = sorted(db.get_all_entries_overview(),
                     key=lambda e: (_natural_sort_key(e['course_name']),
                                    _natural_sort_key(e['bib_number']),
                                    _natural_sort_key(e['racer_name'])))
    courses = sorted(db.get_all_courses(), key=lambda c: _natural_sort_key(c['name']))
    return templates.TemplateResponse(request, "chips.html", {
        "chips":   chips,
        "entries": entries,
        "courses": courses,
        "msg":     msg,
    })


@app.post("/chips/add")
async def chip_add(
    name:    str = Form(...),
    si_code: int = Form(...),
    chip_id: Optional[int] = Form(None),
):
    """Add a new chip to the library or rename / re-code an existing one."""
    try:
        db.upsert_chip(name.strip(), si_code, chip_id or None)
    except Exception as exc:
        return RedirectResponse(f"/chips?msg={exc}", status_code=303)
    action = "updated" if chip_id else "added"
    return RedirectResponse(f"/chips?msg=Chip+{action}", status_code=303)


@app.post("/chips/{chip_id}/delete")
async def chip_delete(chip_id: int):
    """Remove a chip from the library."""
    db.delete_chip(chip_id)
    return RedirectResponse("/chips?msg=Chip+deleted", status_code=303)


@app.post("/chips/import")
async def chip_import(csv_text: str = Form("")):
    """
    Bulk-import chips from a pasted CSV (name, si_code  OR  si_code, name per line).

    Inserts new chips and updates existing ones (matched by SI code).
    Skips blank lines and lines that can't be parsed.
    """
    inserted = updated = errors = 0
    for raw in csv_text.strip().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            errors += 1
            continue
        try:
            # Accept "name, si_code" or "si_code, name" — detect by trying int(first).
            try:
                si_code = int(parts[0])
                name = ",".join(parts[1:]).strip()
            except ValueError:
                name = parts[0]
                si_code = int(parts[-1])
            if not name:
                errors += 1
                continue
            existing = db.get_chip_by_si(si_code)
            if existing:
                db.upsert_chip(name, si_code, existing["id"])
                updated += 1
            else:
                db.upsert_chip(name, si_code)
                inserted += 1
        except Exception:
            errors += 1

    msg = f"{inserted} added, {updated} updated"
    if errors:
        msg += f", {errors} skipped"
    from urllib.parse import quote_plus
    return RedirectResponse(f"/chips?msg={quote_plus(msg)}", status_code=303)


@app.post("/chips/auto-assign")
async def chip_auto_assign():
    """
    Auto-assign library chips to race entries that have no chip yet.

    Chip uniqueness is enforced globally: a chip already assigned to ANY entry
    on ANY course is excluded from the pool. This prevents two racers ever
    carrying the same physical chip number, which would cause their punches to
    be merged incorrectly.

    Assignment order within each course: entries in bib-number / name order
    (as returned by get_all_entries_overview), chips in chip-name order
    (as returned by get_all_chips).
    """
    all_chips   = db.get_all_chips()   # ordered by name
    all_entries = db.get_all_entries_overview()

    # Build the globally-used set first — any chip already assigned anywhere
    # is off-limits for the entire auto-assign run.
    used_globally: set[int] = {e["si_chip"] for e in all_entries if e["si_chip"]}

    # Collect blank entry IDs per course (already in bib/name order from the query).
    blank_by_course: dict[int, list[int]] = {}
    for e in all_entries:
        if not e["si_chip"]:
            blank_by_course.setdefault(e["course_id"], []).append(e["id"])

    assigned = 0
    for entry_ids in blank_by_course.values():
        # Recompute available chips each iteration so chips assigned in an
        # earlier course aren't offered again in a later one.
        available = [ch["si_code"] for ch in all_chips if ch["si_code"] not in used_globally]
        for entry_id, si_code in zip(entry_ids, available):
            db.set_entry_chip(entry_id, si_code)
            used_globally.add(si_code)
            assigned += 1

    from urllib.parse import quote_plus
    return RedirectResponse(
        f"/chips?msg={quote_plus(str(assigned) + ' chip(s) auto-assigned')}", status_code=303
    )


@app.post("/chips/clear-assignments")
async def chip_clear_assignments(course_id: Optional[int] = Form(None)):
    """
    Clear chip assignments (set si_chip=NULL) for all entries or one course.

    Used to reset before re-running auto-assign, or to fix a mis-assignment.
    """
    from urllib.parse import quote_plus
    db.clear_chip_assignments(course_id or None)
    if course_id:
        course = db.get_course(course_id)
        label  = course["name"] if course else f"course {course_id}"
        msg    = f"Chip assignments cleared for {label}"
    else:
        msg = "All chip assignments cleared"
    return RedirectResponse(f"/chips?msg={quote_plus(msg)}", status_code=303)


@app.post("/chips/bulk-assign")
async def chip_bulk_assign(request: Request):
    """
    Assign chips to race entries in bulk.

    Reads form fields named `chip_{entry_id}` (the SI code to assign).
    Empty or unchanged fields are skipped.
    """
    form = await request.form()
    updated = 0
    for key, val in form.items():
        if not key.startswith("chip_"):
            continue
        entry_id = int(key[5:])
        val = str(val).strip()
        if not val:
            db.set_entry_chip(entry_id, None)
        else:
            try:
                db.set_entry_chip(entry_id, int(val))
                updated += 1
            except (ValueError, TypeError):
                pass
    return RedirectResponse(f"/chips?msg={updated}+chip+assignment(s)+saved", status_code=303)


def _analyze_chip_assignment_csv(
    text: str,
    all_racers: list,
    entries_overview: list,
    existing_chips: dict,   # {si_code: Row}
) -> list[dict]:
    """
    Parse a chip-assignment CSV and produce a row-by-row import plan.

    Expected columns (headers are detected by keyword, order doesn't matter):
      Team Name  | Class | <Activity> Chip Name | <Activity> Chip SI Code | ...

    Activity keywords recognised: hike, paddle, bike.
    Each activity may have 0, 1, or 2 columns (name + SI code).  Only the SI
    code column is required; if no name column exists, the SI code is used as
    the chip name.

    Returns a list of dicts, one per data row, describing what would happen.
    """
    import csv as _csv, io as _io, difflib as _df

    # ── Parse ─────────────────────────────────────────────────────────────────
    # Accept comma or tab-delimited (Excel copy-paste is tab-separated).
    text = text.strip()
    if not text:
        return []
    sample = text[:2048]
    try:
        dialect = _csv.Sniffer().sniff(sample, delimiters=",\t")
    except _csv.Error:
        dialect = _csv.excel
    reader = _csv.reader(_io.StringIO(text), dialect)
    rows   = [r for r in reader if any(c.strip() for c in r)]
    if len(rows) < 2:
        return []

    headers   = rows[0]
    data_rows = rows[1:]

    # ── Detect columns ────────────────────────────────────────────────────────
    # Find the team-name column (first column whose header contains "team" or "name").
    team_col = 0
    for i, h in enumerate(headers):
        if "team" in h.lower() or ("name" in h.lower() and "chip" not in h.lower()):
            team_col = i
            break

    # For each activity keyword find the chip-name column and SI-code column.
    ACTIVITY_KW = ["hike", "paddle", "bike"]
    activity_cols: dict[str, dict] = {}
    for kw in ACTIVITY_KW:
        name_idx = si_idx = None
        for i, h in enumerate(headers):
            hl = h.lower()
            if kw not in hl:
                continue
            if "name" in hl:
                name_idx = i
            elif "si" in hl or "code" in hl:
                si_idx = i
        if si_idx is not None:
            activity_cols[kw] = {"name_idx": name_idx, "si_idx": si_idx}

    # ── Build lookups ─────────────────────────────────────────────────────────
    racer_names    = [r["name"] for r in all_racers]
    racer_by_lower = {r["name"].lower(): r for r in all_racers}

    entries_by_racer: dict[int, list] = {}
    for e in entries_overview:
        entries_by_racer.setdefault(e["racer_id"], []).append(e)

    # ── Analyse each data row ─────────────────────────────────────────────────
    plan = []
    for row in data_rows:
        team_name = row[team_col].strip() if team_col < len(row) else ""
        if not team_name:
            continue

        item: dict = {
            "team_name":   team_name,
            "racer_id":    None,
            "racer_name":  None,
            "match_type":  None,
            "match_score": None,
            "error":       None,
            "assignments": [],
        }

        # ── Racer matching ────────────────────────────────────────────────────
        lower = team_name.lower()
        if lower in racer_by_lower:
            r = racer_by_lower[lower]
            item.update(racer_id=r["id"], racer_name=r["name"],
                        match_type="exact", match_score=1.0)
        else:
            candidates = _df.get_close_matches(
                lower, [n.lower() for n in racer_names], n=1, cutoff=0.55
            )
            if candidates:
                matched_lower = candidates[0]
                r = racer_by_lower[matched_lower]
                score = _df.SequenceMatcher(None, lower, matched_lower).ratio()
                item.update(racer_id=r["id"], racer_name=r["name"],
                            match_type="fuzzy", match_score=round(score, 2))
            else:
                item["error"] = f"Racer not found"
                plan.append(item)
                continue

        racer_entries = entries_by_racer.get(item["racer_id"], [])
        if not racer_entries:
            item["error"] = "Racer has no course enrollments"
            plan.append(item)
            continue

        # ── Activity assignments ──────────────────────────────────────────────
        for activity, cols in activity_cols.items():
            si_idx   = cols["si_idx"]
            name_idx = cols.get("name_idx")

            si_val   = str(row[si_idx]).strip()   if si_idx   is not None and si_idx   < len(row) else ""
            name_val = str(row[name_idx]).strip() if name_idx is not None and name_idx < len(row) else ""

            if not si_val:
                continue

            # Excel sometimes exports integers as "2031141.0" — strip the decimal.
            try:
                chip_si = int(float(si_val))
            except ValueError:
                item["assignments"].append({
                    "activity": activity, "chip_name": name_val, "chip_si": si_val,
                    "chip_in_library": False, "entry_id": None, "course_name": None,
                    "ok": False, "skip_reason": f"Invalid SI code: {si_val!r}",
                })
                continue

            chip_name = name_val if name_val else str(chip_si)

            # Match the racer's enrolled course by activity keyword in course name.
            matched_entry = next(
                (e for e in racer_entries if activity in e["course_name"].lower()),
                None,
            )

            if matched_entry is None:
                item["assignments"].append({
                    "activity": activity, "chip_name": chip_name, "chip_si": chip_si,
                    "chip_in_library": chip_si in existing_chips,
                    "entry_id": None, "course_name": None,
                    "ok": False,
                    "skip_reason": (
                        f"No course containing '{activity}' in racer's enrollments "
                        f"({', '.join(e['course_name'] for e in racer_entries)})"
                    ),
                })
            else:
                item["assignments"].append({
                    "activity": activity, "chip_name": chip_name, "chip_si": chip_si,
                    "chip_in_library": chip_si in existing_chips,
                    "entry_id": matched_entry["id"],
                    "course_name": matched_entry["course_name"],
                    "ok": True, "skip_reason": None,
                })

        plan.append(item)
    return plan


@app.post("/chips/import-assignments/preview", response_class=HTMLResponse)
async def chip_import_preview(request: Request, csv_text: str = Form("")):
    """
    Parse a chip-assignment CSV, analyse each row, and render a preview for
    the user to review before committing.
    """
    import secrets as _sec
    existing_chips = {ch["si_code"]: ch for ch in db.get_all_chips()}
    plan = _analyze_chip_assignment_csv(
        csv_text,
        db.get_all_racers(),
        db.get_all_entries_overview(),
        existing_chips,
    )
    token = _sec.token_hex(8)
    _chip_import_pending[token] = plan
    if len(_chip_import_pending) > 10:
        _chip_import_pending.pop(next(iter(_chip_import_pending)))

    n_ok    = sum(1 for r in plan if not r["error"] and any(a["ok"] for a in r["assignments"]))
    n_warn  = sum(1 for r in plan if r["match_type"] == "fuzzy")
    n_error = sum(1 for r in plan if r["error"])
    return templates.TemplateResponse(request, "chips_import_preview.html", {
        "plan": plan, "token": token,
        "n_ok": n_ok, "n_warn": n_warn, "n_error": n_error,
    })


@app.post("/chips/import-assignments/confirm")
async def chip_import_confirm(request: Request, token: str = Form(...)):
    """
    Commit a previously-previewed chip assignment import.

    Only rows where the user checked the include checkbox are processed.
    For each included assignment: upserts the chip to the library, then
    assigns it to the matched course entry.
    """
    from urllib.parse import quote_plus
    plan = _chip_import_pending.pop(token, None)
    if not plan:
        return RedirectResponse("/chips?msg=Import+session+expired", status_code=303)

    form = await request.form()
    chips_created = chips_updated = assigned = 0

    for i, item in enumerate(plan):
        if item.get("error"):
            continue
        if f"include_{i}" not in form:
            continue
        for asgn in item["assignments"]:
            if not asgn["ok"]:
                continue
            existing = db.get_chip_by_si(asgn["chip_si"])
            if existing:
                db.upsert_chip(asgn["chip_name"], asgn["chip_si"], existing["id"])
                chips_updated += 1
            else:
                db.upsert_chip(asgn["chip_name"], asgn["chip_si"])
                chips_created += 1
            db.set_entry_chip(asgn["entry_id"], asgn["chip_si"])
            assigned += 1

    msg = f"{chips_created} chip(s) added to library, {chips_updated} updated, {assigned} assigned"
    return RedirectResponse(f"/chips?msg={quote_plus(msg)}", status_code=303)


@app.post("/api/assign-chip-read")
async def assign_chip_read(
    si_chip:  int = Form(...),
    entry_id: int = Form(...),
):
    """
    Assign an unknown chip read to a specific race entry and replay the chip read.

    Used from the dashboard "Unassigned Reads" panel when an unrecognised chip is
    read. Assigns the chip to the chosen entry, then calls on_chip_read so the
    existing punch/scoring pipeline processes it as normal.
    Removes the read from the _unknown_reads queue on success.
    """
    read = _unknown_reads.get(si_chip)
    if not read:
        return RedirectResponse("/?msg=Read+expired+or+already+assigned", status_code=303)

    db.set_entry_chip(entry_id, si_chip)
    on_chip_read(
        si_chip,
        read["punches"],
        read["finish_time"],
        read["chip_start_time"],
    )
    _unknown_reads.pop(si_chip, None)
    return RedirectResponse("/?msg=Chip+assigned+and+scored", status_code=303)


# ── Public JSON API ───────────────────────────────────────────────────────────

@app.get("/api/results")
async def api_results():
    """
    Machine-readable results endpoint — consumed by the remote results server sync
    and by any external scoreboard that wants to display live results.

    Listed in _PUBLIC_PREFIXES so no authentication is required. Returns all
    ranked results as a flat JSON array, one object per entry.
    """
    ranked = rank_results(db.load_all_results())
    return JSONResponse([
        {
            "rank":            rank,
            "bib":             r.racer.bib_number,
            "team":            r.racer.name,
            "course":          r.course.name,
            "total_points":    r.total_points,
            "elapsed_seconds": r.elapsed_seconds,
            "status":          r.entry.status.value,
        }
        for rank, r in ranked
    ])
