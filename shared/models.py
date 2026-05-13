"""
Data models shared between the local client and the remote server.

All business objects are plain Python dataclasses — no ORM, no DB logic here.
The database layer (client/database.py) is responsible for converting between
sqlite3.Row results and these dataclasses.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class EntryStatus(str, Enum):
    """
    Lifecycle status of a team's course entry.

    Pre-race workflow (set manually):
      SIGNED_UP  = Entered / paid — default for new enrollments.
      REGISTERED = Checked in at registration desk on race day.
      ON_COURSE  = Verified at start line; physically on course.

    Post-race (automatic or manual):
      FINISHED   = Completed the race (set automatically for the class's final course).
      OK         = Legacy alias for FINISHED.
      ACTIVE     = Legacy alias for ON_COURSE.

    Non-competitive (shown at bottom of leaderboard, not ranked):
      DNF = Did Not Finish — withdrew during the race.
      DSQ = Disqualified   — e.g. returned after the cutoff time.

    Excluded from leaderboard entirely:
      DNS = Did Not Start — registered but never punched in.
    """
    # Pre-race
    SIGNED_UP  = "SIGNED_UP"
    REGISTERED = "REGISTERED"
    ON_COURSE  = "ON_COURSE"
    # Finished
    FINISHED   = "FINISHED"
    OK         = "OK"       # legacy
    ACTIVE     = "ACTIVE"   # legacy
    # Non-competitive
    DNF = "DNF"
    DSQ = "DSQ"
    # Excluded
    DNS = "DNS"


@dataclass
class ControlPoint:
    """
    A single SI timing control (checkpoint) within a course.

    si_code        — The code number programmed into the physical SportIdent station.
                     This is what the SI chip records when punched. Must match exactly
                     what appears in the punch records (after the CN offset is applied).
    points         — Rogaining score awarded for visiting this control. 0 = non-scoring
                     (e.g. mandatory route gates that don't add points).
    is_mandatory   — If True, missing this control zeros the team's raw score entirely.
    mandatory_order — If set, the control must be visited in this sequence relative to
                     other ordered controls (e.g. mandatory river crossing must come before
                     the summit). Violation is flagged but doesn't automatically zero points.
    """
    id: int
    course_id: int
    si_code: int
    name: str
    points: int
    is_mandatory: bool = False
    mandatory_order: Optional[int] = None
    mandatory_miss_penalty: int = 0
    circuit_group: str = ""
    circuit_miss_penalty: int = 0


@dataclass
class Course:
    """
    A rogaining course (e.g. "4-Hour", "8-Hour", "24-Hour").

    time_limit_minutes        — Teams must finish within this window. Overtime is penalised.
    overtime_penalty_per_minute — Points deducted per minute over the time limit.
    controls                  — All control points on this course (scoring + mandatory).
                                 A team is scored only against the controls in their course.
    """
    id: int
    name: str
    time_limit_minutes: int
    overtime_penalty_per_minute: int = 0
    controls: list[ControlPoint] = field(default_factory=list)


@dataclass
class RaceClass:
    """
    A competition category used to group teams for a separate ranking.

    Examples: "Open", "Competitive", "Junior", "Elite".
    Classes are optional — teams without a class are shown as "Unclassified"
    on the Class Results page but still appear in the per-course Results page.
    """
    id: int
    name: str


@dataclass
class Racer:
    """
    A team entry. Called 'Racer' internally but represents a whole team.

    bib_number — printed on race bibs; used for quick look-up at finish line.
    class_id   — optional link to a RaceClass for category rankings.
    class_name — denormalised display name (populated by JOIN in database queries).
    """
    id: int
    name: str       # team name
    bib_number: str = ""
    class_id: Optional[int] = None
    class_name: str = ""


@dataclass
class Adjustment:
    """
    A bonus, penalty, or free-form adjustment applied to a course entry.

    points > 0 = bonus  (e.g. photo challenge, gear check passed)
    points < 0 = penalty (e.g. littering, equipment violation)

    category — controls which leaderboard column this rolls into:
      'bonus'   — applied from a predefined bonus type; shown in the Bonuses column.
      'penalty' — applied from a predefined penalty type; shown in the Penalties column.
      'manual'  — free-form entry by an official; shown in the Manual Adj column.
    type_id  — FK to adjustment_types if a predefined template was used; None for manual.

    All three categories are summed into total_adjustments for scoring purposes.
    The split is purely for display on the leaderboard.
    """
    id: int
    entry_id: int
    description: str
    points: int
    category: str = "manual"       # 'bonus', 'penalty', or 'manual'
    type_id: Optional[int] = None  # set when applied from a predefined adjustment_type


@dataclass
class Punch:
    """
    A single SI chip punch record downloaded from a competitor's chip.

    si_code    — The control station code recorded by the chip.
    punch_time — Wall-clock time of the punch as stored on the chip.
    is_manual  — True if this punch was entered by a race official (not from a chip read).
                 Manual punches are flagged in the UI so officials can tell them apart.
    """
    si_code: int
    punch_time: datetime
    is_manual: bool = False


@dataclass
class CourseEntry:
    """
    One team's participation in one specific course.

    A team can be enrolled in multiple courses (e.g. a mixed-format event).
    Each enrolment gets its own CourseEntry, its own chip assignment, its own
    punches, and is scored independently.

    si_chip    — The SI chip number assigned to this team for this course.
                 One chip per team per course — teams with multiple members
                 share a single chip on each course.
    start_time — Set automatically from the chip's START record on first read,
                 or manually by an official. Not overwritten once set.
    finish_time — Set from the chip's FINISH record; updated if a later time arrives
                 (handles multiple reads of the same chip).
    """
    id: int
    racer_id: int
    course_id: int
    si_chip: Optional[int] = None
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    status: EntryStatus = EntryStatus.ACTIVE
    punches: list[Punch] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ScoredResult:
    """
    A fully-computed result for one team on one course.

    Produced by scoring.score_entry() — never stored directly in the DB.
    Recomputed fresh from raw punches/adjustments every time results are loaded,
    so editing punches or adjustments is always immediately reflected.

    controls_visited        — Controls matched between chip punches and course setup.
    controls_missed_mandatory — Mandatory controls the team did NOT visit (causes score=0).
    unmatched_punch_codes   — SI codes on the chip that don't match any configured control.
                              Shown as a warning in Adjustments so officials can fix setup.
    raw_points              — Sum of points for visited controls (0 if mandatory missed).
    overtime_penalty        — Points deducted for finishing late.
    total_adjustments       — Net bonus/penalty from manual adjustments.
    total_points            — Final score: raw - overtime + adjustments, minimum 0.
    elapsed_seconds         — Total race time from start to finish (None if times missing).
    order_violation         — True if mandatory-order controls were visited out of sequence.
    """
    entry: CourseEntry
    racer: Racer
    course: Course
    controls_visited: list[ControlPoint] = field(default_factory=list)
    controls_missed_mandatory: list[ControlPoint] = field(default_factory=list)
    unmatched_punch_codes: list[int] = field(default_factory=list)
    raw_points: int = 0
    overtime_minutes: int = 0
    overtime_penalty: int = 0
    total_adjustments: int = 0
    total_points: int = 0
    elapsed_seconds: Optional[int] = None
    order_violation: bool = False
    circuit_violations: list[str] = field(default_factory=list)
