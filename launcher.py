"""
MHM Race Management -- launcher entry point.

Starts the local FastAPI server and shows a system-tray icon.
Right-click the tray icon to open the browser or quit.
"""
import sys
import os
import threading
import webbrowser
import time
import logging
import traceback

# ── Frozen-exe path fix ───────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _bundle = sys._MEIPASS  # type: ignore[attr-defined]
    if _bundle not in sys.path:
        sys.path.insert(0, _bundle)
    os.chdir(os.path.dirname(sys.executable))

# ── Log file (always written next to the exe) ─────────────────────────────────
if getattr(sys, "frozen", False):
    _log_path = os.path.join(os.path.dirname(sys.executable), "mhm.log")
else:
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mhm.log")

_file_handler = logging.FileHandler(_log_path, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

logging.root.setLevel(logging.INFO)
logging.root.addHandler(_file_handler)
# Also log to stdout when not frozen (dev mode)
if not getattr(sys, "frozen", False):
    logging.root.addHandler(logging.StreamHandler(sys.stdout))

log = logging.getLogger("launcher")
log.info("=== MHM Race starting === log: %s", _log_path)


# ── Catch uncaught thread exceptions ─────────────────────────────────────────
def _thread_excepthook(args):
    log.error(
        "Uncaught exception in thread %s:\n%s",
        args.thread.name if args.thread else "unknown",
        "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
    )

threading.excepthook = _thread_excepthook


PORT = 8080
URL = f"http://localhost:{PORT}"
_server_started = threading.Event()
_server_failed = threading.Event()


# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_icon():
    try:
        from PIL import Image, ImageDraw  # type: ignore
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([0, 0, size - 1, size - 1], fill="#c0392b")
        pts = [(10, 50), (10, 14), (32, 36), (54, 14), (54, 50)]
        draw.line(pts, fill="white", width=7)
        return img
    except Exception as e:
        log.warning("Could not create tray icon image: %s", e)
        return None


# ── Server thread ─────────────────────────────────────────────────────────────

def _run_server():
    log.info("Server thread started")
    try:
        import uvicorn
        log.info("uvicorn imported OK")
        from client.app import app
        log.info("client.app imported OK")

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=PORT,
            log_level="info",
            # Disable uvicorn's own log config so our file handler stays active
            log_config=None,
        )
        server = uvicorn.Server(config)
        log.info("Starting uvicorn on port %d", PORT)
        _server_started.set()
        server.run()
        log.info("uvicorn stopped")
    except Exception:
        log.exception("Server failed to start")
        _server_failed.set()
        _server_started.set()  # unblock browser thread


def _open_browser():
    # Wait until server signals it's starting, then give it a moment to bind
    _server_started.wait(timeout=20)
    if _server_failed.is_set():
        log.error("Not opening browser -- server failed. Check mhm.log")
        return
    time.sleep(2)
    log.info("Opening browser: %s", URL)
    webbrowser.open(URL)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Launching on %s", URL)

    server_thread = threading.Thread(target=_run_server, name="uvicorn-server", daemon=True)
    server_thread.start()

    browser_thread = threading.Thread(target=_open_browser, name="browser-opener", daemon=True)
    browser_thread.start()

    try:
        import pystray  # type: ignore

        icon_img = _make_icon()
        if icon_img is None:
            raise ImportError("PIL icon unavailable")

        def on_open(icon, item):
            webbrowser.open(URL)

        def on_quit(icon, item):
            log.info("Quit via tray")
            icon.stop()
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("Open MHM Race", on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )
        tray = pystray.Icon("MHM Race", icon_img, "MHM Race Management", menu)
        log.info("System tray ready")
        tray.run()  # blocks until quit

    except Exception as exc:
        log.warning("Tray unavailable (%s) -- console mode. Press Ctrl+C to stop.", exc)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            log.info("Stopped by user")
            sys.exit(0)


if __name__ == "__main__":
    main()
