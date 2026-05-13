"""
Generate the MHM Race Management System user guide as a PDF.
Run:  python generate_guide.py
Output: MHM_Race_Guide.pdf (same directory as this script)
"""
from fpdf import FPDF
from fpdf.enums import XPos, YPos
import os

OUTPUT = os.path.join(os.path.dirname(__file__), "MHM_Race_Guide.pdf")

# ── Colour palette ──────────────────────────────────────────────────────────
BLUE      = (26,  86, 219)   # accent / headings
DARK      = (17,  24,  39)   # body text
MUTED     = (107, 114, 128)  # captions, notes
LIGHT_BG  = (243, 244, 246)  # step-number background
GREEN     = (22, 163, 74)
AMBER     = (180, 120,  0)
RED       = (185,  28,  28)
WHITE     = (255, 255, 255)
RULE      = (209, 213, 219)  # horizontal rule

# ── Content definition ──────────────────────────────────────────────────────
SECTIONS = [
    {
        "title": "Overview",
        "body": (
            "MHM (Medicine Hat Massacre) is a local web application that runs on a Windows "
            "laptop at the finish line. It manages team registration, course setup, SI chip "
            "reading, live scoring, and results export for rogaining and adventure-race events.\n\n"
            "The app opens automatically in your browser at http://localhost:8000 when launched. "
            "All data is stored locally in a SQLite database (mhm.db) — no internet connection "
            "is required on race day."
        ),
        "steps": []
    },
    {
        "title": "Step 1 — First Launch & Settings",
        "body": "Before anything else, configure the basics in Settings.",
        "steps": [
            ("Open the app", "Double-click MHM.exe. Your browser opens to http://localhost:8000 automatically. Log in with the admin username and password set during installation (default: admin / changeme)."),
            ("Settings page", "Click Settings in the top navigation. Set the Race Name (printed on receipts), the SI CN Offset (default 256 — only change if all your SI stations are in the 1–255 range), and the receipt Printer Name if you have a thermal printer connected."),
            ("Change your password", "In Settings, scroll to Change Password and update from the default before race day."),
        ]
    },
    {
        "title": "Step 2 — Create Classes",
        "body": "Classes group teams for separate leaderboard rankings (e.g. Open, Elite, Junior). Teams without a class are shown as Unclassified.",
        "steps": [
            ("Go to Classes", "Click Classes in the navigation menu."),
            ("Add a class", "Type the class name (e.g. 'Open') and click Add Class. Repeat for each category."),
            ("Set the Final Course", "Once courses exist (Step 3), return here and edit each class to set its Final Course. When a team's chip is read for that course, they are automatically marked FINISHED. If no final course is set, statuses must be set manually."),
        ]
    },
    {
        "title": "Step 3 — Create Courses",
        "body": "A course represents one leg or race category (e.g. '4-Hour Rogaine', '8-Hour Rogaine'). Each course has its own set of controls and time limit.",
        "steps": [
            ("Go to Courses", "Click Courses in the navigation menu."),
            ("Fill in course details",
             "• Course Name — e.g. '8-Hour'\n"
             "• Overtime Penalty — points deducted per minute over the time limit (0 = no penalty)\n"
             "• Start Time — mass-start time for all teams (or tick 'Use SI start beacon' for individual starts)\n"
             "• End Time — when the course closes\n"
             "• Cut-off Time — teams finishing after this are auto-DSQ'd"),
            ("Click Add Course", "The course appears below the form."),
        ]
    },
    {
        "title": "Step 4 — Set Up Controls",
        "body": "Controls are the checkpoints teams punch with their SI chip. Each course has its own independent control list.",
        "steps": [
            ("Open the course card", "Scroll down to your course on the Courses page."),
            ("Define Circuit Groups (optional)",
             "If your course has circuit controls (checkpoints that must be visited in order), define them first in the left panel:\n"
             "• Click + Add Group, enter a short name (e.g. 'P') and the penalty in points if the circuit is broken.\n"
             "• Repeat for each circuit group.\n"
             "Groups are shared across all controls — define once, assign to many."),
            ("Add controls",
             "Click + Add Control (collapsed by default). For each control enter:\n"
             "• SI Code — the number programmed into the physical SportIdent station\n"
             "• Name — a human-readable label (e.g. 'Hill Top')\n"
             "• Points — rogaining score for visiting this control\n"
             "Click Add Control. The control appears in the table above."),
            ("Assign mandatory / order / circuit",
             "In the controls table, for each row:\n"
             "• Mandatory — tick if the team must visit this control\n"
             "• Miss Pen. — points deducted if a mandatory control is missed\n"
             "• Order — visit sequence number for mandatory ordered controls\n"
             "• Circuit — select the circuit group this control belongs to (or leave blank)\n"
             "When finished, click Save Assignments. All rows are saved at once."),
            ("Bulk CSV import (optional)",
             "Expand 'Bulk Add / Update Controls (CSV)' to paste a CSV list:\n"
             "  si_code, points [, name [, is_mandatory [, order [, miss_penalty]]]]\n"
             "Useful for importing a large control list quickly. Circuit assignments are done via Save Assignments afterward."),
            ("Edit SI code / name / points",
             "Click the Edit button in any control row to update just its SI code, name, or point value without touching its mandatory or circuit settings."),
        ]
    },
    {
        "title": "Step 5 — Register Teams",
        "body": "Each entry in the race is a 'team' (even solo competitors). Team names, bib numbers, and class assignments are managed here.",
        "steps": [
            ("Go to Racers (Teams)", "Click Racers in the navigation menu."),
            ("Add a team",
             "Click + Add Team in the left panel:\n"
             "• Bib # — the number on the team's race bib\n"
             "• Team Name — required\n"
             "• Class — assign to a class for category rankings\n"
             "Click Add Team."),
            ("Bulk-add teams",
             "Select multiple teams in the left panel using the checkboxes, then use the bulk bar that appears at the bottom to set their class or enroll them in courses all at once."),
            ("Edit team details",
             "Click a team name in the left panel to open their detail panel on the right. From here you can edit bib number, name, class, merge duplicate records, or delete the team."),
        ]
    },
    {
        "title": "Step 6 — Enroll Teams in Courses",
        "body": "A team must be enrolled in a course before their chip can be scored. Enrollment creates a course entry that tracks their chip number, times, and punches.",
        "steps": [
            ("Open the team detail panel", "Click the team name in the Racers list."),
            ("Enroll in a course",
             "At the bottom of the Course Entries section, select the course from the dropdown, optionally enter their SI chip number now, and click Enroll."),
            ("Assign SI chip numbers",
             "In the Course Entries table, use the chip number field in each row to enter or update the chip number. Click Set to save. You can also assign chips later, or during bulk enrollment."),
            ("Bulk enrollment",
             "On the Racers page, tick multiple teams, then use the bulk bar to select one or more courses and click Apply. All selected teams are enrolled in all selected courses at once."),
        ]
    },
    {
        "title": "Step 7 — Assign SI Chips (Chip Library)",
        "body": "The Chips page lets you pre-load your chip inventory and auto-assign chips to teams.",
        "steps": [
            ("Go to Chips", "Click Chips in the navigation menu."),
            ("Import chip list",
             "Paste a CSV of your chip inventory (name, si_code) to pre-load all your chips. This lets the auto-assign function work."),
            ("Auto-assign",
             "Click Auto-Assign to automatically pair chips from your library to enrolled teams that don't yet have a chip number. Chips are assigned by bib number order."),
            ("Manual assignment",
             "Chips can also be assigned individually in the Racers → Course Entries table. Type the chip number and click Set."),
        ]
    },
    {
        "title": "Step 8 — Race Day: Reading Chips",
        "body": "Connect the SI reader to the laptop before teams start returning. Chips are read automatically as teams insert them into the station.",
        "steps": [
            ("Connect the SI reader",
             "Plug the SI station into a USB port. On the Dashboard, under SportIdent Reader, select the COM port from the dropdown and click Connect. The status dot turns green."),
            ("Select active course (optional)",
             "If you have multiple courses running simultaneously and want to restrict reads to one course, select it in the 'Active Course' dropdown on the Dashboard. Leave blank to score any matched course."),
            ("Team finishes — chip is read",
             "When a team inserts their chip into the SI station, the reader downloads it automatically. The Dashboard event log shows: 'Chip XXXX PENDING REVIEW'."),
            ("Review screen appears",
             "An amber banner appears at the top of the Dashboard with a Review button. Click it to open the chip review page for that chip."),
            ("Preview the results",
             "The review page shows:\n"
             "• Team name and course\n"
             "• All controls visited (green chips) with points\n"
             "• Unmatched punches (SI codes not in the course setup) in amber\n"
             "• Timing: start, finish, elapsed, overtime if any\n"
             "• Score breakdown: controls, overtime deduction, estimated total"),
            ("Make quick adjustments (if needed)",
             "Before saving, you can:\n"
             "• Add missing punches — tick any controls the team visited but the chip didn't record (e.g. chip malfunctioned at one station). They are saved as manual punches.\n"
             "• Waive overtime — tick 'Waive overtime penalty' to forgive the deduction (e.g. medical stop). A compensating adjustment is added automatically.\n"
             "• Custom adjustment — enter a signed point value (e.g. +50 or -30) for any other time-related correction."),
            ("Save & Print Receipt",
             "Click 'Save & Print Receipt'. The result is committed to the database, the team's status updates, and a receipt prints if a printer is configured. You are returned to the Dashboard."),
            ("Discard (wrong chip)",
             "If the chip belongs to a completely different event or was read by mistake, click Discard. Nothing is saved. The chip data is lost — re-read the chip if needed."),
        ]
    },
    {
        "title": "Step 9 — Wrong Chip / Course Fixes",
        "body": "Sometimes a team accidentally uses the wrong chip on a course. The review screen flags this as '0 controls matched' with all punches unmatched.",
        "steps": [
            ("Detect the problem",
             "On the review page, if the Controls Visited section shows 0 and there are unmatched punches, the chip was likely used on the wrong course. The row is highlighted in amber."),
            ("Fix during review",
             "Discard the read, have the team re-insert the correct chip (if available), and re-read. Or if the chip number is the problem, update the chip assignment in Racers first."),
            ("Fix after saving",
             "Go to Racers, select the team, and in their Course Entries table find the entry that has the wrong data (finish time set, 0 points). Click 'Move →' and select the correct course. All punch data, finish time, and start time are transferred. The source entry is reset to SIGNED_UP."),
        ]
    },
    {
        "title": "Step 10 — Manage Statuses",
        "body": "Team statuses flow through: SIGNED UP → REGISTERED → ON COURSE → FINISHED / DSQ. DNF and DNS can be set at any time.",
        "steps": [
            ("Automatic status updates",
             "• Reading a chip sets the entry to ON COURSE (or FINISHED if it's the class's final course).\n"
             "• A finish after the cutoff time auto-sets DSQ.\n"
             "• Manually setting any status on the Racers page applies to all of that team's course entries at once."),
            ("Individual team status",
             "On the Racers page, select a team. In the header, there is a colored status dropdown. Change it and it auto-saves, updating all their course entries."),
            ("Bulk status changes",
             "Go to Points → Manage Statuses. Each row is a team with their overall status. Use the inline dropdown to change one team, or tick multiple teams and use the bulk bar to set them all at once."),
            ("Common status meanings",
             "• SIGNED UP — registered in the system, not yet checked in\n"
             "• REGISTERED — checked in at registration desk\n"
             "• ON COURSE — left the start; chip read but not the final course\n"
             "• FINISHED — completed the final course (chip read or manual)\n"
             "• DNF — did not finish; withdrawn mid-race (shown at leaderboard bottom)\n"
             "• DSQ — disqualified; excluded from rankings but shown at bottom\n"
             "• DNS — did not start; excluded from leaderboard entirely"),
        ]
    },
    {
        "title": "Step 11 — Manual Adjustments",
        "body": "Use the Adjustments page to add bonuses, penalties, or free-form point corrections to any team's score.",
        "steps": [
            ("Go to Adjustments", "Click Points → Adjustments in the navigation."),
            ("Create adjustment types (optional)",
             "Pre-define reusable adjustment types with a name, category (bonus / penalty), and default point value. Examples: 'Gear Check Passed' (+50), 'Littering' (-100), 'Photo Challenge' (+30). These can then be applied to any team in one click."),
            ("Apply a predefined adjustment",
             "Find the team in the adjustments table, click the adjustment type from your predefined list, and click Apply. The points are added immediately."),
            ("Add a free-form manual adjustment",
             "In the team's adjustment row, enter a description and a signed point value (positive = bonus, negative = penalty) and click Add. These appear in the Manual column on the leaderboard."),
            ("Add a missing punch manually",
             "If you discover after the fact that a control should have been credited, find the team's entry in Adjustments, enter the SI code in the 'Add Punch' field, and click Add. This is recorded as a manual punch and re-scores immediately."),
            ("Delete a punch",
             "Individual punches can also be removed from the Adjustments page if a punch was recorded in error."),
        ]
    },
    {
        "title": "Step 12 — Live Leaderboard",
        "body": "The public leaderboard at /results shows live rankings as chips are read. It is accessible without login and can be displayed on a screen for spectators.",
        "steps": [
            ("Results page", "Click Results in the navigation (or go to http://localhost:8000/results in any browser on the same network). The page shows all teams sorted by total score within each course."),
            ("Class Results page",
             "Click Results → Class Results for rankings grouped by class (Open, Elite, etc.) with bonus, penalty, and manual adjustment columns broken out separately."),
            ("Score is live",
             "Every page load re-computes scores from raw punches. No caching — adjustments, new chip reads, and status changes are always reflected immediately on next load."),
        ]
    },
    {
        "title": "Step 13 — Finalize & Export",
        "body": "When all teams have been processed and results are final, export for records and awards.",
        "steps": [
            ("Quick export (CSV)",
             "Click the 'Quick Export' button on the Results page header to download a CSV of all results immediately."),
            ("Finalize & Export page",
             "Click 'Finalize & Export' on the Results page (or go to /finalize). This page lets you review the final standings and download the official results CSV."),
            ("Course control export",
             "On the Courses page, click 'Export CSV' on any course card to download a CSV of all its controls and point values. Useful for posting on the website or in the race booklet."),
            ("Export team card (Racers)",
             "On the Racers page, select a team and click 'Export Card'. This downloads a PDF card for that team showing their course entries, chip numbers, and current scores."),
        ]
    },
    {
        "title": "Step 14 — MeOS Import",
        "body": "If you use MeOS (orienteering event software) to set up courses and entries, you can import from a .meosxml file instead of entering everything manually.",
        "steps": [
            ("Go to Import → MeOS",
             "Click Import in the navigation, then select MeOS Import."),
            ("Upload the file",
             "Select your .meosxml export file. A preview shows which courses, controls, and teams will be imported."),
            ("Select and confirm",
             "Tick the courses and teams you want to import and click Confirm Import. Existing records with matching names are updated; new ones are created. The import is safe to re-run."),
        ]
    },
    {
        "title": "Quick-Reference: Race Day Checklist",
        "body": "",
        "steps": [
            ("Before race day", "✓ Create classes\n✓ Create courses with correct start/end/cutoff times\n✓ Add all controls with SI codes and points\n✓ Set circuit groups and mandatory controls, click Save Assignments\n✓ Set the Final Course for each class (Classes page)\n✓ Register all teams with bib numbers and class\n✓ Enroll all teams in their courses\n✓ Assign SI chip numbers (manually or via auto-assign)"),
            ("Race morning", "✓ Connect SI reader — green dot on Dashboard\n✓ Verify active course filter if running multiple courses simultaneously\n✓ Mark checked-in teams as REGISTERED via bulk status change\n✓ Mark started teams as ON COURSE once they leave"),
            ("As teams finish", "✓ Team inserts chip into SI station\n✓ Review page opens — check score, add any missing punches\n✓ Waive overtime if applicable\n✓ Click Save & Print Receipt\n✓ Receipt prints, team's status updates to FINISHED"),
            ("Post-race", "✓ Set DNS for any teams that never started (bulk status change)\n✓ Set DNF for teams that withdrew\n✓ Add any bonus/penalty adjustments\n✓ Open Class Results to verify final standings\n✓ Click Finalize & Export to download official CSV"),
        ]
    },
    {
        "title": "Troubleshooting",
        "body": "",
        "steps": [
            ("SI reader not detected",
             "Check that the USB cable is connected and the driver is installed. On the Dashboard, try each COM port in the dropdown until the status dot turns green. The app remembers the last working port on restart."),
            ("Chip read but 0 points",
             "The chip may be assigned to the wrong course, or the course controls don't match the chip's punches. On the review page, check the 'Unmatched punches' section. If all punches are unmatched, the chip is likely on the wrong course — discard and re-read with the correct chip, or use Racers → Move chip read to reassign saved data."),
            ("Wrong overtime penalty applied",
             "Use the review page (before saving) to waive or adjust. If already saved, go to Adjustments and add a manual bonus of +N pts to compensate."),
            ("Duplicate punch from re-read",
             "Re-reading the same chip a second time is safe — the app detects duplicate (entry_id, si_code, time) combinations and ignores them. Only the latest finish time is kept."),
            ("App won't start",
             "Check that no other app is using port 8000. Look in mhm.log (same folder as the .exe) for startup errors. If the database is corrupted, contact the developer — a backup copy of mhm.db from before the event is the best recovery path."),
            ("Score seems wrong",
             "Scores are recomputed live on every page load from raw punch data. If a score looks wrong, go to Adjustments for that team and check: punch list, adjustment list, and entry status. Mandatory miss penalties and circuit penalties only apply after the chip is read (not while the team is still on course)."),
        ]
    },
]


class PDF(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(20, 20, 20)
        self._page_num = 0

    def normalize_text(self, text):
        pairs = [
            (u'\u2014', ' - '),
            (u'\u2013', '-'),
            (u'\u2022', '-'),
            (u'\u2018', "'"),
            (u'\u2019', "'"),
            (u'\u201c', '"'),
            (u'\u201d', '"'),
            (u'\u2026', '...'),
            (u'\u2713', '[/]'),
            (u'\u2717', '[x]'),
            (u'\u2192', '->'),
            (u'\u2190', '<-'),
        ]
        for char, repl in pairs:
            text = text.replace(char, repl)
        text = text.encode('latin-1', errors='replace').decode('latin-1')
        return super().normalize_text(text)

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _rgb(self, colour):
        self.set_text_color(*colour)

    def _fill(self, colour):
        self.set_fill_color(*colour)

    def _draw(self, colour):
        self.set_draw_color(*colour)

    def hline(self, thickness=0.3):
        self._draw(RULE)
        self.set_line_width(thickness)
        x = self.get_x()
        y = self.get_y()
        self.line(self.l_margin, y, self.w - self.r_margin, y)
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

    def note_text(self, text):
        self._rgb(MUTED)
        self.set_font("Helvetica", "I", 8.5)
        self.set_x(self.l_margin)
        self.multi_cell(
            w=self.w - self.l_margin - self.r_margin,
            h=5, text=text,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )

    # ── Header / Footer ───────────────────────────────────────────────────────
    def header(self):
        if self.page_no() == 1:
            return
        self._rgb(MUTED)
        self.set_font("Helvetica", "I", 7.5)
        self.set_y(10)
        self.cell(0, 5, "MHM Race Management — User Guide", align="L",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_y(14)
        self.hline(0.2)
        self.set_y(18)

    def footer(self):
        self.set_y(-15)
        self._rgb(MUTED)
        self.set_font("Helvetica", "I", 7.5)
        self.cell(0, 5, f"Page {self.page_no()}", align="C")

    # ── Cover page ────────────────────────────────────────────────────────────
    def cover(self):
        self.add_page()
        # Blue header band
        self._fill(BLUE)
        self.rect(0, 0, self.w, 80, "F")
        self.set_y(22)
        self.set_font("Helvetica", "B", 28)
        self.set_text_color(*WHITE)
        self.cell(0, 12, "MHM Race Management", align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", size=15)
        self.cell(0, 8, "User Guide & Race Setup Walkthrough", align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", size=10)
        self.set_text_color(200, 215, 255)
        self.cell(0, 6, "Medicine Hat Massacre — SEASAR", align="C",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        # Body
        self.set_y(95)
        self._rgb(DARK)
        self.set_font("Helvetica", "B", 11)
        self.cell(0, 7, "What's in this guide:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

        items = [
            ("Settings & Initial Setup", "Configure race name, SI offset, and printer"),
            ("Classes & Courses",        "Create competition categories and course legs"),
            ("Controls",                 "Enter SI codes, points, mandatory controls, circuit groups"),
            ("Team Registration",        "Add teams, assign bib numbers, enroll in courses"),
            ("SI Chip Assignment",       "Assign physical chips to team entries"),
            ("Race Day Operations",      "Connect SI reader, review chip reads, save results"),
            ("Manual Adjustments",       "Add bonuses, penalties, missing punches"),
            ("Results & Export",         "Live leaderboard, finalize, CSV export"),
            ("Troubleshooting",          "Common issues and fixes"),
        ]
        for title, desc in items:
            self._fill(LIGHT_BG)
            self.set_x(self.l_margin)
            self.set_font("Helvetica", "B", 9)
            self._rgb(BLUE)
            self.cell(65, 6.5, f"  {title}", fill=True,
                      new_x=XPos.RIGHT, new_y=YPos.TOP)
            self.set_font("Helvetica", size=9)
            self._rgb(DARK)
            self.cell(self.w - self.l_margin - self.r_margin - 65, 6.5,
                      f"  {desc}", fill=True,
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(1)

        self.set_y(-40)
        self.hline()
        self.note_text("http://localhost:8000  ·  All data stored locally in mhm.db  ·  No internet required on race day")

    # ── Section ───────────────────────────────────────────────────────────────
    def section(self, title, body, steps):
        self.add_page()

        # Section title bar
        self._fill(BLUE)
        self.set_font("Helvetica", "B", 14)
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
            # Step number badge
            avail = self.h - self.b_margin - self.get_y()
            if avail < 18:
                self.add_page()

            self._fill(BLUE)
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

            # Step body — handle bullet lines
            self._rgb(DARK)
            self.set_font("Helvetica", size=9)
            lines = step_body.split("\n")
            for line in lines:
                stripped = line.strip()
                if stripped.startswith(("•", "✓", "-", "[/]")):
                    # Bullet line — indent slightly
                    self.set_x(self.l_margin + 9)
                    self.multi_cell(
                        w=self.w - self.l_margin - self.r_margin - 9,
                        h=5, text=stripped,
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
                    )
                else:
                    self.set_x(self.l_margin + 9)
                    self.multi_cell(
                        w=self.w - self.l_margin - self.r_margin - 9,
                        h=5, text=stripped,
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
                    )
            self.ln(3)


# ── Build ─────────────────────────────────────────────────────────────────────
pdf = PDF()
pdf.cover()
for sec in SECTIONS:
    pdf.section(sec["title"], sec["body"], sec["steps"])

pdf.output(OUTPUT)
print(f"Guide written to: {OUTPUT}")
