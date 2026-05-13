"""
Generate the MHM Server Deployment Guide as a PDF.
Run:  python generate_server_guide.py
Output: MHM_Server_Deployment_Guide.pdf (same directory as this script)
"""
from fpdf import FPDF
from fpdf.enums import XPos, YPos
import os

OUTPUT = os.path.join(os.path.dirname(__file__), "MHM_Server_Deployment_Guide.pdf")

# -- Colour palette --
BLUE     = (26,  86, 219)
DARK     = (17,  24,  39)
MUTED    = (107, 114, 128)
LIGHT_BG = (243, 244, 246)
GREEN    = (22,  163,  74)
AMBER    = (180, 120,   0)
RED      = (185,  28,  28)
WHITE    = (255, 255, 255)
RULE     = (209, 213, 219)
TEAL     = (13,  148, 136)

# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

OVERVIEW = (
    "The MHM server is a lightweight FastAPI application that acts as a central "
    "results hub during race day. The finish-line laptop (running MHM-Race.exe) "
    "pushes scored results to the server every 30 seconds over a pre-shared API key. "
    "Spectators, volunteers, and remote officials can view live standings at the "
    "server's public URL without needing access to the finish-line machine.\n\n"
    "The server is intentionally minimal: it receives sync payloads, stores them in "
    "a local SQLite database, and serves a read-only results page. It has no SI reader, "
    "no chip scoring, and no admin UI -- all race management stays on the client laptop."
)

ARCHITECTURE = [
    ("Finish-line laptop", "Runs MHM-Race.exe. Reads SI chips, scores results, stores everything in mhm_local.db. Every 30 s it POSTs unsynced records to POST /api/sync on the server."),
    ("Central server", "Receives the sync payload, upserts records into mhm_server.db, and serves GET / (live leaderboard) and GET /api/results (JSON feed)."),
    ("Spectators / displays", "Open the server URL in any browser. No login required. The page auto-refreshes every 30 s."),
    ("Authentication", "A single pre-shared API key (MHM_API_KEY env var). The client sends it as the X-API-Key header. The server rejects any request with a wrong or missing key with HTTP 403."),
]

PREREQS = (
    "All three deployment methods require the same MHM server source files:\n"
    "  server/app.py, server/database.py, server/templates/\n"
    "  shared/models.py, shared/scoring.py\n"
    "  run_server.py, requirements-server.txt\n\n"
    "requirements-server.txt installs: fastapi>=0.111, uvicorn[standard]>=0.29, jinja2>=3.1\n\n"
    "Pick ONE of the three deployment options below. Option A (Windows) is the simplest "
    "for a local/LAN setup. Option B (Raspberry Pi) is good for a permanent low-cost host. "
    "Option C (Azure Web App) is the best choice for a public internet-facing results page."
)

SECTIONS = [
    # ------------------------------------------------------------------
    {
        "title": "Option A -- Windows Host (LAN / same network)",
        "label": "A",
        "color": DARK,
        "body": (
            "Best for: club events where spectators are on the same Wi-Fi network as the server, "
            "or where the server is a second laptop connected to a local router.\n\n"
            "The server URL will be something like http://192.168.1.50:8000 -- only reachable "
            "from devices on the same network."
        ),
        "steps": [
            ("Install Python 3.11+",
             "Download from python.org and install. Tick 'Add Python to PATH' during setup.\n"
             "Verify: open a Command Prompt and run: python --version"),
            ("Copy the server files",
             "Copy the following folders/files from the MHM project to the server machine "
             "(USB stick, shared drive, or git clone):\n"
             "  server/\n"
             "  shared/\n"
             "  run_server.py\n"
             "  requirements-server.txt"),
            ("Install dependencies",
             "Open a Command Prompt in the folder containing run_server.py and run:\n"
             "  pip install -r requirements-server.txt"),
            ("Set the API key",
             "In the same Command Prompt, set the environment variable before starting:\n"
             "  set MHM_API_KEY=your-secret-key-here\n\n"
             "Choose a strong random key (e.g. 32+ random characters). Write it down -- "
             "you will enter the same key in the client app's Settings page."),
            ("Start the server",
             "Run:  python run_server.py\n\n"
             "You should see: INFO: Uvicorn running on http://0.0.0.0:8000\n"
             "Open http://localhost:8000 in a browser to confirm the results page loads."),
            ("Find the server's IP address",
             "In a second Command Prompt run:  ipconfig\n"
             "Look for the IPv4 Address under your active network adapter "
             "(e.g. 192.168.1.50). This is the URL clients and spectators will use: "
             "http://192.168.1.50:8000"),
            ("Open Windows Firewall",
             "If other devices cannot reach the server, allow port 8000 inbound:\n"
             "  1. Open Windows Defender Firewall with Advanced Security\n"
             "  2. Inbound Rules -> New Rule -> Port -> TCP -> 8000\n"
             "  3. Allow the connection, apply to all profiles, name it 'MHM Server'"),
            ("Run as a background service (optional)",
             "To keep the server running after closing the Command Prompt, use NSSM "
             "(Non-Sucking Service Manager). Download from nssm.cc, then:\n"
             "  nssm install MHMServer python C:\\path\\to\\run_server.py\n"
             "  nssm set MHMServer AppEnvironmentExtra MHM_API_KEY=your-secret-key\n"
             "  nssm start MHMServer\n\n"
             "The server will now start automatically with Windows."),
        ]
    },
    # ------------------------------------------------------------------
    {
        "title": "Option B -- Raspberry Pi (always-on local/LAN host)",
        "label": "B",
        "color": DARK,
        "body": (
            "Best for: clubs that want a permanent low-cost server that stays on all event "
            "weekend. A Pi 3B+ or newer is more than capable. If the Pi has a public IP "
            "(e.g. via a 4G dongle or port-forwarded router), it can serve spectators over "
            "the internet too."
        ),
        "steps": [
            ("Flash Raspberry Pi OS Lite",
             "Download Raspberry Pi Imager from raspberrypi.com. Flash 'Raspberry Pi OS Lite "
             "(64-bit)' to a microSD card. In Advanced Options, enable SSH and set a hostname "
             "(e.g. mhm-server.local), username, and password before flashing."),
            ("Boot and connect",
             "Insert the SD card, power on the Pi, and wait ~60 s. SSH in from your laptop:\n"
             "  ssh pi@mhm-server.local\n"
             "(Replace 'pi' with the username you set.)"),
            ("Update and install Python",
             "  sudo apt update && sudo apt upgrade -y\n"
             "  sudo apt install -y python3 python3-pip python3-venv git\n\n"
             "Python 3.11+ is included in recent Raspberry Pi OS. Verify: python3 --version"),
            ("Copy or clone the server files",
             "Option 1 -- git clone (recommended if your repo is on GitHub):\n"
             "  git clone https://github.com/YOUR_ORG/mhm.git ~/mhm\n\n"
             "Option 2 -- SCP from your laptop:\n"
             "  scp -r ./server ./shared run_server.py requirements-server.txt \\\n"
             "    pi@mhm-server.local:~/mhm/"),
            ("Create a virtual environment and install deps",
             "  cd ~/mhm\n"
             "  python3 -m venv .venv\n"
             "  source .venv/bin/activate\n"
             "  pip install -r requirements-server.txt"),
            ("Set the API key and test",
             "  export MHM_API_KEY=your-secret-key-here\n"
             "  python run_server.py\n\n"
             "Visit http://mhm-server.local:8000 from another device on the same network "
             "to confirm the page loads. Press Ctrl+C to stop."),
            ("Run as a systemd service",
             "Create /etc/systemd/system/mhm-server.service:\n\n"
             "  [Unit]\n"
             "  Description=MHM Results Server\n"
             "  After=network.target\n\n"
             "  [Service]\n"
             "  User=pi\n"
             "  WorkingDirectory=/home/pi/mhm\n"
             "  Environment=MHM_API_KEY=your-secret-key-here\n"
             "  ExecStart=/home/pi/mhm/.venv/bin/uvicorn server.app:app \\\n"
             "    --host 0.0.0.0 --port 8000\n"
             "  Restart=always\n\n"
             "  [Install]\n"
             "  WantedBy=multi-user.target\n\n"
             "Then enable and start it:\n"
             "  sudo systemctl daemon-reload\n"
             "  sudo systemctl enable mhm-server\n"
             "  sudo systemctl start mhm-server\n"
             "  sudo systemctl status mhm-server"),
            ("Reserve a static IP (optional but recommended)",
             "Log into your router's admin page, find the Pi's MAC address, and assign it "
             "a reserved (static) DHCP lease so its IP never changes. Alternatively edit "
             "/etc/dhcpcd.conf on the Pi to set a static IP directly."),
        ]
    },
    # ------------------------------------------------------------------
    {
        "title": "Option C -- Azure Web App (public internet)",
        "label": "C",
        "color": DARK,
        "body": (
            "Best for: public-facing results where spectators anywhere can view standings "
            "via a real URL (e.g. https://mhm-results.azurewebsites.net). Requires an Azure "
            "account. The Free or Basic tier is sufficient for race-day traffic.\n\n"
            "Azure Web Apps run the server via the Procfile command:\n"
            "  web: uvicorn server.app:app --host 0.0.0.0 --port $PORT"
        ),
        "steps": [
            ("Prerequisites",
             "- Azure account (free.azure.com)\n"
             "- Azure CLI installed: docs.microsoft.com/cli/azure/install-azure-cli\n"
             "- Git repository containing the MHM server files (GitHub, Azure DevOps, etc.)\n"
             "- The Procfile and requirements-server.txt must be in the repo root"),
            ("Log in to Azure CLI",
             "  az login\n\n"
             "A browser window opens. Sign in with your Azure account."),
            ("Create a Resource Group",
             "  az group create --name mhm-rg --location canadacentral\n\n"
             "Choose a location close to your event (canadacentral, eastus, westus2, etc.)."),
            ("Create an App Service Plan (Free tier)",
             "  az appservice plan create \\\n"
             "    --name mhm-plan \\\n"
             "    --resource-group mhm-rg \\\n"
             "    --sku F1 \\\n"
             "    --is-linux\n\n"
             "F1 is the free tier. Upgrade to B1 (~$13/month) for a custom domain and "
             "always-on (prevents cold starts). B1 is recommended for race day."),
            ("Create the Web App",
             "  az webapp create \\\n"
             "    --name mhm-results \\\n"
             "    --resource-group mhm-rg \\\n"
             "    --plan mhm-plan \\\n"
             "    --runtime \"PYTHON:3.12\"\n\n"
             "Your app URL will be: https://mhm-results.azurewebsites.net\n"
             "Choose a unique name -- it must be globally unique on azurewebsites.net."),
            ("Set the API key as an App Setting",
             "  az webapp config appsettings set \\\n"
             "    --name mhm-results \\\n"
             "    --resource-group mhm-rg \\\n"
             "    --settings MHM_API_KEY=your-secret-key-here\n\n"
             "App Settings are injected as environment variables at runtime. "
             "Never put the API key in source code or commit it to git."),
            ("Set the startup command",
             "  az webapp config set \\\n"
             "    --name mhm-results \\\n"
             "    --resource-group mhm-rg \\\n"
             "    --startup-file \"uvicorn server.app:app --host 0.0.0.0 --port 8000\"\n\n"
             "Alternatively, Azure will auto-detect the Procfile if it is in the repo root."),
            ("Deploy via Git (local git method)",
             "  # Configure deployment credentials\n"
             "  az webapp deployment user set \\\n"
             "    --user-name mhmadmin --password YourDeployPassword1!\n\n"
             "  # Get the git remote URL\n"
             "  az webapp deployment source config-local-git \\\n"
             "    --name mhm-results --resource-group mhm-rg\n\n"
             "  # Push your code\n"
             "  git remote add azure <URL from above command>\n"
             "  git push azure main\n\n"
             "Azure will install dependencies from requirements-server.txt and start the app."),
            ("Alternatively: deploy via GitHub Actions",
             "In the Azure Portal, go to your Web App -> Deployment Center -> GitHub. "
             "Authorize Azure, select your repo and branch, and click Save. Azure creates "
             "a GitHub Actions workflow that deploys automatically on every push to main."),
            ("Verify the deployment",
             "Visit https://mhm-results.azurewebsites.net in a browser. You should see "
             "the MHM results page (empty until the client syncs data).\n\n"
             "Check logs if something is wrong:\n"
             "  az webapp log tail --name mhm-results --resource-group mhm-rg"),
            ("Persistent database note",
             "By default, Azure Web Apps use ephemeral local storage -- the SQLite database "
             "is lost on each restart or redeploy.\n\n"
             "Fix: mount an Azure Files share as persistent storage:\n"
             "  az webapp config storage-account add \\\n"
             "    --name mhm-results --resource-group mhm-rg \\\n"
             "    --custom-id mhm-data \\\n"
             "    --storage-type AzureFiles \\\n"
             "    --account-name <storage-account> \\\n"
             "    --share-name mhm-db \\\n"
             "    --mount-path /app/server\n\n"
             "Then update server/database.py to point DB_PATH to /app/server/mhm_server.db "
             "when running on Azure (check for WEBSITE_SITE_NAME env var)."),
            ("Custom domain (optional)",
             "Upgrade the App Service Plan to B1 first (free tier doesn't support custom domains).\n"
             "Then in the Azure Portal -> your Web App -> Custom Domains -> Add Custom Domain. "
             "Point your DNS provider's CNAME record to mhm-results.azurewebsites.net. "
             "Azure can also provision a free managed TLS certificate automatically."),
        ]
    },
    # ------------------------------------------------------------------
    {
        "title": "Connecting the Client to the Server",
        "label": None,
        "color": DARK,
        "body": (
            "Once the server is running, configure the finish-line laptop to push data to it. "
            "All settings are in the MHM client's Settings page."
        ),
        "steps": [
            ("Open Settings on the finish-line laptop",
             "Launch MHM-Race.exe and click Settings in the top navigation."),
            ("Enter the Server URL",
             "In the 'Server URL' field, enter the full URL of the server:\n"
             "  Windows LAN:    http://192.168.1.50:8000\n"
             "  Raspberry Pi:   http://mhm-server.local:8000\n"
             "  Azure:          https://mhm-results.azurewebsites.net\n\n"
             "Do NOT include a trailing slash."),
            ("Enter the API Key",
             "In the 'API Key' field, enter the same key you set in MHM_API_KEY on the server. "
             "The client sends this as the X-API-Key header with every sync request."),
            ("Set the sync interval",
             "The default is 30 seconds. Increase to 60 s on a slow or unreliable connection. "
             "Decrease to 10 s if you want near-real-time updates on the public display."),
            ("Save and confirm",
             "Click Save Settings. The Dashboard will show the sync status:\n"
             "  Green dot = connected, syncing to <URL> every N s\n"
             "  No dot / offline = URL is blank or server unreachable\n\n"
             "After the first chip is read and saved, visit the server URL in a browser -- "
             "the team should appear in the results within one sync interval."),
            ("Troubleshoot sync failures",
             "If the Dashboard shows errors or results don't appear on the server:\n"
             "- Check the server URL has no trailing slash and is exactly correct\n"
             "- Confirm the API key matches on both sides (case-sensitive)\n"
             "- Check the server is running (visit its URL directly in a browser)\n"
             "- On Windows LAN: confirm the firewall allows port 8000\n"
             "- On Azure: check logs with: az webapp log tail --name mhm-results --resource-group mhm-rg\n"
             "- Look in mhm.log (same folder as the .exe) for 'Sync failed' warning lines"),
        ]
    },
    # ------------------------------------------------------------------
    {
        "title": "Security & Production Checklist",
        "label": None,
        "color": DARK,
        "body": "",
        "steps": [
            ("API key strength",
             "Use a randomly generated key of at least 32 characters. Example generation:\n"
             "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n\n"
             "Never use the default 'changeme' key in production."),
            ("HTTPS",
             "The sync POST includes the API key in a plain HTTP header. On a local LAN "
             "this is acceptable. For internet-facing deployments (Azure, public Pi), always "
             "use HTTPS. Azure Web Apps provide HTTPS automatically. For a Pi exposed to the "
             "internet, put Nginx + Certbot (Let's Encrypt) in front of uvicorn."),
            ("Nginx reverse proxy for Pi (HTTPS)",
             "  sudo apt install -y nginx certbot python3-certbot-nginx\n"
             "  # Create /etc/nginx/sites-available/mhm with:\n"
             "  #   server { listen 80; server_name results.yourdomain.com;\n"
             "  #     location / { proxy_pass http://127.0.0.1:8000; } }\n"
             "  sudo ln -s /etc/nginx/sites-available/mhm /etc/nginx/sites-enabled/\n"
             "  sudo nginx -t && sudo systemctl reload nginx\n"
             "  sudo certbot --nginx -d results.yourdomain.com"),
            ("Database backups",
             "The server database (mhm_server.db) is a SQLite file. Back it up by copying "
             "it off the host at the end of each day. On Linux:\n"
             "  sqlite3 mhm_server.db '.backup /backup/mhm_server_backup.db'\n\n"
             "The client (mhm_local.db) is the authoritative source -- if the server "
             "database is lost, the client can re-sync all data by resetting the synced "
             "flags (contact developer for the reset script)."),
            ("Keeping the server up to date",
             "If the shared/ scoring logic changes between events, redeploy the server "
             "with the updated files before the event. The server and client must run "
             "the same version of shared/models.py and shared/scoring.py to score correctly."),
            ("Restrict public write access",
             "The /api/sync endpoint is protected by the API key. The public GET / and "
             "GET /api/results endpoints are intentionally unauthenticated -- anyone with "
             "the URL can view results. If you want to restrict viewing, add HTTP Basic Auth "
             "in Nginx or as FastAPI middleware before the event."),
        ]
    },
    # ------------------------------------------------------------------
    {
        "title": "Quick-Reference: Deployment Checklist",
        "label": None,
        "color": DARK,
        "body": "",
        "steps": [
            ("Server setup", (
             "[/] Choose deployment option (Windows / Pi / Azure)\n"
             "[/] Generate a strong API key (python -c \"import secrets; print(secrets.token_urlsafe(32))\")\n"
             "[/] Deploy server files and install requirements-server.txt\n"
             "[/] Set MHM_API_KEY environment variable / App Setting\n"
             "[/] Start the server and confirm the results page loads in a browser\n"
             "[/] (Azure) Attach persistent Azure Files storage for the database\n"
             "[/] (Internet) Configure HTTPS"
            )),
            ("Client configuration", (
             "[/] Open MHM-Race.exe -> Settings\n"
             "[/] Enter server URL (no trailing slash)\n"
             "[/] Enter API key (must match server exactly)\n"
             "[/] Set sync interval (default 30 s)\n"
             "[/] Click Save Settings\n"
             "[/] Read a test chip and confirm the result appears on the server within 30 s"
            )),
            ("On race day", (
             "[/] Confirm server is running before teams start\n"
             "[/] Display server URL on a screen for spectators\n"
             "[/] Monitor client Dashboard -- sync status dot should stay green\n"
             "[/] If sync fails: check mhm.log and server logs\n"
             "[/] Back up mhm_server.db at end of day"
            )),
        ]
    },
]


# ---------------------------------------------------------------------------
# PDF class
# ---------------------------------------------------------------------------

class PDF(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(20, 20, 20)

    def normalize_text(self, text):
        pairs = [
            (u'—', ' - '),
            (u'–', '-'),
            (u'•', '-'),
            (u'‘', "'"),
            (u'’', "'"),
            (u'“', '"'),
            (u'”', '"'),
            (u'…', '...'),
            (u'✓', '[/]'),
            (u'✗', '[x]'),
            (u'→', '->'),
            (u'←', '<-'),
        ]
        for char, repl in pairs:
            text = text.replace(char, repl)
        text = text.encode('latin-1', errors='replace').decode('latin-1')
        return super().normalize_text(text)

    # -- helpers --
    def _rgb(self, c): self.set_text_color(*c)
    def _fill(self, c): self.set_fill_color(*c)
    def _draw(self, c): self.set_draw_color(*c)

    def hline(self, thickness=0.3):
        self._draw(RULE)
        self.set_line_width(thickness)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def body_text(self, text, indent=0):
        self._rgb(DARK)
        self.set_font("Helvetica", size=9.5)
        self.set_x(self.l_margin + indent)
        self.multi_cell(
            w=self.w - self.l_margin - self.r_margin - indent,
            h=5.5, text=text,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )

    def code_text(self, text):
        self._fill(LIGHT_BG)
        self._rgb(DARK)
        self.set_font("Courier", size=8)
        self.set_x(self.l_margin)
        self.multi_cell(
            w=self.w - self.l_margin - self.r_margin,
            h=4.5, text=text, fill=True,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
        self.ln(1)

    def note_text(self, text):
        self._rgb(MUTED)
        self.set_font("Helvetica", "I", 8.5)
        self.set_x(self.l_margin)
        self.multi_cell(
            w=self.w - self.l_margin - self.r_margin,
            h=5, text=text,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )

    # -- header / footer --
    def header(self):
        if self.page_no() == 1:
            return
        self._rgb(MUTED)
        self.set_font("Helvetica", "I", 7.5)
        self.set_y(10)
        self.cell(0, 5, "MHM Server Deployment Guide", align="L",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(14)
        self.hline(0.2)
        self.set_y(18)

    def footer(self):
        self.set_y(-15)
        self._rgb(MUTED)
        self.set_font("Helvetica", "I", 7.5)
        self.cell(0, 5, f"Page {self.page_no()}", align="C")

    # -- cover --
    def cover(self):
        self.add_page()
        self._fill(TEAL)
        self.rect(0, 0, self.w, 80, "F")
        self.set_y(20)
        self.set_font("Helvetica", "B", 26)
        self.set_text_color(*WHITE)
        self.cell(0, 12, "MHM Race Management", align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", size=14)
        self.cell(0, 8, "Central Server Deployment Guide", align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", size=10)
        self.set_text_color(180, 230, 230)
        self.cell(0, 6, "Medicine Hat Massacre -- SEASAR", align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        self.set_y(92)
        self._rgb(DARK)
        self.set_font("Helvetica", "B", 10.5)
        self.cell(0, 7, "Three deployment options covered in this guide:",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

        options = [
            ("A", "Windows Host",    "LAN / same-network setup. Simplest, no extra hardware."),
            ("B", "Raspberry Pi",    "Always-on low-cost host. Good for club-owned hardware."),
            ("C", "Azure Web App",   "Public internet results page. Best for spectators anywhere."),
        ]
        for letter, title, desc in options:
            self._fill(TEAL)
            self.set_x(self.l_margin)
            self.set_font("Helvetica", "B", 11)
            self.set_text_color(*WHITE)
            self.cell(9, 8, letter, fill=True, align="C",
                      new_x=XPos.RIGHT, new_y=YPos.TOP)
            self._fill(LIGHT_BG)
            self.set_font("Helvetica", "B", 9)
            self._rgb(DARK)
            self.cell(50, 8, f"  {title}", fill=True,
                      new_x=XPos.RIGHT, new_y=YPos.TOP)
            self.set_font("Helvetica", size=9)
            self.cell(self.w - self.l_margin - self.r_margin - 59, 8,
                      f"  {desc}", fill=True,
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(1.5)

        self.ln(6)
        self.hline()
        self.note_text(
            "All options use the same server/app.py FastAPI application. "
            "The client laptop connects by entering the server URL and API key in Settings."
        )

    # -- architecture diagram (text-based) --
    def architecture_page(self):
        self.add_page()
        self._fill(TEAL)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*WHITE)
        self.set_x(self.l_margin)
        self.cell(self.w - self.l_margin - self.r_margin, 10,
                  "  System Architecture", fill=True,
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(4)

        self.body_text(OVERVIEW)
        self.ln(3)
        self.hline()
        self.ln(2)

        self.set_font("Helvetica", "B", 9.5)
        self._rgb(DARK)
        self.cell(0, 6, "Components:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

        for comp, desc in ARCHITECTURE:
            self._fill(TEAL)
            self.set_x(self.l_margin)
            self.set_font("Helvetica", "B", 9)
            self.set_text_color(*WHITE)
            self.cell(45, 6.5, f"  {comp}", fill=True,
                      new_x=XPos.RIGHT, new_y=YPos.TOP)
            self._fill(LIGHT_BG)
            self.set_font("Helvetica", size=8.5)
            self._rgb(DARK)
            self.multi_cell(
                w=self.w - self.l_margin - self.r_margin - 45,
                h=6.5, text=f"  {desc}", fill=True,
                new_x=XPos.LMARGIN, new_y=YPos.NEXT,
            )
            self.ln(1)

        self.ln(4)
        self.hline()
        self.ln(2)

        self.set_font("Helvetica", "B", 9.5)
        self._rgb(DARK)
        self.cell(0, 6, "Prerequisites (all options):", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)
        self.body_text(PREREQS)

    # -- deployment section --
    def section(self, title, body, steps, label=None):
        self.add_page()

        # Title bar
        self._fill(TEAL)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*WHITE)
        self.set_x(self.l_margin)
        self.cell(
            self.w - self.l_margin - self.r_margin,
            10, f"  {title}", fill=True,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
        self.ln(4)

        if body:
            self.body_text(body)
            self.ln(3)

        for idx, (step_title, step_body) in enumerate(steps, 1):
            avail = self.h - self.b_margin - self.get_y()
            if avail < 20:
                self.add_page()

            # Step badge
            self._fill(TEAL)
            self.set_font("Helvetica", "B", 9)
            self.set_text_color(*WHITE)
            self.set_x(self.l_margin)
            self.cell(7, 6, str(idx), fill=True, align="C",
                      new_x=XPos.RIGHT, new_y=YPos.TOP)

            # Step title
            self._rgb(DARK)
            self.set_font("Helvetica", "B", 9.5)
            self.cell(
                self.w - self.l_margin - self.r_margin - 7,
                6, f"  {step_title}",
                new_x=XPos.LMARGIN, new_y=YPos.NEXT,
            )

            # Step body -- detect code blocks (lines starting with spaces or #)
            self._rgb(DARK)
            lines = step_body.split("\n")
            i = 0
            while i < len(lines):
                line = lines[i]
                stripped = line.strip()

                # Collect consecutive code-ish lines into one block
                is_code = (
                    stripped.startswith("#") or
                    stripped.startswith("az ") or
                    stripped.startswith("pip ") or
                    stripped.startswith("python") or
                    stripped.startswith("git ") or
                    stripped.startswith("sudo ") or
                    stripped.startswith("uvicorn") or
                    stripped.startswith("set ") or
                    stripped.startswith("export ") or
                    stripped.startswith("nssm ") or
                    stripped.startswith("sqlite3") or
                    stripped.startswith("ssh ") or
                    stripped.startswith("scp ") or
                    stripped.startswith("curl ") or
                    stripped.startswith("[Unit]") or
                    stripped.startswith("[Service]") or
                    stripped.startswith("[Install]") or
                    stripped.startswith("WantedBy") or
                    stripped.startswith("ExecStart") or
                    stripped.startswith("Restart=") or
                    stripped.startswith("User=") or
                    stripped.startswith("WorkingDirectory") or
                    stripped.startswith("Environment=") or
                    stripped.startswith("Description=") or
                    stripped.startswith("After=") or
                    (len(stripped) > 0 and stripped[0] in ('-', '|') and
                     any(c in stripped for c in ['/', '\\']))
                )

                if is_code and stripped:
                    # Gather run of code lines
                    code_lines = []
                    while i < len(lines):
                        sl = lines[i].strip()
                        if sl == "" and i + 1 < len(lines) and lines[i+1].strip() == "":
                            break  # double blank = end of code block
                        code_lines.append(lines[i])
                        i += 1
                    avail = self.h - self.b_margin - self.get_y()
                    if avail < 12:
                        self.add_page()
                    self.set_x(self.l_margin + 9)
                    self.code_text("\n".join(code_lines))
                elif stripped.startswith(("-", "[/]", "[x]")):
                    self.set_font("Helvetica", size=9)
                    self.set_x(self.l_margin + 9)
                    self.multi_cell(
                        w=self.w - self.l_margin - self.r_margin - 9,
                        h=5, text=stripped,
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
                    )
                    i += 1
                elif stripped:
                    self.set_font("Helvetica", size=9)
                    self.set_x(self.l_margin + 9)
                    self.multi_cell(
                        w=self.w - self.l_margin - self.r_margin - 9,
                        h=5, text=stripped,
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
                    )
                    i += 1
                else:
                    self.ln(2)
                    i += 1

            self.ln(3)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
pdf = PDF()
pdf.cover()
pdf.architecture_page()
for sec in SECTIONS:
    pdf.section(sec["title"], sec["body"], sec["steps"], label=sec.get("label"))

pdf.output(OUTPUT)
print(f"Guide written to: {OUTPUT}")
