# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for MHM Race Management."""

from pathlib import Path
ROOT = Path(SPECPATH)

block_cipher = None

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # resource_path("templates") and resource_path("static") look for these
        # at the root of sys._MEIPASS, so bundle without the client/ prefix.
        (str(ROOT / "client" / "templates"), "templates"),
        (str(ROOT / "client" / "static"),    "static"),
    ],
    hiddenimports=[
        # FastAPI / Starlette internals
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "starlette.routing",
        "starlette.staticfiles",
        "starlette.templating",
        "starlette.middleware",
        "starlette.middleware.base",
        "starlette.middleware.sessions",
        "itsdangerous",
        "itsdangerous.url_safe",
        "anyio",
        "anyio._backends._asyncio",
        # Jinja2
        "jinja2",
        "jinja2.ext",
        "markupsafe",
        # pystray backends
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        # python-escpos
        "escpos",
        "escpos.printer",
        # sportident
        "sportident",
        # pyserial
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        # requests
        "requests",
        "urllib3",
        # app packages
        "client",
        "client.app",
        "client.database",
        "client.si_reader",
        "client.printer",
        "client.sync",
        "client.config",
        "client.utils",
        "shared",
        "shared.models",
        "shared.scoring",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MHM-Race",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no console window — tray icon only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="client/static/mhm.ico",   # uncomment if you add a .ico file
)
