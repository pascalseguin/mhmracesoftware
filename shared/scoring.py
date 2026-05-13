"""
Rogaining scoring engine.

Scoring rules:
  1. A punch matches a control if its SI code equals the control's si_code.
     Only the FIRST punch per control counts (duplicates ignored).
  2. If any MANDATORY control is missed, a per-control miss penalty is deducted.
  3. If mandatory-ORDER controls exist, visiting them out of sequence sets
     order_violation = True (flagged in results but does not zero points by itself).
  4. CIRCUIT controls: controls sharing a circuit_group must be visited in cyclic
     sequential order (alphabetical by name within the group). Teams may start at
     any point in the circuit but must continue sequentially, wrapping P10→P01.
     Missing any control or visiting out of order deducts circuit_miss_penalty.
  5. Overtime: every minute past the time limit deducts overtime_penalty_per_minute.
     Overtime is rounded UP to the nearest whole minute.
  6. Named adjustments (bonuses/penalties entered by officials) are summed and applied last.
  7. Final score is never negative — clamped to 0.

This module has no database or I/O dependencies and can be used independently.
"""
from __future__ import annotations
import math
from datetime import datetime
from .models import Course, CourseEntry, Racer, ScoredResult, EntryStatus


def score_entry(entry: CourseEntry, course: Course, racer: Racer) -> ScoredResult:
    """
    Compute a full ScoredResult for one team on one course.

    Called every time results are loaded — never cached — so any change to
    punches or adjustments is always reflected immediately.
    """
    result = ScoredResult(entry=entry, course=course, racer=racer)

    # DNS teams don't race at all — return a zeroed result immediately.
    if entry.status == EntryStatus.DNS:
        return result

    # Build a lookup from SI code → ControlPoint for fast punch matching.
    # If two controls had the same SI code (misconfiguration), the last one wins —
    # but the UI prevents that during course setup.
    control_map = {c.si_code: c for c in course.controls}

    # ── Step 1: Match punches to controls ─────────────────────────────────────
    # Sort punches chronologically so that when a control is punched twice,
    # we always use the EARLIEST punch time (rules: first visit counts).
    seen: set[int] = set()          # SI codes already matched
    visited = []                    # ControlPoint objects in visit order
    punch_times: dict[int, datetime] = {}   # si_code → time of first punch

    for punch in sorted(entry.punches, key=lambda p: p.punch_time):
        if punch.si_code in control_map and punch.si_code not in seen:
            seen.add(punch.si_code)
            visited.append(control_map[punch.si_code])
            punch_times[punch.si_code] = punch.punch_time

    result.controls_visited = visited

    # Track punch codes that didn't match any configured control.
    # Shown as a warning in the Adjustments page so officials know
    # which SI codes need to be added to the course setup.
    result.unmatched_punch_codes = sorted(
        {p.si_code for p in entry.punches if p.si_code not in control_map}
    )

    # ── Step 2: Check mandatory controls ──────────────────────────────────────
    mandatory = [c for c in course.controls if c.is_mandatory]
    missed = [c for c in mandatory if c.si_code not in seen]
    result.controls_missed_mandatory = missed

    # ── Step 3: Check mandatory order ─────────────────────────────────────────
    # Controls with a mandatory_order value must be visited in ascending order.
    # We only check the subset that was actually visited (can't penalise for
    # controls the team never reached).
    ordered = sorted(
        [c for c in course.controls if c.mandatory_order is not None],
        key=lambda c: c.mandatory_order,  # type: ignore[arg-type]
    )
    if ordered:
        visited_ordered = [c for c in ordered if c.si_code in seen]
        if visited_ordered:
            # If the punch times for these controls aren't in ascending order,
            # the team visited them out of sequence.
            times = [punch_times[c.si_code] for c in visited_ordered]
            result.order_violation = times != sorted(times)

    # ── Step 4: Raw points ────────────────────────────────────────────────────
    # Mandatory miss penalties only apply once results have been read (finish_time
    # is set). Until then a team has no punches to evaluate, so deducting for
    # "missing" mandatory controls would produce meaningless negative scores.
    result.raw_points = sum(c.points for c in visited)
    if entry.finish_time is not None:
        result.raw_points -= sum(c.mandatory_miss_penalty for c in missed)

    # ── Step 4b: Circuit checks ───────────────────────────────────────────────
    # Same rationale: circuit penalties only apply after the chip is read.
    # Group controls by circuit_group (blank = not in any circuit).
    # Controls in the same group must be visited in cyclic alphabetical-name order:
    # the team may start at any position but must continue sequentially, with
    # the last control wrapping back to the first (e.g. P10 → P01 is valid).
    if entry.finish_time is not None:
        circuit_groups: dict[str, list] = {}
        for ctrl in course.controls:
            grp = getattr(ctrl, "circuit_group", "")
            if grp:
                circuit_groups.setdefault(grp, []).append(ctrl)

        for grp_name, grp_controls in circuit_groups.items():
            # Alphabetical name sort defines the canonical circuit order.
            ordered = sorted(grp_controls, key=lambda c: c.name)
            n = len(ordered)
            pos_map = {c.si_code: i for i, c in enumerate(ordered)}
            penalty = max(
                (c.circuit_miss_penalty for c in grp_controls if c.circuit_miss_penalty),
                default=0,
            )

            # Get visit times for controls in this circuit (only those punched).
            timed = [
                (pos_map[c.si_code], punch_times[c.si_code])
                for c in ordered
                if c.si_code in seen
            ]

            circuit_ok = False
            if len(timed) == n:
                # All controls were visited — check cyclic order.
                timed.sort(key=lambda x: x[1])   # sort by punch time
                positions = [pos for pos, _ in timed]
                circuit_ok = all(
                    (positions[i + 1] - positions[i]) % n == 1
                    for i in range(n - 1)
                )

            if not circuit_ok:
                result.circuit_violations.append(grp_name)
                result.raw_points -= penalty

    # ── Step 5: Elapsed time and overtime ────────────────────────────────────
    # We need both start and finish times to calculate elapsed time.
    # If either is missing (team hasn't finished, or times weren't recorded),
    # overtime is assumed to be 0.
    if entry.start_time and entry.finish_time:
        elapsed = entry.finish_time - entry.start_time
        result.elapsed_seconds = int(elapsed.total_seconds())
        limit_seconds = course.time_limit_minutes * 60

        if result.elapsed_seconds > limit_seconds:
            over = result.elapsed_seconds - limit_seconds
            # Overtime rounds UP — 1 second late = 1 full minute penalty.
            result.overtime_minutes = math.ceil(over / 60)
            result.overtime_penalty = (
                result.overtime_minutes * course.overtime_penalty_per_minute
            )

    # ── Step 6: Named adjustments ─────────────────────────────────────────────
    # Bonuses (positive) and penalties (negative) entered manually by officials.
    # Examples: gear check bonus, littering penalty, timing correction.
    result.total_adjustments = sum(a.points for a in entry.adjustments)

    # ── Step 7: Final total ───────────────────────────────────────────────────
    # Score can never go below 0, regardless of how many penalties are applied.
    result.total_points = max(
        0,
        result.raw_points - result.overtime_penalty + result.total_adjustments,
    )

    return result


def rank_results(results: list[ScoredResult]) -> list[tuple[int | None, ScoredResult]]:
    """
    Sort a list of ScoredResults into ranked order and return (rank, result) pairs.

    Ranking rules:
      1. DNS entries are excluded entirely (not returned).
      2. DNF and DSQ entries are returned with rank=None at the bottom of the list,
         sorted among themselves by descending points then ascending time.
      3. All other statuses are ranked normally:
         higher total_points = better rank; tie-break = shorter elapsed time.
    """
    _EXCLUDED    = {EntryStatus.DNS}
    _UNRANKED    = {EntryStatus.DNF, EntryStatus.DSQ}

    competitive = [r for r in results if r.entry.status not in _EXCLUDED | _UNRANKED]
    unranked    = [r for r in results if r.entry.status in _UNRANKED]

    _sort_key = lambda r: (-r.total_points, r.elapsed_seconds or 99999)
    competitive.sort(key=_sort_key)
    unranked.sort(key=_sort_key)

    return (
        [(i, r) for i, r in enumerate(competitive, start=1)]
        + [(None, r) for r in unranked]
    )
