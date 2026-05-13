"""
Parse MeOS XML backup files (.meosxml) into importable Python objects.

MeOS is the orienteering event software used to pre-register teams and configure
courses before the race. Exporting a backup from MeOS gives us a .meosxml file
with all the course, control, and team data that would otherwise need to be
entered manually into MHM.

MeOS XML structure (relevant sections):
  <meosdata>
    <Name>Event Name</Name>

    <ControlList>
      <Control>
        <Id>31</Id>            ← MeOS internal control ID (not the SI station code)
        <Numbers>359</Numbers> ← Actual SI station Code Number (what the chip records)
        <Name>CP A</Name>
        <oData>
          <Rogaining>10</Rogaining>  ← point value for rogaining scoring
        </oData>
      </Control>
      ...
    </ControlList>

    <CourseList>
      <Course>
        <Id>1</Id>
        <Name>8-Hour</Name>
        <Controls>31;45;67;</Controls>  ← semicolon-separated MeOS control IDs (not SI codes!)
        <oData>
          <RTimeLimit>28800</RTimeLimit>   ← time limit in SECONDS (÷60 for minutes)
          <RReduction>10</RReduction>      ← overtime penalty in pts/min
        </oData>
      </Course>
    </CourseList>

    <ClassList>
      <Class>
        <Id>1</Id>
        <Name>Open</Name>
        <Course>1</Course>   ← course ID this class is assigned to
      </Class>
    </ClassList>

    <ClubList>
      <Club>                 ← Each Club = one team
        <Id>101</Id>
        <Name>Mountain Goats</Name>
      </Club>
    </ClubList>

    <RunnerList>
      <Runner>               ← One Runner per team per course entry
        <Id>1</Id>
        <Club>101</Club>     ← links to ClubList
        <Class>1</Class>     ← links to ClassList (which links to CourseList)
        <CardNo>2031141</CardNo>  ← SI chip number
        <StartNo>42</StartNo>    ← bib number
      </Runner>
    </RunnerList>
  </meosdata>

Important: the Controls field in CourseList uses MeOS internal control IDs,
NOT SI station codes. We resolve them through ControlList during import.
"""
from __future__ import annotations
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Result dataclasses ────────────────────────────────────────────────────────
# These are intermediate objects used only during the import preview/confirm flow.
# They are NOT the same as the shared/models.py classes — they carry MeOS-specific
# IDs and raw parsed data before it's committed to the local database.

@dataclass
class ParsedControl:
    """A single control point as read from MeOS ControlList."""
    ctrl_id: str    # MeOS internal ID (e.g. "31") — used to resolve course references
    si_code: int    # SI station Code Number (from <Numbers>) — used for punch matching
    name: str       # Display name (e.g. "Checkpoint A")
    points: int     # Rogaining point value (0 = non-scoring, e.g. start/finish gates)


@dataclass
class ParsedCourse:
    """A course as read from MeOS CourseList, with controls already resolved."""
    course_id: str          # MeOS internal course ID
    name: str               # Display name (e.g. "8-Hour")
    time_limit_minutes: int # Converted from seconds stored in MeOS
    overtime_penalty: int   # Points deducted per minute over the limit
    controls: list[ParsedControl] = field(default_factory=list)

    @property
    def scoring_controls(self) -> list[ParsedControl]:
        """Controls with points > 0 (excludes start/finish gates, route checkmarks, etc.)."""
        return [c for c in self.controls if c.points > 0]


@dataclass
class ParsedEntry:
    """
    One team's enrolment in one course.

    In MeOS, each Runner record represents one team-course assignment.
    A team can have multiple Runner records if they're entered in multiple courses.
    """
    runner_id: str      # MeOS Runner ID
    course_id: str      # MeOS Course ID this entry is for
    course_name: str    # Denormalised for display in preview
    class_name: str     # Denormalised class/category name
    si_chip: Optional[int]  # SI chip number (None if not assigned in MeOS)
    bib: str            # Bib number from MeOS StartNo


@dataclass
class ParsedTeam:
    """
    A team (Club in MeOS terminology) with all their course entries.

    MeOS calls teams "clubs" because it was designed for orienteering clubs.
    In rogaining we use it to represent a single competing team.
    """
    club_id: str    # MeOS Club ID — used as the checkbox value in the preview form
    name: str       # Team name
    bib: str = ""   # Assigned as the minimum StartNo across all entries
    entries: list[ParsedEntry] = field(default_factory=list)


@dataclass
class MeosParseResult:
    """
    Complete parsed result from one .meosxml file.

    Passed to the preview template directly (as dataclass objects with properties).
    Serialised to plain dicts via to_session() for storage between the preview
    (GET) and confirm (POST) HTTP requests.
    """
    event_name: str
    courses: list[ParsedCourse]
    teams: list[ParsedTeam]
    warnings: list[str] = field(default_factory=list)

    def to_session(self) -> dict:
        """
        Convert to plain nested dicts for storage in _meos_pending between requests.

        asdict() recursively converts all nested dataclasses to dicts,
        which is safe to store in memory and easy to iterate in the confirm route.
        """
        return {
            "event_name": self.event_name,
            "courses":    [asdict(c) for c in self.courses],
            "teams":      [asdict(t) for t in self.teams],
            "warnings":   self.warnings,
        }


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_meos_xml(content: str) -> MeosParseResult:
    """
    Parse the full text of a .meosxml backup file into a MeosParseResult.

    The parse happens in five passes over the XML tree:
      1. ControlList  → build a map of MeOS ctrl_id → ParsedControl
      2. CourseList   → build courses, resolving control references via the map
      3. ClassList    → build class_id → course_id and class_id → name maps
      4. ClubList     → build team_id → ParsedTeam (skip "Vacant" placeholder teams)
      5. RunnerList   → attach ParsedEntry to each team, resolve course via class

    Raises ValueError if the file is not valid XML or not a MeOS backup.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"Not valid XML: {exc}")

    if root.tag != "meosdata":
        raise ValueError(
            f"Expected <meosdata> root, got <{root.tag}>. Is this a MeOS backup?"
        )

    event_name = _txt(root, "Name") or "Unknown Event"
    warnings: list[str] = []

    # ── Pass 1: Controls ──────────────────────────────────────────────────────
    # Build a lookup from MeOS internal ctrl_id to ParsedControl.
    # The ctrl_id is used in CourseList to reference controls; the si_code
    # (from <Numbers>) is what actually gets stored in punch records.
    control_map: dict[str, ParsedControl] = {}
    for ctrl in root.findall("ControlList/Control"):
        cid     = _txt(ctrl, "Id")
        si_str  = _txt(ctrl, "Numbers")   # SI station Code Number — NOT the MeOS ID
        name    = _txt(ctrl, "Name")
        pts_str = ctrl.findtext("oData/Rogaining") or "0"

        if not cid or not si_str:
            continue    # skip controls without an ID or SI code

        try:
            si_code = int(si_str)
            points  = int(pts_str)
        except ValueError:
            continue    # skip controls with non-numeric codes

        control_map[cid] = ParsedControl(
            ctrl_id=cid, si_code=si_code, name=name, points=points
        )

    # ── Pass 2: Courses ───────────────────────────────────────────────────────
    # The Controls field is a semicolon-separated list of MeOS ctrl_ids:
    # e.g. "31;45;67;" — we look each up in control_map to get the full control.
    courses: list[ParsedCourse] = []
    course_map: dict[str, ParsedCourse] = {}

    for course_el in root.findall("CourseList/Course"):
        cid      = _txt(course_el, "Id")
        name     = _txt(course_el, "Name") or f"Course {cid}"
        ctrl_str = _txt(course_el, "Controls")      # "31;45;67;"

        # Time limit is stored in SECONDS in MeOS; we convert to minutes.
        # Default is 28800 seconds (8 hours) if the field is missing.
        tl_sec   = int(course_el.findtext("oData/RTimeLimit")  or "28800")
        overtime = int(course_el.findtext("oData/RReduction")  or "0")

        controls: list[ParsedControl] = []
        for token in ctrl_str.rstrip(";").split(";"):
            token = token.strip()
            if token in control_map:
                controls.append(control_map[token])
            elif token:
                # Control referenced in the course but missing from ControlList —
                # warn but continue so the rest of the import isn't blocked.
                warnings.append(
                    f"Control {token} referenced in '{name}' not found in ControlList"
                )

        c = ParsedCourse(
            course_id=cid,
            name=name,
            time_limit_minutes=tl_sec // 60,
            overtime_penalty=overtime,
            controls=controls,
        )
        courses.append(c)
        if cid:
            course_map[cid] = c

    # ── Pass 3: Classes ───────────────────────────────────────────────────────
    # A MeOS "class" maps a category (e.g. "Open 8-Hour") to a specific course.
    # Runners reference a class, not a course directly — we follow class → course
    # when building ParsedEntry objects in Pass 5.
    class_course_map: dict[str, str] = {}   # class_id → course_id
    class_name_map:   dict[str, str] = {}   # class_id → class display name

    for cls in root.findall("ClassList/Class"):
        cid  = _txt(cls, "Id")
        name = _txt(cls, "Name")
        crid = _txt(cls, "Course")
        if cid:
            class_course_map[cid] = crid
            class_name_map[cid]   = name

    # ── Pass 4: Clubs (Teams) ─────────────────────────────────────────────────
    # MeOS uses "Club" to represent a competing team in rogaining events.
    # "Vacant" is a MeOS placeholder for unregistered start slots — skip it.
    team_map: dict[str, ParsedTeam] = {}

    for club in root.findall("ClubList/Club"):
        cid  = _txt(club, "Id")
        name = _txt(club, "Name").strip()
        if not name or name.lower() == "vacant" or not cid:
            continue
        team_map[cid] = ParsedTeam(club_id=cid, name=name)

    # ── Pass 5: Runners → entries ─────────────────────────────────────────────
    # Each Runner record is one team-course enrolment. We attach it to the
    # correct ParsedTeam and resolve the course via the class mapping.
    for runner in root.findall("RunnerList/Runner"):
        club_id  = _txt(runner, "Club")
        class_id = _txt(runner, "Class")
        rid      = _txt(runner, "Id")
        card_str = _txt(runner, "CardNo")    # SI chip number
        bib      = _txt(runner, "StartNo")   # bib number

        # Skip runners not attached to a known team (e.g. orphaned records).
        if club_id not in team_map:
            continue

        # Resolve class → course.
        course_id   = class_course_map.get(class_id, "")
        course_name = course_map[course_id].name if course_id in course_map else ""
        class_name  = class_name_map.get(class_id, "")

        si_chip: Optional[int] = None
        if card_str:
            try:
                si_chip = int(card_str)
            except ValueError:
                pass    # non-numeric chip number — leave as None

        team_map[club_id].entries.append(ParsedEntry(
            runner_id=rid,
            course_id=course_id,
            course_name=course_name,
            class_name=class_name,
            si_chip=si_chip,
            bib=bib,
        ))

    # Assign each team's bib number as the minimum StartNo across all their entries.
    # This handles multi-person teams where each member has their own Runner record.
    for team in team_map.values():
        numeric_bibs = [int(e.bib) for e in team.entries if e.bib.isdigit()]
        if numeric_bibs:
            team.bib = str(min(numeric_bibs))

    if not courses and not team_map:
        warnings.append(
            "No courses or teams found. Make sure this is a MeOS XML backup file."
        )

    return MeosParseResult(
        event_name=event_name,
        courses=courses,
        teams=sorted(team_map.values(), key=lambda t: t.name.lower()),
        warnings=warnings,
    )


def _txt(el: ET.Element, tag: str) -> str:
    """
    Safely get the text content of a child element.

    Returns an empty string if the element or its text is missing,
    so callers don't need to handle None everywhere.
    """
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else ""
