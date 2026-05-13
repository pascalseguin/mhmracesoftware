"""
Runtime configuration for the MHM local client.

Config is loaded from config.json (next to the .exe / in client/ during development).
Environment variables with the prefix MHM_ override any file value — useful for
CI or running multiple instances with different ports.

The config dict is loaded once at app startup (CFG = cfg_mod.load()) and kept in
memory. Individual settings are updated in-place and written back to disk via save().
"""
import json
import os
from pathlib import Path
from client.utils import data_path

# config.json lives in the writable data directory (next to the .exe when frozen,
# or next to client/ when running from source).
_cfg_path = data_path("config.json")

# All keys and their factory defaults.
# New keys added here automatically appear in fresh installs without migration.
_defaults = {
    "si_port": "COM3",              # Serial port for the SportIdent readout station
    "printer_name": "",             # Windows printer name for ESC/POS receipt printer
    "server_url": "",               # Remote results server URL (blank = offline mode)
    "api_key": "",                  # API key for authenticating with the remote server
    "sync_interval_seconds": 30,    # How often to push unsynced records to the server
    "host": "127.0.0.1",            # Interface the local web server binds to
    "port": 8080,                   # Port for the local web UI
    "selected_course_id": None,     # If set, SI chip reads are filtered to this course only
    "race_name": "Medicine Hat Massacre",  # Shown in the nav bar and on printed receipts
    "race_year": "2026",            # Shown next to the race name in the nav bar
    # SI Code Number Offset — added to every punch code number before scoring.
    # The Python sportident library reads only 8 bits of the 12-bit CN field, so
    # stations numbered 256–511 come back as (actual - 256). Setting this to 256
    # corrects that. Set to 0 if your stations are numbered 1–255.
    "si_cn_offset": 256,
    # Whether to automatically print a receipt on the thermal printer when a chip
    # is read. True = print if a printer_name is configured; False = skip printing.
    "print_on_read": True,
}


def load() -> dict:
    """
    Load config from disk, then apply any environment overrides.

    Priority (highest to lowest):
      1. MHM_<KEY> environment variables
      2. Values in config.json
      3. _defaults above
    """
    cfg = dict(_defaults)   # start with defaults so new keys are always present

    if _cfg_path.exists():
        try:
            cfg.update(json.loads(_cfg_path.read_text()))
        except Exception:
            # Corrupt or empty config.json — silently fall back to defaults.
            pass

    # Environment variable overrides.
    # e.g. MHM_PORT=9090 overrides cfg["port"].
    for key in cfg:
        env_val = os.environ.get(f"MHM_{key.upper()}")
        if env_val is not None:
            cfg[key] = env_val

    return cfg


def save(cfg: dict):
    """Persist the current config dict to disk as pretty-printed JSON."""
    _cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
