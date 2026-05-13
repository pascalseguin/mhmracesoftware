"""
Path helpers that work both when running from source and inside a PyInstaller .exe.

PyInstaller bundles the app into a single .exe. At runtime it extracts read-only
files (templates, static assets) into a temporary folder (sys._MEIPASS) and runs
the code from there. Writable files (database, config, logs, receipts) must go
next to the .exe, not in the temp folder which is deleted on exit.

Two helpers handle this split:
  resource_path() — read-only bundled files  → sys._MEIPASS when frozen
  data_path()     — writable runtime files   → directory of the .exe when frozen
"""
import sys
from pathlib import Path


def resource_path(*parts: str) -> Path:
    """
    Return the path to a bundled read-only resource (templates, static files).

    When frozen (running as .exe):  base = sys._MEIPASS  (PyInstaller temp dir)
    When running from source:       base = this file's directory (client/)
    """
    if getattr(sys, "frozen", False):
        # sys._MEIPASS is set by PyInstaller at runtime to the extraction temp dir.
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).parent
    return base.joinpath(*parts)


def data_path(*parts: str) -> Path:
    """
    Return the path to a writable data file (DB, config, logs, receipt files).

    When frozen:         next to the .exe  (e.g. C:/MHM/mhm_local.db)
    When running source: next to client/   (e.g. client/mhm_local.db)

    Parent directories are created automatically so callers don't need to mkdir.
    """
    if getattr(sys, "frozen", False):
        # sys.executable is the .exe itself; its parent is the install directory.
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    path = base.joinpath(*parts)
    # Ensure the parent directory exists before the caller tries to open the file.
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
