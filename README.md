# MHM Race Management System

Local race management client + public results server for the Medicine Hat Massacre (and similar rogaining events).

Copyright (c) 2026 Pascal Hamish Seguin. All rights reserved.

This software and its source code are proprietary and confidential. No part of this
codebase may be reproduced, distributed, or transmitted in any form or by any means,
or used to create derivative works, without the prior written permission of the copyright holder.

A perpetual, royalty-free, non-exclusive license is hereby granted to SEASAR
(South Eastern Alberta Search and Rescue) to use, run, and internally modify this
software solely for the purpose of managing SEASAR events. This license does not
permit SEASAR to sublicense, sell, or redistribute the software or its source code
to any third party.

---

## Architecture

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│  Client (Windows .exe)      │  sync  │  Server (cloud)              │
│  FastAPI + SQLite           │ ──────▶│  FastAPI + SQLite            │
│  SI chip reader             │        │  Public leaderboard          │
│  localhost:8080             │        │  https://your-url.onrender.com│
└─────────────────────────────┘        └──────────────────────────────┘
```

- **Client** — runs on the race laptop. Reads SI chips, scores entries, manages racers/courses/classes, handles adjustments. Web UI on `localhost:8080`. Runs as a system-tray app; right-click the tray icon to open the browser or quit.
- **Server** — runs in the cloud. Receives synced results from the client every 30 seconds and serves a public read-only leaderboard that spectators can view on their phones.

---

## Repository Layout

```
mhm/
├── client/                # Local race management app
│   ├── app.py             # FastAPI routes
│   ├── database.py        # SQLite read/write
│   ├── printer.py         # ESC/POS thermal receipt printing
│   ├── si_reader.py       # SportIdent chip reader
│   ├── sync.py            # Background sync worker
│   ├── static/style.css
│   └── templates/         # Jinja2 HTML templates
├── server/                # Public results server
│   ├── app.py
│   ├── database.py
│   └── templates/
├── shared/                # Models and scoring engine (used by both)
│   ├── models.py
│   └── scoring.py
├── launcher.py            # Entry point for the bundled .exe (tray icon)
├── run_client.py          # Dev entry point: python run_client.py
├── run_server.py          # Dev entry point: python run_server.py
├── mhm.spec               # PyInstaller bundle spec
├── build.ps1              # Build script: produces dist\MHM-Race.exe
├── installer\setup.iss    # Inno Setup script: produces MHM-Race-Setup.exe
├── Procfile               # Used by Render/Railway for cloud deploy
└── requirements-server.txt
```

---

## Client Deployment (Race Laptop)

This is the full process from source code to a `.exe` installer that can be dropped onto any Windows 10/11 race laptop with no other software required.

### Prerequisites (developer machine only — not needed on the race laptop)

| Software | Purpose | Download |
|---|---|---|
| Python 3.11+ | Run the build script | python.org — tick **Add to PATH** |
| Inno Setup 6 | Create the Windows installer | jrsoftware.org/isdl.php |
| Git | Push code to GitHub | git-scm.com |

---

### Step 1 — Allow PowerShell scripts to run (one-time, developer machine)

By default Windows blocks running `.ps1` scripts. Open PowerShell **as Administrator** and run:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope LocalMachine
```

This only affects your developer machine and only needs to be done once. The race laptop does **not** need this change — the installed `.exe` handles everything internally.

---

### Step 2 — Build the executable

Open PowerShell in the `mhm\` folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

The script will:
1. Check Python is installed
2. Install all pip dependencies automatically
3. Run PyInstaller to bundle everything into a single file
4. Output `dist\MHM-Race.exe` (~50–80 MB)

To do a clean build (wipe previous output first):

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1 -Clean
```

**Troubleshooting the build:**
- `Python not found` — install Python 3.11+ from python.org and tick "Add to PATH", then restart PowerShell
- `pip install failed` — run as Administrator or check your internet connection
- `PyInstaller failed` — read the output; usually a missing `hiddenimports` entry in `mhm.spec`
- `pyinstaller not recognized` — this is handled automatically; the script uses `python -m PyInstaller` instead of the bare command
- Windows Defender may quarantine the freshly-built `.exe` — add an exclusion for the `dist\` folder

---

### Step 3 — Create the Windows installer

1. Open **Inno Setup** (installed in Step 0 above)
2. Open `installer\setup.iss`
3. Press **F9** (or Build → Compile)
4. Output: `installer\output\MHM-Race-Setup.exe`

Or from PowerShell:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\setup.iss
```

---

### Step 4 — Install on the race laptop

1. Copy `MHM-Race-Setup.exe` to the race laptop (USB drive, OneDrive, email, etc.)
2. Double-click `MHM-Race-Setup.exe` and follow the wizard
3. The app installs to `C:\Users\<you>\AppData\Local\MHM Race\`
4. A **Desktop shortcut** is created automatically
5. Tick "Launch MHM Race now" at the end of the wizard

The app opens a browser tab at `http://localhost:8080`. Default login: **admin / admin**.

> **First run only:** Windows SmartScreen may show a blue warning ("Windows protected your PC") because the `.exe` is not signed. Click **More info** → **Run anyway**. This only happens once.

---

### Step 5 — Race laptop hardware setup

#### Thermal receipt printer
1. Install the printer driver from the manufacturer (usually a small download)
2. In Windows, go to Settings → Bluetooth & devices → Printers & scanners
3. Confirm the printer appears by name
4. In the MHM app → **System → Settings** → enter the exact printer name → Save
5. Click **Send Test Print** to verify

#### SI chip reader
1. Plug the SportIdent BSM7/BSF8 station into USB
2. Windows automatically installs a COM port driver (check Device Manager if unsure which port)
3. In the MHM app → **System → Settings** → set **SI Port** to the correct COM port (e.g. `COM3`) → Save
4. The SI status dot on the Dashboard turns green when connected

#### Local network access (optional)
If you want judges or volunteers to view the leaderboard on their phones over the event WiFi:

1. Open PowerShell **as Administrator** on the race laptop:
   ```powershell
   netsh advfirewall firewall add rule name="MHM Race" dir=in action=allow protocol=TCP localport=8080
   ```
2. In the MHM app → **System → Settings** → change **Host** from `127.0.0.1` to `0.0.0.0` → Save → restart the app
3. Other devices can now open `http://<race-laptop-IP>:8080/results` in their browser

---

### Upgrading to a new version

1. Run `build.ps1` and `ISCC.exe installer\setup.iss` again on the developer machine
2. Copy the new `MHM-Race-Setup.exe` to the race laptop
3. Run it — the installer automatically replaces the old `.exe`
4. Your race database (`mhm_local.db`) and config (`config.json`) are preserved because they sit in the same folder as the `.exe` and the installer only replaces the `.exe` itself

---

## Running the Client in Development (no installer)

```powershell
pip install -r requirements.txt
python run_client.py
```

Open `http://localhost:8080` — default login is `admin` / `admin`.

---

## Deploying the Server to the Cloud

### Step 1 — Generate an API key

Run this once on any machine with Python:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output (looks like `a3f8c2d1b9e0...`). Save it — you will need it in two places: the server environment variable and the client Settings page.

---

### Step 2 — Put the code on GitHub

1. Go to [github.com](https://github.com) and create a free account if you don't have one.
2. Click **New repository** → name it `mhm-server` → set to **Private** → Create.
3. In PowerShell, from this folder:

```powershell
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/mhm-server.git
git push -u origin main
```

Replace `YOUR-USERNAME` with your GitHub username.

---

### Step 3 — Deploy on Render.com (recommended, free tier)

1. Go to [render.com](https://render.com) and sign up using your GitHub account.
2. Click **New** → **Web Service**.
3. Connect GitHub if prompted → select the `mhm-server` repo.
4. Fill in the fields:

| Field | Value |
|---|---|
| Name | `mhm-results` (or anything) |
| Region | US West or US East (closest to Alberta) |
| Branch | `main` |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements-server.txt` |
| Start Command | `uvicorn server.app:app --host 0.0.0.0 --port $PORT` |

5. Scroll to **Environment Variables** → **Add Environment Variable**:
   - Key: `MHM_API_KEY`
   - Value: the key you generated in Step 1

6. Click **Create Web Service**.

Render builds and deploys in about 2 minutes. When done you get a URL like:

```
https://mhm-results.onrender.com
```

Open it in a browser — you should see the public results page (empty until the client syncs).

---

### Step 4 — Configure the client

1. Open the MHM client on race day.
2. Go to **System → Server Sync**.
3. Set **Server URL** to your Render URL (e.g. `https://mhm-results.onrender.com`).
4. Set **API Key** to the key from Step 1.
5. Click Save. The sync worker pushes results automatically every 30 seconds.

---

### Free Tier Notes

- Render free tier **sleeps after 15 minutes of inactivity**. The first request after idle takes ~30 seconds to wake up. On race day, open the public URL yourself before teams start so it stays warm.
- The SQLite database resets if the service redeploys on the free tier. For persistent results across redeploys, upgrade to Render's $7/month paid instance with a persistent disk, or use Railway instead.

---

### Alternative: Railway.app

Railway has a persistent filesystem on its free tier, so the database survives redeploys.

1. Go to [railway.app](https://railway.app) → sign in with GitHub.
2. **New Project** → **Deploy from GitHub repo** → select your repo.
3. Add environment variable `MHM_API_KEY` → your key from Step 1.
4. Railway auto-detects the `Procfile` and deploys. You get a public URL automatically.
5. Add a **Volume** mounted at `/app/server` so `mhm_server.db` persists between deploys.

---

### Changing the API Key Later

Both sides must always match:

1. **Server (Render):** Service → Environment → update `MHM_API_KEY` → Save (triggers a redeploy).
2. **Client:** System → Server Sync → update the API Key field → Save.
