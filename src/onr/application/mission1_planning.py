"""Code-owned Mission 1 surveillance candidates, DAG, oracle, and gate."""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cache
from itertools import pairwise, repeat
from typing import Any, cast

import numpy as np

from onr.contracts.fsm import FSMStatus, Statechart
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot

TIME_SCALE = 2
SCORE_SCALE = 1_000_000
OBSERVATION_DWELL_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class _PublicReport:
    report_id: str
    entity_id: int
    time_s: float
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class ObservationOpportunity:
    report_id: str
    entity_id: int
    time_s: float
    x: float
    y: float
    recall: float
    estimation: float
    utility: float
    variance: float = 0.0
    omission_rate: float = 0.0
    omission_lookback_s: float = 0.0
    omission_intervals: tuple[tuple[float, float], ...] = ()
    information_schedule_count: int = 1


@dataclass(frozen=True, slots=True)
class CandidateUtility:
    recall: float
    estimation: float
    omission_yield: float

    @property
    def combined(self) -> float:
        return self.recall + self.estimation + self.omission_yield


@dataclass(frozen=True, slots=True)
class SurveillanceCandidate:
    candidate_id: str
    mode: str
    entity_id: int | None
    start_s: float
    end_s: float
    x: float
    y: float
    end_x: float
    end_y: float
    report_ids: tuple[str, ...]
    target_posterior_risk: float
    expected_omission_probability: float
    public_report_rate: float
    report_span_s: float
    recall_utility: float
    estimation_utility: float
    omission_yield: float
    combined_score: float
    arrival_direction: int | None = None
    observation_delay_s: float = 0.0
    scored_observation_windows: tuple[tuple[float, float], ...] = ()

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def last_report_time_s(self) -> float:
        """Original report epoch; GPS acquisition can precede the first report."""
        if self.mode == "pursue_ship":
            return self.end_s - OBSERVATION_DWELL_SECONDS
        return self.start_s - self.observation_delay_s + self.report_span_s


@dataclass(frozen=True, slots=True)
class CandidateDAG:
    candidates: tuple[SurveillanceCandidate, ...]
    arcs: tuple[tuple[int, int], ...]
    source: int
    sink: int


@dataclass(frozen=True, slots=True)
class AdvisoryRoute:
    candidates: tuple[SurveillanceCandidate, ...]
    score: float
    duration_s: float
    covered_report_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplanGateDecision:
    trigger: bool
    current_score: float
    advisory_score: float
    relative_improvement: float
    reason: str


def _candidate_id(
    mode: str, report_ids: Sequence[str], *, viewpoint: tuple[int, int] | None = None,
    arrival_direction: int | None = None,
    observation_delay_s: float = 0.0,
    observation_dwell_s: float = OBSERVATION_DWELL_SECONDS,
    observation_start_s: float | None = None,
) -> str:
    identity: dict[str, object] = {"mode": mode, "report_ids": list(report_ids)}
    if viewpoint is not None:
        identity["viewpoint"] = viewpoint
    if arrival_direction is not None:
        identity["arrival_direction"] = arrival_direction
    if observation_delay_s:
        identity["observation_delay_s"] = observation_delay_s
    if observation_dwell_s != OBSERVATION_DWELL_SECONDS:
        identity["observation_dwell_s"] = observation_dwell_s
    if observation_start_s is not None:
        identity["observation_start_s"] = observation_start_s
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"candidate-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _travel_time(ax: float, ay: float, bx: float, by: float, speed: float) -> float:
    # Navigation follows cardinal grid edges. Reserve 10% of the advertised
    # speed for execution overhead; obstacle detours still require replanning.
    return (abs(bx - ax) + abs(by - ay)) / (0.9 * speed)


def _navigation_time(
    ax: float, ay: float, bx: float, by: float, speed: float,
    start_direction: int | None, arrival_direction: int | None,
    quarter_turn_seconds: float,
) -> float:
    """Reserve cardinal travel and turns for either obstacle-free axis order.

    NED x is north, y is east; grid headings are east/south/west/north.
    Pursuit leaves an unknown heading, so reserve the worst initial turn.
    Obstacle detours, like before, remain a replanning concern.
    """
    travel = _travel_time(ax, ay, bx, by, speed)
    if quarter_turn_seconds == 0:
        return travel
    north = None if bx == ax else (3 if bx > ax else 1)
    east = None if by == ay else (0 if by > ay else 2)
    return travel + _navigation_turns(north, east, start_direction, arrival_direction) * quarter_turn_seconds


@cache
def _navigation_turns(
    north: int | None, east: int | None,
    start_direction: int | None, arrival_direction: int | None,
) -> int:
    # Only 3 * 3 * 5 * 5 discrete combinations, reused across the candidate DAG.
    legs = [direction for direction in (north, east) if direction is not None]

    def turns(left: int, right: int) -> int:
        delta = abs(left - right)
        return min(delta, 4 - delta)

    counts = []
    for initial in range(4) if start_direction is None else (start_direction,):
        for order in (legs, legs[::-1]):
            headings = [initial, *order]
            if arrival_direction is not None:
                headings.append(arrival_direction)
            counts.append(sum(turns(a, b) for a, b in pairwise(headings)))
    return max(counts)


def _vehicle_direction(vehicle: Mapping[str, Any]) -> int | None:
    heading = vehicle.get("heading_degrees")
    return None if heading is None else (round(float(heading) / 90) - 1) % 4


def _fixed_view_report_ids(
    environment: Mapping[str, Any], parameters: Mapping[str, Any],
    opportunities: Sequence[ObservationOpportunity],
    *, start_s: float | None = None, end_s: float | None = None,
) -> set[str]:
    return set(_fixed_view_observation_times(
        environment, parameters, opportunities, start_s=start_s, end_s=end_s,
    ))


def _fixed_view_observation_times(
    environment: Mapping[str, Any], parameters: Mapping[str, Any],
    opportunities: Sequence[ObservationOpportunity],
    *, start_s: float | None = None, end_s: float | None = None,
) -> dict[str, float]:
    views = environment.get("surveillance_views")
    if views is not None:
        by_id = {item.report_id: item for item in opportunities}
        times: dict[str, float] = {}
        for view in views:
            if not all(view[key] == parameters.get(key) for key in ("x", "y", "arrival_direction")):
                continue
            for report_id in view["report_ids"]:
                if report_id not in by_id:
                    continue
                time_s = by_id[report_id].time_s + view.get("observation_delay_s", 0)
                if (start_s is None or time_s >= start_s) and (end_s is None or time_s < end_s):
                    times[report_id] = min(times.get(report_id, time_s), time_s)
        return times
    return {
        item.report_id: item.time_s for item in opportunities
        if math.hypot(item.x - parameters["x"], item.y - parameters["y"])
        <= environment["controlled_vehicle"]["fov_radius"]
    }


def _public_reports(
    environment: Mapping[str, object], belief: ReportingReliabilitySnapshot
) -> tuple[_PublicReport, ...]:
    reports = environment.get("static_info")
    if not isinstance(reports, (list, tuple)):
        raise ValueError("Mission 1 planning requires static_info reports")
    by_ship = {ship.entity_id: ship for ship in belief.ships}
    valid: list[_PublicReport] = []
    seen: set[str] = set()
    for report in reports:
        if not isinstance(report, Mapping):
            raise ValueError("Mission 1 report must be an object")
        report_id = report.get("report_id")
        entity_id = report.get("entity_id")
        position = report.get("position")
        if (
            not isinstance(report_id, str)
            or not report_id
            or report_id in seen
            or entity_id not in by_ship
            or not isinstance(entity_id, int)
            or isinstance(entity_id, bool)
            or not isinstance(position, (list, tuple))
            or len(position) < 2
        ):
            continue
        seen.add(report_id)
        time_s = float(report["time"])
        x, y = float(position[0]), float(position[1])
        valid.append(
            _PublicReport(
                report_id=report_id,
                entity_id=entity_id,
                time_s=time_s,
                x=x,
                y=y,
            )
        )
    return tuple(valid)


def public_position_fix_anchors(environment, belief):
    """Known public geometry, not reports, checks, or future trajectory samples.

    Return (entity, sampled time, north, east) anchors shared by forecast
    preparation and omission-exposure bounds. Duplicate fixes add no credit.
    """
    known = {ship.entity_id for ship in belief.ships}
    now = float(environment["mission_time_seconds"])
    result = set()
    for fix in environment.get("world_model_info", {}).get("public_position_fixes", ()):
        entity = fix["entity_id"]
        sampled = float(fix["sampled_at_s"])
        if entity not in known or isinstance(entity, bool) or not 0 <= sampled <= now:
            continue
        x, y = float(fix["position"]["x"]), float(fix["position"]["y"])
        if math.isfinite(x) and math.isfinite(y):
            result.add((entity, sampled, x, y))
    return tuple(sorted(result))


def public_report_rates(
    environment: Mapping[str, object], belief: ReportingReliabilitySnapshot
) -> dict[int, float]:
    """Return report rates from each ship's complete valid public schedule."""

    schedules: dict[int, list[float]] = {ship.entity_id: [] for ship in belief.ships}
    for report in _public_reports(environment, belief):
        schedules[report.entity_id].append(report.time_s)
    return {
        entity_id: (
            (len(schedule) - 1) / (max(schedule) - min(schedule))
            if len(set(schedule)) >= 2
            else 0.0
        )
        for entity_id, schedule in schedules.items()
    }


def _opportunities(
    environment: Mapping[str, object], belief: ReportingReliabilitySnapshot,
) -> tuple[ObservationOpportunity, ...]:
    world = environment.get("world_model_info")
    checks = world.get("event_report_checks", ()) if isinstance(world, Mapping) else ()
    checked = {
        check.get("report_id")
        for check in checks
        if isinstance(check, Mapping) and isinstance(check.get("report_id"), str)
    }
    by_ship = {ship.entity_id: ship for ship in belief.ships}
    now = float(cast(Any, environment["mission_time_seconds"]))
    window = float(cast(Any, environment.get("observation_window_seconds", 0.0)))
    lookback = float(cast(Any, environment.get("event_check_window_seconds", 0.0)))
    if not math.isfinite(lookback) or lookback < 0:
        raise ValueError("event_check_window_seconds must be finite and nonnegative")
    reports = _public_reports(environment, belief)
    rates = public_report_rates(environment, belief)
    intervals = _unsearched_report_intervals(reports, checks, now, lookback)
    raw: list[tuple[str, int, float, float, float, float, float]] = []
    for report in reports:
        if (
            report.report_id in checked
            or report.time_s + window < now
        ):
            continue
        ship = by_ship[report.entity_id]
        raw.append(
            (
                report.report_id,
                report.entity_id,
                report.time_s,
                report.x,
                report.y,
                ship.mean,
                ship.expected_variance_reduction,
            )
        )
    max_estimation = max((item[6] for item in raw), default=0.0)
    counts: dict[int, int] = {}
    for _report_id, entity_id, *_rest in raw:
        counts[entity_id] = counts.get(entity_id, 0) + 1
    return tuple(
        ObservationOpportunity(
            report_id=report_id,
            entity_id=entity_id,
            time_s=time_s,
            x=x,
            y=y,
            recall=recall,
            estimation=estimation,
            variance=by_ship[entity_id].variance,
            information_schedule_count=counts[entity_id],
            omission_rate=by_ship[entity_id].expected_omission_probability * rates[entity_id],
            omission_lookback_s=lookback,
            omission_intervals=intervals[(entity_id, time_s)],
            utility=0.5 * recall
            + 0.5 * (estimation / max_estimation if max_estimation > 0.0 else 0.0),
        )
        for report_id, entity_id, time_s, x, y, recall, estimation in raw
    )


def _unsearched_report_intervals(
    reports: Sequence[_PublicReport], checks: Sequence[Any], now: float, lookback: float,
) -> dict[tuple[int, float], tuple[tuple[float, float], ...]]:
    """Disjoint public-epoch cells, minus detector lookbacks already evidenced.

    A co-timed batch owns (previous distinct public time, time]. Assigning the
    whole schedule before excluding checked/expired reports prevents another
    candidate reclaiming their intervals. Checks establish visibility only for
    their entity; no hidden event positions or corruption labels are used.
    """
    searched: dict[int, list[tuple[float, float]]] = {}
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        time_s, entity_id = check.get("checked_at_s"), check.get("entity_id")
        if isinstance(time_s, (int, float)) and time_s <= now and isinstance(entity_id, int):
            searched.setdefault(entity_id, []).append((max(0.0, time_s - lookback), time_s))
    previous: dict[int, float] = {}
    result: dict[tuple[int, float], tuple[tuple[float, float], ...]] = {}
    for entity_id, time_s in sorted({(r.entity_id, r.time_s) for r in reports}):
        pieces = [(previous.get(entity_id, 0.0), time_s)]
        previous[entity_id] = time_s
        for start, end in searched.get(entity_id, ()):
            pieces = [(a, b) for left, right in pieces
                      for a, b in ((left, min(right, start)), (max(left, end), right)) if a < b]
        result[(entity_id, time_s)] = tuple(pieces)
    return result


def _fixed_omission_yield(item: ObservationOpportunity, observation_s: float) -> float:
    duration = math.fsum(
        max(0.0, min(end, observation_s) - max(start, observation_s - item.omission_lookback_s))
        for start, end in item.omission_intervals
    )
    return item.omission_rate * duration


def merge_time_intervals(intervals):
    merged = []
    for start, end in sorted(set(intervals)):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def _subtract_intervals(intervals, excluded):
    pieces = list(intervals)
    for start, end in excluded:
        pieces = [(a, b) for left, right in pieces
                  for a, b in ((left, min(right, start)), (max(left, end), right)) if a < b]
    return tuple(pieces)


def holding_exposures(environment, belief):
    """Weight public native-visibility forecasts, excluding all report lookbacks.

    Report-owned omission cells keep their existing allocation. Holding only
    earns exposure outside their union, so even a later check at another pose
    cannot recredit that interval. Current/future holds exclude elapsed time.
    """
    by_ship = {ship.entity_id: ship for ship in belief.ships}
    rates = public_report_rates(environment, belief)
    lookback = float(environment.get("event_check_window_seconds", 0))
    reserved = {}
    activity_span = {}
    for report in _public_reports(environment, belief):
        reserved.setdefault(report.entity_id, []).append((max(0, report.time_s - lookback), report.time_s))
        first, last = activity_span.get(report.entity_id, (report.time_s, report.time_s))
        activity_span[report.entity_id] = min(first, report.time_s), max(last, report.time_s)
    for ship, sampled, _x, _y in public_position_fix_anchors(environment, belief):
        if ship in activity_span:
            first, last = activity_span[ship]
            activity_span[ship] = min(first, sampled), last
    reserved = {ship: merge_time_intervals(rows) for ship, rows in reserved.items()}
    raw = {}
    for view in environment.get("surveillance_views", ()):
        pose = (view["x"], view["y"], view["arrival_direction"])
        for interval in view.get("holding_intervals", ()):
            ship = interval["entity_id"]
            if ship in by_ship:
                raw.setdefault((pose, ship), []).append((interval["start_s"], interval["end_s"]))
    result = {}
    for (pose, ship), intervals in raw.items():
        rate = by_ship[ship].expected_omission_probability * rates[ship]
        if rate == 0:
            continue
        first, last = activity_span[ship]
        for start, end in _subtract_intervals(merge_time_intervals(intervals), reserved.get(ship, ())):
            start, end = max(first, start), min(last, end)
            if start < end:
                result.setdefault(pose, []).append((ship, start, end, rate))
    return result


def score_holding_exposure(exposures, windows, *, now=-math.inf, claimed=None):
    """Round per selected window; its last capture precedes departure by one tick."""
    if claimed is None:
        claimed = {}
    units = 0
    for start, end in windows:
        end -= OBSERVATION_DWELL_SECONDS
        start = max(start, now)
        gain = 0.0
        for ship, left, right, rate in exposures:
            lo, hi = max(start, left), min(end, right)
            if lo >= hi:
                continue
            pieces = _subtract_intervals(((lo, hi),), claimed.get(ship, ()))
            gain += rate * math.fsum(b - a for a, b in pieces)
            claimed[ship] = merge_time_intervals((*claimed.get(ship, ()), *pieces))
        units += round(gain * SCORE_SCALE)
    return units / SCORE_SCALE


def score_candidate_opportunities(
    covered: Sequence[ObservationOpportunity],
    *,
    expected_omission_probability: float = 0.0,
    public_report_rate: float = 0.0,
    observation_start_s: float | None = None,
) -> CandidateUtility:
    """Score public reports and one pursuit interval's expected hidden yield."""

    ordered = tuple(sorted(covered, key=lambda item: (item.time_s, item.report_id)))
    recall = math.fsum(0.5 * item.recall for item in ordered)
    batches: dict[tuple[int, float], list[ObservationOpportunity]] = {}
    for item in ordered:
        batches.setdefault((item.entity_id, item.time_s), []).append(item)
    estimation = math.fsum(_batch_information_value(items) for items in batches.values())
    report_span = ordered[-1].time_s - ordered[0].time_s if len(ordered) >= 2 else 0.0
    if ordered and observation_start_s is not None:
        report_span = max(0.0, ordered[-1].time_s - observation_start_s)
    return CandidateUtility(
        recall=recall,
        estimation=estimation,
        omission_yield=(
            expected_omission_probability * public_report_rate * report_span
        ),
    )


def information_curve(variance: float, gain: float, normalizer: float, count: int) -> list[float]:
    """Selected-count additive-precision gain G(k), with existing 50% weight.

    This bounds repeated measurements of the same vessel. It is not exact
    Bayesian lookahead and does not value downstream decisions after evidence.
    """
    if variance <= 0 or gain <= 0 or normalizer <= 0:
        return [0.0] * (count + 1)
    return [0.0] + [
        0.5 * variance * k * gain / (variance + (k - 1) * gain) / normalizer
        for k in range(1, count + 1)
    ]


def route_information_tables(opportunities: Sequence[ObservationOpportunity]) -> dict[int, tuple[int, ...]]:
    """Integer information budgets indexed by actual selected report counts."""
    by_ship: dict[int, list[ObservationOpportunity]] = {}
    for item in opportunities:
        by_ship.setdefault(item.entity_id, []).append(item)
    normalizer = max((item.estimation for item in opportunities), default=0.0)
    return {
        entity: tuple(round(value * SCORE_SCALE) for value in information_curve(
            items[0].variance, items[0].estimation, normalizer, len(items),
        ))
        for entity, items in by_ship.items()
    }


def _information_capture_eligible(observation_s: float, deadline_s: float | None) -> bool:
    return deadline_s is None or observation_s + OBSERVATION_DWELL_SECONDS <= deadline_s + 1e-9


def _information_reports(candidate, by_id, deadline_s):
    return tuple(r for r in candidate.report_ids if _information_capture_eligible(
        by_id[r].time_s + candidate.observation_delay_s, deadline_s))


def allocate_route_information(
    selected: Sequence[SurveillanceCandidate], opportunities: Sequence[ObservationOpportunity],
    *, information_deadline_s: float | None = None,
) -> tuple[SurveillanceCandidate, ...]:
    """Assign each route item its marginal information, with no duplicate credit.

    Round cumulative vessel budgets before differencing so assignment credits
    telescope exactly to the integer route score, including after view merging.
    Existing recall and omission components remain untouched.
    """
    by_id = {item.report_id: item for item in opportunities}
    tables = route_information_tables(opportunities)
    counts = dict.fromkeys(tables, 0)
    seen: set[str] = set()
    result = []
    for candidate in selected:
        credit = 0
        for report_id in candidate.report_ids:
            if report_id in seen:
                raise ValueError("selected route repeats a public report")
            if report_id not in by_id:
                raise ValueError("selected route references unavailable public reports")
            seen.add(report_id)
            if not _information_capture_eligible(
                by_id[report_id].time_s + candidate.observation_delay_s, information_deadline_s,
            ):
                continue
            entity = by_id[report_id].entity_id
            before = counts[entity]
            counts[entity] += 1
            credit += tables[entity][before + 1] - tables[entity][before]
        estimation = credit / SCORE_SCALE
        result.append(replace(candidate, estimation_utility=estimation,
                              combined_score=candidate.recall_utility + estimation + candidate.omission_yield))
    return tuple(result)


def _batch_information_value(items: Sequence[ObservationOpportunity]) -> float:
    """Share a saturating information budget across remaining public reports.

    If V is current variance and g the one-check expected reduction, additive
    measurement precision gives G(n) = V*n*g / (V + (n-1)*g). It matches g at
    n=1 and saturates below V. Retain the existing normalization and 50% weight.
    With N remaining reports, a batch of n earns n/N of G(N). This keeps the
    same full-schedule cap without discounting later unobserved epochs. A subset
    can still be undervalued; this is not route-conditioned Bayesian lookahead.
    """
    first = items[0]
    one = first.utility - 0.5 * first.recall
    count = len(items)
    # Standalone opportunities default to a one-report schedule; a supplied
    # batch itself establishes a lower bound on that schedule's size.
    total = max(count, first.information_schedule_count)
    if total == 1:
        return one
    if first.variance <= 0 or first.estimation <= 0:
        return 0.0
    return count * one * first.variance / (
        first.variance + (total - 1) * first.estimation
    )


def _score_units(utility: CandidateUtility) -> int:
    return sum(
        round(component * SCORE_SCALE)
        for component in (utility.recall, utility.estimation, utility.omission_yield)
    )


def score_fixed_view_opportunities(
    covered: Sequence[ObservationOpportunity],
    *, observation_delay_s: float = 0.0,
    observation_times: Mapping[str, float] | None = None,
) -> CandidateUtility:
    """Round each report-time block once, also when rescoring a sustained view."""
    by_time: dict[float, list[ObservationOpportunity]] = {}
    for item in covered:
        by_time.setdefault(item.time_s, []).append(item)
    utilities = [score_candidate_opportunities(items) for items in by_time.values()]
    omission_units = 0
    for items in by_time.values():
        # Multiple public reports at one entity/epoch expose the same interval.
        by_entity: dict[int, float] = {}
        for item in items:
            observed = (observation_times[item.report_id] if observation_times is not None
                        else item.time_s + observation_delay_s)
            by_entity[item.entity_id] = max(by_entity.get(item.entity_id, 0.0),
                                            _fixed_omission_yield(item, observed))
        omission_units += round(math.fsum(by_entity.values()) * SCORE_SCALE)
    return CandidateUtility(
        sum(round(item.recall * SCORE_SCALE) for item in utilities) / SCORE_SCALE,
        sum(round(item.estimation * SCORE_SCALE) for item in utilities) / SCORE_SCALE,
        omission_units / SCORE_SCALE,
    )


def _candidate_utility(candidate: SurveillanceCandidate) -> CandidateUtility:
    return CandidateUtility(
        candidate.recall_utility,
        candidate.estimation_utility,
        candidate.omission_yield,
    )


def _prune_dominated_arcs(
    arcs: set[tuple[int, int]],
    candidates: Sequence[SurveillanceCandidate],
    sink: int,
) -> tuple[tuple[int, int], ...]:
    """Drop arcs whose route can always gain a positive compatible candidate."""

    outgoing = [0] * (sink + 1)
    incoming = [0] * (sink + 1)
    for source, target in arcs:
        outgoing[source] |= 1 << target
        incoming[target] |= 1 << source
    positive_candidates = 0
    for node, candidate in enumerate(candidates, start=1):
        if _score_units(_candidate_utility(candidate)) > 0:
            positive_candidates |= 1 << node
    return tuple(
        sorted(
            (source, target)
            for source, target in arcs
            if not outgoing[source] & incoming[target] & positive_candidates
        )
    )


def _candidate(
    mode: str,
    covered: Sequence[ObservationOpportunity],
    *,
    x: float,
    y: float,
    end_x: float,
    end_y: float,
    entity_id: int | None,
    target_posterior_risk: float = 0.0,
    expected_omission_probability: float = 0.0,
    public_report_rate: float = 0.0,
    arrival_direction: int | None = None,
    observation_delay_s: float = 0.0,
    observation_dwell_s: float = OBSERVATION_DWELL_SECONDS,
    holding_omission_yield: float = 0.0,
) -> SurveillanceCandidate:
    ordered = tuple(sorted(covered, key=lambda item: (item.time_s, item.report_id)))
    report_ids = tuple(item.report_id for item in ordered)
    utility = (
        score_fixed_view_opportunities(ordered, observation_delay_s=observation_delay_s)
        if mode == "fixed_view" else score_candidate_opportunities(
            ordered,
            expected_omission_probability=expected_omission_probability,
            public_report_rate=public_report_rate,
        )
    )
    report_span = ordered[-1].time_s - ordered[0].time_s
    utility = replace(utility, omission_yield=utility.omission_yield + holding_omission_yield)
    start_s = ordered[0].time_s + observation_delay_s
    end_s = ordered[-1].time_s + observation_delay_s + observation_dwell_s
    return SurveillanceCandidate(
        candidate_id=_candidate_id(
            mode, report_ids,
            viewpoint=(round(x), round(y)) if mode == "fixed_view" else None,
            arrival_direction=arrival_direction,
            observation_delay_s=observation_delay_s,
            observation_dwell_s=observation_dwell_s,
        ),
        mode=mode,
        entity_id=entity_id,
        start_s=start_s,
        end_s=end_s,
        x=x,
        y=y,
        end_x=end_x,
        end_y=end_y,
        report_ids=report_ids,
        target_posterior_risk=target_posterior_risk,
        expected_omission_probability=expected_omission_probability,
        public_report_rate=public_report_rate,
        report_span_s=report_span,
        recall_utility=utility.recall,
        estimation_utility=utility.estimation,
        omission_yield=utility.omission_yield,
        combined_score=utility.combined,
        arrival_direction=arrival_direction,
        observation_delay_s=observation_delay_s,
        scored_observation_windows=((start_s, end_s),) if observation_dwell_s > OBSERVATION_DWELL_SECONDS else (),
    )


def _candidate_arcs(
    candidates: Sequence[SurveillanceCandidate],
    speed: float,
    quarter_turn_seconds: float = 0.0,
    chronological_reports: bool = False,
    *,
    information_aware: bool = False,
    report_history_aware: bool = False,
) -> tuple[tuple[int, int], ...]:
    """Build the same reduced arcs without materializing the dense closure.

    Generated candidates end with observation dwell; disjoint time windows
    cannot repeat a report when report epochs are strictly ordered. Otherwise
    For report-history-aware callers, an intermediate must be new after the
    source finishes and its batches must expire before the bypassed target.
    Otherwise defer reduction until batch histories are lifted.
    Backward traversal reuses each target's reachable
    successors: a compatible path through a positive-utility intermediate
    dominates a direct arc to the same successor. Zero-utility candidates
    must not erase a shorter equivalent route.
    """
    sink = len(candidates) + 1
    sink_bit = 1 << sink
    all_nodes = (1 << (sink + 1)) - 1
    reachable = [0] * (sink + 1)
    history_reachable = [0] * (sink + 1)
    starts = [candidate.start_s + 1e-9 for candidate in candidates]
    max_delay = max((c.observation_delay_s for c in candidates), default=0.0)
    # No alternative containing an intermediate's batch can start after its
    # last report epoch plus the largest offered observation delay. Witness
    # chains must also introduce reports strictly after each preceding finish;
    # unrestricted reachability could itself repeat a batch. Intersecting each
    # witness's expiry mask protects all inserted batches from the suffix.
    safe_successors = [0] + [
        all_nodes
        & ~(
            (1 << (bisect_right(starts, c.last_report_time_s + max_delay + 1e-9) + 1))
            - 1
        )
        if c.report_ids
        else all_nodes
        for c in candidates
    ]
    reports = [frozenset(candidate.report_ids) for candidate in candidates]
    positive = (
        [False]
        + [
            (
                _score_units(replace(_candidate_utility(candidate), estimation=0.0))
                if information_aware
                else _score_units(_candidate_utility(candidate))
            )
            > 0
            for candidate in candidates
        ]
        + [False]
    )
    xs = np.array([c.x for c in candidates], dtype=float)
    ys = np.array([c.y for c in candidates], dtype=float)
    start_times = np.array([c.start_s for c in candidates], dtype=float)
    report_epochs = np.array([c.start_s - c.observation_delay_s for c in candidates])
    arrival = np.array(
        [4 if c.arrival_direction is None else c.arrival_direction for c in candidates],
        dtype=int,
    )
    turns = np.array(
        [
            [
                [
                    [
                        _navigation_turns(north, east, initial, final)
                        for final in (0, 1, 2, 3, None)
                    ]
                    for initial in (0, 1, 2, 3, None)
                ]
                for east in (2, None, 0)
            ]
            for north in (1, None, 3)
        ]
    )
    report_nodes: dict[str, int] = {}
    for i, batch in enumerate(reports, 1):
        for report in batch:
            report_nodes[report] = report_nodes.get(report, 0) | (1 << i)
    positive_bits = sum(1 << i for i, value in enumerate(positive) if value)
    arcs: list[tuple[int, int]] = []
    for source in range(sink - 1, 0, -1):
        left = candidates[source - 1]
        first = bisect_left(starts, left.end_s) + 1
        travel = (np.abs(xs - left.end_x) + np.abs(ys - left.end_y)) / (0.9 * speed)
        if quarter_turn_seconds:
            north = np.sign(xs - left.end_x).astype(int) + 1
            east = np.sign(ys - left.end_y).astype(int) + 1
            initial = 4 if left.arrival_direction is None else left.arrival_direction
            travel += turns[north, east, initial, arrival] * quarter_turn_seconds
        feasible = start_times + 1e-9 >= left.end_s + travel
        if chronological_reports:
            feasible &= report_epochs > left.last_report_time_s + 1e-9
        pending = (
            int.from_bytes(np.packbits(feasible, bitorder="little").tobytes(), "little")
            << 1
        ) | sink_bit
        pending &= ~((1 << first) - 1)
        overlap = 0
        for report in reports[source - 1]:
            overlap |= report_nodes[report]
        pending &= ~overlap
        # Only positive intermediates can remove a direct arc. Process those
        # choices first, leaving zero-utility arcs intact for all tie objectives.
        positive_pending = pending & positive_bits
        history_successors = sink_bit
        while positive_pending:
            bit = positive_pending & -positive_pending
            target = bit.bit_length() - 1
            right = candidates[target - 1]
            if not report_history_aware:
                pending &= ~reachable[target]
            elif (
                not right.report_ids
                or right.start_s - right.observation_delay_s > left.end_s + 1e-9
            ):
                history_successors |= history_reachable[target]
                pending &= ~history_reachable[target]
            positive_pending &= pending & ~bit
        targets = np.flatnonzero(
            np.unpackbits(
                np.frombuffer(
                    pending.to_bytes((sink + 8) // 8, "little"), dtype=np.uint8
                ),
                bitorder="little",
            )
        ).tolist()
        arcs.extend(zip(repeat(source), targets))
        history_successors |= pending
        successors = pending | sink_bit
        closure_pending = pending & ~sink_bit
        while closure_pending:
            bit = closure_pending & -closure_pending
            target = bit.bit_length() - 1
            successors |= reachable[target]
            closure_pending &= ~(bit | reachable[target])
        reachable[source] = successors
        history_reachable[source] = history_successors & safe_successors[source]
    # Every candidate has already passed initial-pose/time admission.
    pending = all_nodes & ~1
    while pending:
        bit = pending & -pending
        pending ^= bit
        target = bit.bit_length() - 1
        arcs.append((0, target))
        if positive[target] and not report_history_aware:
            pending &= ~reachable[target]
        elif positive[target]:
            # The source has no selected-report history.
            pending &= ~history_reachable[target]
    ordered_arcs = tuple(sorted(arcs))
    return (
        _prune_terminal_alternatives(candidates, ordered_arcs)
        if chronological_reports and not information_aware
        else ordered_arcs
    )


def _prune_terminal_alternatives(
    candidates: Sequence[SurveillanceCandidate], arcs: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Remove locally dominated choices with no possible later observation.

    Compare the complete remaining lexicographic cost from each predecessor.
    No best prefix/advisory route is imposed. Candidate indices are retained,
    including their deterministic tie-break meaning. A terminal made orphaned
    by dominance keeps its initially feasible source arc so model potentials
    still have a defined incoming recurrence for every candidate.
    """
    sink = len(candidates) + 1
    nonterminal = {source for source, target in arcs if target != sink}
    terminal = set(range(1, sink)) - nonterminal
    nodes = _route_nodes(candidates)
    best = {}
    for source, target in arcs:
        if target in terminal:
            score, count, duration, order = _route_cost(nodes, source, target)
            priority = score, -count, -duration, -order
            if source not in best or priority > best[source][0]:
                best[source] = priority, target
    kept = {(source, target) for source, target in arcs
            if target not in terminal or best[source][1] == target}
    incoming = {target for _, target in kept}
    kept.update((0, target) for target in terminal - incoming)
    return tuple(sorted(kept))


def sample_fixed_viewpoints(
    opportunities: Sequence[ObservationOpportunity], radius: float,
    current_position: tuple[float, float],
) -> tuple[tuple[int, int], ...]:
    """Public-schedule sampling shared with offline native-camera evaluation."""
    viewpoints = {
        (round(report.x), round(report.y)) for report in opportunities
    } | {(round(current_position[0]), round(current_position[1]))}
    for anchor in opportunities:
        simultaneous = tuple(
            report for report in opportunities
            if abs(report.time_s - anchor.time_s) <= OBSERVATION_DWELL_SECONDS
        )
        viewpoints.update(
            (round((anchor.x + other.x) / 2), round((anchor.y + other.y) / 2))
            for other in simultaneous
            if math.hypot(anchor.x - other.x, anchor.y - other.y) <= 2 * radius
        )
    return tuple(sorted(viewpoints))


def _fixed_view_candidates(
    opportunities: Sequence[ObservationOpportunity],
    radius: float,
    current_position: tuple[float, float],
    surveillance_views: Sequence[Mapping[str, Any]] | None = None,
    dwell_options: Sequence[float] = (OBSERVATION_DWELL_SECONDS,),
    exposures: Mapping | None = None,
) -> tuple[SurveillanceCandidate, ...]:
    """Reuse sampled report centres/midpoints and the current viewpoint over time.

    A report need not be reachable at its own position to be observable.
    Quantize viewpoints before checking coverage, matching MiniZinc output.
    Keep distinct locations even when they cover identical reports: their
    connections to earlier/later assignments can differ. A location sampled
    from one report is also a valid viewpoint for other visible report times.
    """
    candidates: dict[str, SurveillanceCandidate] = {}
    if surveillance_views is not None:
        # Supplied by the world-model visibility evaluator, using public report
        # positions only. An empty table means no visible opportunities, not
        # permission to fall back to omnidirectional radius coverage.
        for view in surveillance_views:
            direction = view["arrival_direction"]
            if type(direction) is not int or direction not in range(4):
                raise ValueError("surveillance view arrival_direction must be an integer in 0..3")
            x, y = view["x"], view["y"]
            if x != round(x) or y != round(y):
                raise ValueError("surveillance viewpoints must use integer output coordinates")
            visible_ids = set(view["report_ids"])
            by_time: dict[float, list[ObservationOpportunity]] = {}
            for report in opportunities:
                if report.report_id in visible_ids:
                    by_time.setdefault(report.time_s, []).append(report)
            for covered in by_time.values():
                delay = float(view.get("observation_delay_s", 0.0))
                start_s = covered[0].time_s + delay
                for dwell in dwell_options:
                    extra = 0.0 if dwell == OBSERVATION_DWELL_SECONDS else score_holding_exposure(
                        (exposures or {}).get((x, y, direction), ()), ((start_s, start_s + dwell),),
                    )
                    # Longer occupancy with no extra value cannot beat its short
                    # counterpart and only removes feasible continuations.
                    if dwell > OBSERVATION_DWELL_SECONDS and extra == 0:
                        continue
                    candidate = _candidate(
                        "fixed_view", covered, x=x, y=y, end_x=x, end_y=y,
                        entity_id=None, arrival_direction=direction,
                        observation_delay_s=delay, observation_dwell_s=dwell,
                        holding_omission_yield=extra,
                    )
                    candidates[candidate.candidate_id] = candidate
            for window in view.get("gap_observation_windows", ()):
                start, end = float(window["start_s"]), float(window["end_s"])
                if (not all(math.isfinite(t) and t * TIME_SCALE == round(t * TIME_SCALE)
                            for t in (start, end)) or end - start < 2 * OBSERVATION_DWELL_SECONDS):
                    raise ValueError("gap windows must use half-second times and at least one second of dwell")
                extra = score_holding_exposure(
                    (exposures or {}).get((x, y, direction), ()), ((start, end),),
                )
                if extra <= 0:
                    continue
                identity = _candidate_id(
                    "fixed_view", (), viewpoint=(round(x), round(y)), arrival_direction=direction,
                    observation_start_s=start, observation_dwell_s=end - start,
                )
                candidates[identity] = SurveillanceCandidate(
                    candidate_id=identity, mode="fixed_view", entity_id=None,
                    start_s=start, end_s=end, x=x, y=y, end_x=x, end_y=y,
                    report_ids=(), target_posterior_risk=0.0,
                    expected_omission_probability=0.0, public_report_rate=0.0,
                    report_span_s=0.0, recall_utility=0.0, estimation_utility=0.0,
                    omission_yield=extra, combined_score=extra,
                    arrival_direction=direction, scored_observation_windows=((start, end),),
                )
        return tuple(candidates.values())
    for x, y in sample_fixed_viewpoints(opportunities, radius, current_position):
        visible = tuple(
            report for report in opportunities
            if math.hypot(report.x - x, report.y - y) <= radius
        )
        by_time: dict[float, list[ObservationOpportunity]] = {}
        for report in visible:
            by_time.setdefault(report.time_s, []).append(report)
        for covered in by_time.values():
            candidate = _candidate(
                "fixed_view", covered, x=x, y=y, end_x=x, end_y=y, entity_id=None,
            )
            candidates[candidate.candidate_id] = candidate
    return tuple(candidates.values())


def build_candidate_dag(
    environment: Mapping[str, object], belief: ReportingReliabilitySnapshot,
    *, information_horizon_seconds: float | None = None,
    route_horizon_seconds: float | None = None,
    positive_only: bool = False,
) -> CandidateDAG:
    """Build sampled fixed views and pursuits, optionally with bounded route information.

    A supplied information horizon normally bounds candidate finish times. An
    optional longer route horizon retains later recall/omission opportunities,
    but only captures finishing within the information horizon earn information
    credit. Public schedules and belief normalization remain complete. The
    unconfigured default retains full-schedule additive scoring.
    """

    if belief.belief_kind != "reporting_reliability":
        raise ValueError("Mission 1 planning requires a reporting reliability belief")
    vehicle = environment.get("controlled_vehicle")
    if not isinstance(vehicle, Mapping) or not isinstance(
        vehicle.get("position"), Mapping
    ):
        raise ValueError("Mission 1 planning requires controlled vehicle state")
    position = vehicle["position"]
    speed = float(vehicle["max_velocity"])
    turn_seconds = float(vehicle.get("quarter_turn_seconds", 0.0))
    observation_window = float(cast(Any, environment.get("observation_window_seconds", 0.0)))
    views = environment.get("surveillance_views")
    gap_views = [v for v in cast(Any, views or ()) if v.get("gap_observation_windows")]
    if any("holding_intervals" not in v for v in gap_views):
        raise ValueError("gap windows require public holding interval forecasts")
    dwell_options = sorted(set(cast(Any, environment.get("fixed_view_dwell_options_s", (.5,)))))
    if not dwell_options or dwell_options[0] != OBSERVATION_DWELL_SECONDS or any(
        not math.isfinite(d) or d < OBSERVATION_DWELL_SECONDS or d * TIME_SCALE != round(d * TIME_SCALE)
        for d in dwell_options
    ):
        raise ValueError("fixed-view dwell options must include 0.5 and use positive half-second steps")
    if len(dwell_options) > 1 and (views is None or any("holding_intervals" not in v for v in cast(Any, views))):
        raise ValueError("long fixed-view dwells require public holding interval forecasts")
    if (len(dwell_options) > 1 or gap_views) and float(cast(Any, environment.get("event_check_window_seconds", 0))) < OBSERVATION_DWELL_SECONDS:
        raise ValueError("holding requires at least one observation tick of detector lookback")
    if observation_window < 0 or not math.isfinite(observation_window):
        raise ValueError("observation window must be finite and nonnegative")
    if observation_window > 0 and views is None:
        raise ValueError("observation windows require forecast surveillance views")
    if views is not None and any(
        not 0 <= float(view.get("observation_delay_s", 0.0)) <= observation_window
        or round(float(view.get("observation_delay_s", 0.0)) * TIME_SCALE)
        != float(view.get("observation_delay_s", 0.0)) * TIME_SCALE
        for view in cast(Any, views)
    ):
        raise ValueError("view observation delay is outside the declared window")
    if "surveillance_views" in environment and (not math.isfinite(turn_seconds) or turn_seconds <= 0):
        raise ValueError("sensor-aware planning requires positive quarter_turn_seconds")
    direction = _vehicle_direction(vehicle)
    fov = float(vehicle["fov_radius"])
    now = float(cast(Any, environment["mission_time_seconds"]))
    if route_horizon_seconds is not None and (
        information_horizon_seconds is None or not math.isfinite(route_horizon_seconds)
        or route_horizon_seconds < information_horizon_seconds
    ):
        raise ValueError("route horizon requires a finite horizon at least as long as the information horizon")
    planning_horizon = route_horizon_seconds if route_horizon_seconds is not None else information_horizon_seconds
    if information_horizon_seconds is not None and (
        not math.isfinite(information_horizon_seconds) or information_horizon_seconds <= 0
    ):
        raise ValueError("information planning horizon must be finite and positive")
    start_x, start_y = float(position["x"]), float(position["y"])
    opportunities = _opportunities(environment, belief)
    report_rates = public_report_rates(environment, belief)
    candidates: dict[str, SurveillanceCandidate] = {}

    for item in _fixed_view_candidates(
        opportunities, fov, (start_x, start_y),
        cast(Any, environment.get("surveillance_views")),
        dwell_options, holding_exposures(environment, belief) if len(dwell_options) > 1 or gap_views else {},
    ):
        if (
            now + _navigation_time(start_x, start_y, item.x, item.y, speed,
                                   direction, item.arrival_direction, turn_seconds)
            <= item.start_s + 1e-9
        ):
            candidates[item.candidate_id] = item

    by_ship = {ship.entity_id: ship for ship in belief.ships}
    for entity_id in sorted(by_ship):
        ordered = tuple(
            sorted(
                (item for item in opportunities if item.entity_id == entity_id),
                key=lambda item: (item.time_s, item.report_id),
            )
        )
        ship = by_ship[entity_id]
        for start_index in range(len(ordered) - 1):
            first = ordered[start_index]
            if (
                now + _navigation_time(start_x, start_y, first.x, first.y, speed,
                                       direction, None, turn_seconds)
                > first.time_s + 1e-9
            ):
                continue
            for end_index in range(start_index + 1, len(ordered)):
                previous = ordered[end_index - 1]
                following = ordered[end_index]
                if (
                    _navigation_time(
                        previous.x, previous.y, following.x, following.y, speed,
                        None, None, turn_seconds,
                    )
                    > following.time_s - previous.time_s + 1e-9
                ):
                    break
                window = ordered[start_index : end_index + 1]
                item = _candidate(
                    "pursue_ship",
                    window,
                    x=first.x,
                    y=first.y,
                    end_x=following.x,
                    end_y=following.y,
                    entity_id=entity_id,
                    target_posterior_risk=ship.mean,
                    expected_omission_probability=(ship.expected_omission_probability),
                    public_report_rate=report_rates[entity_id],
                )
                candidates[item.candidate_id] = item

    # A public GPS fix is an acquisition/search hint, not a synthetic report
    # or a guaranteed future ship position. Retain the report-start windows
    # and also value starting pursuit at an earlier reachable GPS rendezvous.
    # Actual acquisition and recovery remain Maneuver Control's responsibility.
    latest_fixes = {}
    for entity, sampled, x, y in public_position_fix_anchors(environment, belief):
        if entity not in latest_fixes or sampled > latest_fixes[entity][0]:
            latest_fixes[entity] = sampled, round(x), round(y)
    opportunities_by_id = {item.report_id: item for item in opportunities}
    for item in tuple(candidates.values()):
        if item.mode != "pursue_ship" or item.entity_id not in latest_fixes:
            continue
        _sampled, x, y = latest_fixes[item.entity_id]
        arrival = math.ceil((now + _navigation_time(
            start_x, start_y, x, y, speed, direction, None, turn_seconds,
        )) * TIME_SCALE) / TIME_SCALE
        if arrival >= item.start_s or arrival + _navigation_time(
            x, y, item.x, item.y, speed, None, None, turn_seconds,
        ) > item.start_s + 1e-9:
            continue
        utility = score_candidate_opportunities(
            tuple(opportunities_by_id[report] for report in item.report_ids),
            expected_omission_probability=item.expected_omission_probability,
            public_report_rate=item.public_report_rate,
            observation_start_s=arrival,
        )
        identity = _candidate_id("pursue_ship", item.report_ids,
                                 viewpoint=(x, y), observation_start_s=arrival)
        candidates[identity] = replace(item, candidate_id=identity, start_s=arrival,
            x=x, y=y, recall_utility=utility.recall, estimation_utility=utility.estimation,
            omission_yield=utility.omission_yield, combined_score=utility.combined)

    ordered_candidates = tuple(
        sorted(
            (candidate for candidate in candidates.values()
             if (planning_horizon is None or candidate.end_s <= now + planning_horizon)
             and (
                 not positive_only
                 or _score_units(_candidate_utility(candidate)) > 0
             )),
            key=lambda item: (item.start_s, item.end_s, item.mode, item.candidate_id),
        )
    )
    source = 0
    sink = len(ordered_candidates) + 1
    graph = CandidateDAG(
        ordered_candidates,
        _candidate_arcs(ordered_candidates, speed, turn_seconds,
                        observation_window > 0 and information_horizon_seconds is None,
                        information_aware=information_horizon_seconds is not None,
                        report_history_aware=information_horizon_seconds is not None),
        source,
        sink,
    )
    return expand_information_states(
        graph, opportunities,
        information_deadline_s=now + information_horizon_seconds,
    ) if route_horizon_seconds is not None else (
        expand_information_states(graph, opportunities) if information_horizon_seconds is not None else graph
    )


@dataclass(frozen=True, slots=True)
class _RouteNode:
    """Integer objective inputs shared with the serialized-data inspector."""

    score: int
    start: int
    duration: int
    mode: str
    x: int
    y: int
    arrival_direction: int | None = None


def _route_nodes(candidates: Sequence[SurveillanceCandidate]) -> tuple[_RouteNode, ...]:
    return tuple(
        _RouteNode(_score_units(_candidate_utility(c)), round(c.start_s * TIME_SCALE),
                   round(c.duration_s * TIME_SCALE), c.mode, round(c.x), round(c.y),
                   c.arrival_direction)
        for c in candidates
    )


def _same_fixed_view(
    left: _RouteNode | SurveillanceCandidate, right: _RouteNode | SurveillanceCandidate,
) -> bool:
    return (
        left.mode == right.mode == "fixed_view"
        and left.x == right.x and left.y == right.y
        and left.arrival_direction == right.arrival_direction
    )


def _route_cost(
    nodes: Sequence[_RouteNode], source: int, target: int,
) -> tuple[int, int, int, int]:
    if target == len(nodes) + 1:
        return (0, 0, 0, 0)
    right = nodes[target - 1]
    left = nodes[source - 1] if source else None
    holding = left is not None and _same_fixed_view(left, right)
    duration = right.duration + (
        right.start - left.start - left.duration if holding and left is not None else 0
    )
    return right.score, int(not holding), duration, target


def _best_route(
    nodes: Sequence[_RouteNode],
    arcs: Sequence[tuple[int, int]],
) -> tuple[int, int, int, int, tuple[int, ...]]:
    """Exact lexicographic path with vectorized predecessor cost comparisons."""
    sink = len(nodes) + 1
    incoming = [[] for _ in range(sink + 1)]
    for source, target in arcs:
        incoming[target].append(source)
    scores = np.zeros(sink + 1, dtype=np.int64)
    maneuvers = np.zeros(sink + 1, dtype=np.int64)
    durations = np.zeros(sink + 1, dtype=np.int64)
    orders = np.zeros(sink + 1, dtype=np.int64)
    reached = np.zeros(sink + 1, dtype=bool)
    reached[0] = True
    node_x = np.array([0] + [n.x for n in nodes])
    node_y = np.array([0] + [n.y for n in nodes])
    arrival_directions = np.array(
        [-1]
        + [-1 if n.arrival_direction is None else n.arrival_direction for n in nodes]
    )
    fixed_views = np.array([False] + [n.mode == "fixed_view" for n in nodes])
    node_starts = np.array([0] + [n.start for n in nodes], dtype=np.int64)
    node_durations = np.array([0] + [n.duration for n in nodes], dtype=np.int64)
    paths: list[tuple[int, ...]] = [()] * (sink + 1)
    reverse_paths: list[tuple[int, ...]] = [()] * (sink + 1)
    for node in range(1, sink + 1):
        previous = np.array(incoming[node], dtype=np.intp)
        previous = previous[reached[previous]]
        if not len(previous):
            continue
        if node == sink:
            candidate_scores = scores[previous]
            candidate_maneuvers = maneuvers[previous]
            candidate_durations = durations[previous]
            candidate_orders = orders[previous]
        else:
            right = nodes[node - 1]
            holding = (
                fixed_views[previous]
                & fixed_views[node]
                & (node_x[previous] == node_x[node])
                & (node_y[previous] == node_y[node])
                & (arrival_directions[previous] == arrival_directions[node])
            )
            candidate_scores = scores[previous] + right.score
            candidate_maneuvers = maneuvers[previous] + (~holding)
            candidate_durations = (
                durations[previous]
                + right.duration
                + np.where(
                    holding,
                    right.start - node_starts[previous] - node_durations[previous],
                    0,
                )
            )
            candidate_orders = orders[previous] + node
        # Resolve each integer objective before comparing reversed paths.
        # All alternatives here append the same node, so only the predecessor
        # paths need comparison for the final deterministic tie break.
        choices = np.flatnonzero(candidate_scores == candidate_scores.max())
        choices = choices[
            candidate_maneuvers[choices] == candidate_maneuvers[choices].min()
        ]
        choices = choices[
            candidate_durations[choices] == candidate_durations[choices].min()
        ]
        choices = choices[candidate_orders[choices] == candidate_orders[choices].min()]
        selected = min(choices, key=lambda i: reverse_paths[previous[i]])
        parent = int(previous[selected])
        scores[node] = candidate_scores[selected]
        maneuvers[node] = candidate_maneuvers[selected]
        durations[node] = candidate_durations[selected]
        orders[node] = candidate_orders[selected]
        reached[node] = True
        paths[node] = paths[parent] if node == sink else paths[parent] + (node - 1,)
        reverse_paths[node] = (
            reverse_paths[parent]
            if node == sink
            else (node - 1,) + reverse_paths[parent]
        )
    if not reached[sink]:
        raise ValueError("Mission 1 candidate graph has no route")
    return (
        int(scores[sink]),
        int(maneuvers[sink]),
        int(durations[sink]),
        int(orders[sink]),
        paths[sink],
    )


def _fixed_view_runs(
    selected: Sequence[SurveillanceCandidate],
) -> tuple[SurveillanceCandidate, ...]:
    groups: list[list[SurveillanceCandidate]] = []
    for candidate in selected:
        if groups and _same_fixed_view(groups[-1][-1], candidate):
            groups[-1].append(candidate)
        else:
            groups.append([candidate])
    result: list[SurveillanceCandidate] = []
    for group in groups:
        first, last = group[0], group[-1]
        if len(group) == 1:
            result.append(first)
            continue
        recall = sum(round(c.recall_utility * SCORE_SCALE) for c in group) / SCORE_SCALE
        estimation = sum(round(c.estimation_utility * SCORE_SCALE) for c in group) / SCORE_SCALE
        omission = sum(round(c.omission_yield * SCORE_SCALE) for c in group) / SCORE_SCALE
        reported = [c for c in group if c.report_ids]
        result.append(replace(
            first, candidate_id=first.candidate_id + "--" + last.candidate_id,
            end_s=last.end_s,
            report_ids=tuple(r for c in group for r in c.report_ids),
            report_span_s=(max(c.start_s - c.observation_delay_s + c.report_span_s for c in reported)
                           - min(c.start_s - c.observation_delay_s for c in reported)) if reported else 0.0,
            recall_utility=recall, estimation_utility=estimation,
            omission_yield=omission, combined_score=recall + estimation + omission,
            scored_observation_windows=(tuple(w for c in group for w in
                (c.scored_observation_windows or ((c.start_s, c.end_s),)))
                if any(c.scored_observation_windows for c in group) else ()),
        ))
    return tuple(result)


def expand_information_states(
    graph: CandidateDAG, opportunities: Sequence[ObservationOpportunity],
    *, information_deadline_s: float | None = None,
) -> CandidateDAG:
    """Lift a bounded graph by selected counts for the existing additive model.

    Each lifted node fixes both its current observation and resulting count
    vector, making its marginal information a constant. Counts for entities
    absent from this and every later candidate can be forgotten: they cannot
    affect future rewards, and their earned utility remains in the path prefix.
    No advisory route or additive terminal dominance removes other histories.
    """
    tables = route_information_tables(opportunities)
    entities = sorted(tables)
    position = {entity: i for i, entity in enumerate(entities)}
    by_id = {item.report_id: item for item in opportunities}
    credited = [_information_reports(c, by_id, information_deadline_s) for c in graph.candidates]
    relevant: list[set[int]] = [set() for _ in graph.candidates]
    # Reports of one vessel at one epoch share a sensing/omission cell. Keep
    # their batch identity until its final alternative to prevent nonadjacent
    # report or interval reuse, without excluding other vessels at that epoch.
    batches = sorted({(item.entity_id, item.time_s) for item in opportunities})
    batch_bits = {batch: 1 << index for index, batch in enumerate(batches)}
    masks = [sum({batch_bits[(by_id[r].entity_id, by_id[r].time_s)] for r in c.report_ids})
             for c in graph.candidates]
    future_masks = [0] * (len(graph.candidates) + 1)
    observation_starts = [c.start_s + 1e-9 for c in graph.candidates]
    future: set[int] = set()
    for index in range(len(graph.candidates) - 1, -1, -1):
        future = future | {position[by_id[r].entity_id]
                           for r in credited[index]}
        relevant[index] = future
        future_masks[index] = future_masks[index + 1] | masks[index]
    incoming: list[list[int]] = [[] for _ in range(graph.sink + 1)]
    for source, target in graph.arcs:
        incoming[target].append(source)
    states: list[dict[tuple[tuple[int, ...], int], int]] = [{} for _ in range(graph.sink + 1)]
    states[graph.source][((0,) * len(entities), 0)] = 0
    candidates = []
    arcs = []
    for node, candidate in enumerate(graph.candidates, start=1):
        increment = [0] * len(entities)
        for report in credited[node - 1]:
            increment[position[by_id[report].entity_id]] += 1
        # Later indices at the same observation time cannot follow this node.
        # Keep history only for batches with a temporally possible successor.
        future_mask = future_masks[bisect_left(observation_starts, candidate.end_s)]
        connections: dict[tuple[tuple[int, ...], int], set[int]] = {}
        for previous in incoming[node]:
            for (counts, seen), lifted in states[previous].items():
                if seen & masks[node - 1]:
                    continue
                after = tuple(a + b if i in relevant[node - 1] else 0
                              for i, (a, b) in enumerate(zip(counts, increment)))
                remaining = (seen | masks[node - 1]) & future_mask
                connections.setdefault((after, remaining), set()).add(lifted)
        for (after, remaining), previous_nodes in sorted(connections.items()):
            credit = sum(tables[entity][after[i]] - tables[entity][after[i] - increment[i]]
                         for i, entity in enumerate(entities))
            estimation = credit / SCORE_SCALE
            label = ",".join(map(str, after))
            if remaining:
                label += f":seen:{remaining:x}"
            candidates.append(replace(candidate, candidate_id=f"{candidate.candidate_id}:counts:{label}",
                                      estimation_utility=estimation,
                                      combined_score=candidate.recall_utility + estimation + candidate.omission_yield))
            lifted = len(candidates)
            states[node][after, remaining] = lifted
            arcs.extend((previous, lifted) for previous in sorted(previous_nodes))
    sink = len(candidates) + 1
    for previous in incoming[graph.sink]:
        arcs.extend((lifted, sink) for lifted in states[previous].values())
    return CandidateDAG(tuple(candidates),
                        _prune_dominated_arcs(set(arcs), candidates, sink), 0, sink)


def route_information_oracle(
    graph: CandidateDAG, opportunities: Sequence[ObservationOpportunity],
    *, information_deadline_s: float | None = None,
) -> AdvisoryRoute:
    """Exact count-labelled path reference for a bounded candidate graph.

    The graph must preserve alternatives for history-dependent rewards: do not
    use additive terminal dominance or additive objective-potential pruning.
    Track vessel/epoch batches independently of counts to prevent reusing a
    report or its omission cell across delayed observation alternatives.
    """
    tables = route_information_tables(opportunities)
    entities = sorted(tables)
    positions = {entity: i for i, entity in enumerate(entities)}
    by_id = {item.report_id: item for item in opportunities}
    increments = []
    node_batches = []
    last_batch_node = {}
    for candidate in graph.candidates:
        counts = [0] * len(entities)
        for report in _information_reports(candidate, by_id, information_deadline_s):
            counts[positions[by_id[report].entity_id]] += 1
        increments.append(tuple(counts))
        batches = frozenset((by_id[r].entity_id, by_id[r].time_s) for r in candidate.report_ids)
        node_batches.append(batches)
        for batch in batches:
            last_batch_node[batch] = len(increments)
    nodes = tuple(replace(node, score=round(candidate.recall_utility * SCORE_SCALE)
                          + round(candidate.omission_yield * SCORE_SCALE))
                  for node, candidate in zip(_route_nodes(graph.candidates), graph.candidates))
    incoming: list[list[int]] = [[] for _ in range(graph.sink + 1)]
    for source, target in graph.arcs:
        incoming[target].append(source)
    frontiers: list[dict] = [{} for _ in range(graph.sink + 1)]
    frontiers[graph.source][((0,) * len(entities), frozenset())] = (0, 0, 0, 0, ())
    def priority(record):
        return record[0], -record[1], -record[2], -record[3], tuple(-i for i in reversed(record[4]))
    for node in range(graph.source + 1, graph.sink + 1):
        for previous in incoming[node]:
            for (counts, seen), prior in frontiers[previous].items():
                next_counts = counts
                next_seen = frozenset()
                record = prior
                if node != graph.sink:
                    if seen & node_batches[node - 1]:
                        continue
                    next_seen = frozenset(batch for batch in seen | node_batches[node - 1]
                                          if last_batch_node[batch] > node)
                    next_counts = tuple(a + b for a, b in zip(counts, increments[node - 1]))
                    score, maneuvers, duration, order = _route_cost(nodes, previous, node)
                    score += sum(tables[entity][next_counts[i]] - tables[entity][counts[i]]
                                 for i, entity in enumerate(entities))
                    record = (prior[0] + score, prior[1] + maneuvers, prior[2] + duration,
                              prior[3] + order, prior[4] + (node - 1,))
                key = next_counts, next_seen
                incumbent = frontiers[node].get(key)
                if incumbent is None or priority(record) > priority(incumbent):
                    frontiers[node][key] = record
    if not frontiers[graph.sink]:
        raise ValueError("Mission 1 candidate graph has no route")
    best = max(frontiers[graph.sink].values(), key=priority)
    selected = _fixed_view_runs(allocate_route_information(
        tuple(graph.candidates[i] for i in best[4]), opportunities,
        information_deadline_s=information_deadline_s,
    ))
    return AdvisoryRoute(selected, best[0] / SCORE_SCALE, best[2] / TIME_SCALE,
                         tuple(report for candidate in selected for report in candidate.report_ids))


def longest_path_oracle(graph: CandidateDAG) -> AdvisoryRoute:
    result = _best_route(_route_nodes(graph.candidates), graph.arcs)
    selected = _fixed_view_runs(tuple(graph.candidates[index] for index in result[4]))
    covered = tuple(
        report_id for candidate in selected for report_id in candidate.report_ids
    )
    return AdvisoryRoute(
        selected,
        result[0] / SCORE_SCALE,
        result[2] / TIME_SCALE,
        covered,
    )


class Mission1ReplanGate:
    """Cheap advisory comparison; it never creates planning authority."""

    def __init__(self, relative_improvement_threshold: float = 0.10, *,
                 information_horizon_seconds: float | None = None,
                 route_horizon_seconds: float | None = None) -> None:
        self.relative_improvement_threshold = float(relative_improvement_threshold)
        self.information_horizon_seconds = information_horizon_seconds
        self.route_horizon_seconds = route_horizon_seconds

    def evaluate(
        self,
        current_score: float,
        advisory_score: float,
        *,
        next_assignment_feasible: bool,
        explicit_request: bool = False,
    ) -> ReplanGateDecision:
        current = float(current_score)
        advisory = float(advisory_score)
        relative = (
            math.inf
            if current == 0.0 and advisory > 0.0
            else (0.0 if current == 0.0 else (advisory - current) / current)
        )
        if explicit_request:
            return ReplanGateDecision(
                True, current, advisory, relative, "explicit_replan_request"
            )
        if not next_assignment_feasible:
            return ReplanGateDecision(
                True, current, advisory, relative, "next_assignment_infeasible"
            )
        if current == 0.0 and advisory > 0.0:
            return ReplanGateDecision(
                True, current, advisory, relative, "positive_route_from_zero"
            )
        if relative + 1e-12 >= self.relative_improvement_threshold:
            return ReplanGateDecision(
                True, current, advisory, relative, "score_improvement"
            )
        return ReplanGateDecision(False, current, advisory, relative, "below_threshold")

    def assess(
        self,
        environment: Mapping[str, object],
        belief: ReportingReliabilitySnapshot,
        statechart: Statechart,
        status: FSMStatus,
        *,
        explicit_request: bool = False,
    ) -> tuple[ReplanGateDecision, AdvisoryRoute]:
        graph = build_candidate_dag(
            environment,
            belief,
            information_horizon_seconds=self.information_horizon_seconds,
            route_horizon_seconds=self.route_horizon_seconds,
            positive_only=(
                self.information_horizon_seconds is None
                and self.route_horizon_seconds is None
            ),
        )
        advisory = longest_path_oracle(graph)
        active_context = status.active_state_context
        active_target = active_context.get("target_entity_id")
        lifecycle = environment.get("maneuver_lifecycle")
        world = environment.get("world_model_info")
        acquired_pursuit = (
            active_context.get("surveillance_mode") == "pursue_ship"
            and isinstance(lifecycle, Mapping)
            and lifecycle.get("action") == "pursue"
            and lifecycle.get("lifecycle") == "active"
            and isinstance(lifecycle.get("parameters"), Mapping)
            and lifecycle["parameters"].get("entity_id") == active_target
            and (
                lifecycle.get("phase") == "pursuit"
                or (
                    isinstance(world, Mapping)
                    and active_target in world.get("visible_ship_ids", ())
                )
            )
        )
        # Opportunities are sensing targets, not destinations. Reachability
        # belongs to candidate admission; an acquired pursuit need not repeat
        # its rendezvous just to retain the unchecked tail of its assignment.
        opportunities = {
            item.report_id: item for item in _opportunities(environment, belief)
        }
        candidate_keys = {
            (candidate.mode, candidate.entity_id, candidate.report_ids)
            for candidate in graph.candidates
        }
        by_ship = {ship.entity_id: ship for ship in belief.ships}
        report_rates = public_report_rates(environment, belief)
        now = float(cast(Any, environment["mission_time_seconds"]))
        represented: set[str] = set()
        scored_reports: set[str] = set()
        scored_information_reports: set[str] = set()
        scored_holding: dict = {}
        exposures = holding_exposures(environment, belief)
        current_score = 0.0
        next_feasible = True
        next_start = math.inf
        active_candidate_id = status.active_state_context.get("candidate_id")
        for context in statechart.state_context.values():
            identity = context.get("candidate_id")
            window = context.get("observation_window")
            if (
                not isinstance(identity, str)
                or identity in represented
                or not isinstance(window, Mapping)
            ):
                continue
            start = window.get("start")
            duration = window.get("duration")
            if not isinstance(start, Mapping) or not isinstance(duration, Mapping):
                continue
            start_s = float(start["seconds"])
            end_s = start_s + float(duration["seconds"])
            if end_s < now:
                continue
            represented.add(identity)
            mode = context.get("surveillance_mode")
            entity_id = context.get("target_entity_id")
            raw_report_ids = context.get("target_report_ids")
            if mode not in {"fixed_view", "pursue_ship"} or not isinstance(
                raw_report_ids, (list, tuple)
            ):
                continue
            report_ids = tuple(
                report_id
                for report_id in raw_report_ids
                if isinstance(report_id, str) and report_id in opportunities
            )
            newly_scored = tuple(
                report_id for report_id in report_ids if report_id not in scored_reports
            )
            covered = tuple(opportunities[report_id] for report_id in newly_scored)
            planner_item = context.get("planner_item")
            parameters = (
                planner_item.get("parameters") if isinstance(planner_item, Mapping) else None
            )
            observation_times = None
            if mode == "fixed_view" and "surveillance_views" in environment:
                observation_times = (
                    _fixed_view_observation_times(environment, parameters, covered,
                                           start_s=max(now, start_s), end_s=end_s)
                    if isinstance(parameters, Mapping) else {}
                )
                covered = tuple(item for item in covered if item.report_id in observation_times)
            continuing_pursuit = (
                mode == "pursue_ship"
                and identity == active_candidate_id
                and now < end_s
                and (start_s <= now or acquired_pursuit)
            )
            ship = by_ship.get(entity_id) if isinstance(entity_id, int) else None
            utility = (
                score_fixed_view_opportunities(covered, observation_times=observation_times)
                if mode == "fixed_view" else score_candidate_opportunities(
                    covered,
                    expected_omission_probability=(
                        ship.expected_omission_probability if ship is not None else 0.0
                    ),
                    public_report_rate=(report_rates[ship.entity_id] if ship is not None else 0.0),
                    # Future GPS windows already own their earlier planned
                    # interval, even before acquisition or observation starts.
                    observation_start_s=max(now, start_s),
                )
            )
            if mode == "fixed_view" and isinstance(parameters, Mapping):
                windows = tuple(
                    (w["start"] / w["time_scale"], (w["start"] + w["duration"]) / w["time_scale"])
                    for w in parameters.get("scored_observation_windows", ())
                )
                extra = score_holding_exposure(
                    exposures.get((parameters.get("x"), parameters.get("y"), parameters.get("arrival_direction")), ()),
                    windows, now=now, claimed=scored_holding,
                )
                utility = replace(utility, omission_yield=utility.omission_yield + extra)
            current_score += _score_units(replace(utility, estimation=0.0)
                                          if self.information_horizon_seconds is not None else utility) / SCORE_SCALE
            scored_reports.update(item.report_id for item in covered)
            deadline = (now + self.information_horizon_seconds
                        if self.route_horizon_seconds is not None else None)
            scored_information_reports.update(item.report_id for item in covered if _information_capture_eligible(
                observation_times[item.report_id] if observation_times is not None
                else max(now, start_s, item.time_s), deadline))
            key = (
                str(mode),
                entity_id if isinstance(entity_id, int) else None,
                report_ids,
            )
            # The two-report admission rule applies to new pursuit windows,
            # not the unchecked tail of the currently executing assignment.
            # Maneuver Control owns tracking loss and acquisition recovery.
            continuing_fixed_view = (
                mode == "fixed_view" and identity == active_candidate_id
                and start_s <= now < end_s
            )
            if (
                (report_ids or (mode == "fixed_view" and utility.omission_yield > 0))
                and not continuing_pursuit and not continuing_fixed_view
                and start_s < next_start
            ):
                next_start = start_s
                if mode == "fixed_view":
                    # A different point covering the same IDs is not evidence
                    # that the selected point remains reachable. Conversely,
                    # checks can remove a midpoint's defining reports without
                    # invalidating the still-reachable selected viewpoint.
                    vehicle = cast(Mapping[str, Any], environment["controlled_vehicle"])
                    position = vehicle["position"]
                    next_feasible = (
                        isinstance(parameters, Mapping)
                        and isinstance(parameters.get("x"), (int, float))
                        and isinstance(parameters.get("y"), (int, float))
                        and now + _navigation_time(
                            position["x"], position["y"], parameters["x"], parameters["y"],
                            vehicle["max_velocity"],
                            _vehicle_direction(vehicle), parameters.get("arrival_direction"),
                            float(vehicle.get("quarter_turn_seconds", 0.0)),
                        ) <= start_s + 1e-9
                        and set(report_ids) <= _fixed_view_report_ids(
                            environment, parameters, tuple(opportunities.values()),
                            start_s=max(now, start_s), end_s=end_s,
                        )
                    )
                else:
                    next_feasible = key in candidate_keys
        if self.information_horizon_seconds is not None:
            tables = route_information_tables(tuple(opportunities.values()))
            counts = dict.fromkeys(tables, 0)
            for report in scored_information_reports:
                counts[opportunities[report].entity_id] += 1
            current_score += sum(tables[entity][count] for entity, count in counts.items()) / SCORE_SCALE
        return (
            self.evaluate(
                current_score,
                advisory.score,
                next_assignment_feasible=next_feasible,
                explicit_request=explicit_request,
            ),
            advisory,
        )


def _dzn_strings(values: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def _dzn_ints(values: Sequence[int]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def serialize_minizinc_data(graph: CandidateDAG) -> str:
    """Materialize the shared candidate graph for the checked-in flow model."""

    arcs = tuple((source + 1, target + 1) for source, target in graph.arcs)
    node_count = graph.sink + 1
    outgoing_counts = [0] * node_count
    incoming_counts = [0] * node_count
    for source, target in arcs:
        outgoing_counts[source - 1] += 1
        incoming_counts[target - 1] += 1

    def offsets(counts: Sequence[int]) -> list[int]:
        return [1] + [1 + sum(counts[:index]) for index in range(1, len(counts) + 1)]

    incoming_order = sorted(
        range(len(arcs)), key=lambda index: (arcs[index][1], arcs[index][0])
    )
    report_ids = [
        report_id
        for candidate in graph.candidates
        for report_id in candidate.report_ids
    ]
    report_counts = [len(candidate.report_ids) for candidate in graph.candidates]
    report_offsets = offsets(report_counts)
    durations = [
        round(candidate.duration_s * TIME_SCALE) for candidate in graph.candidates
    ]
    incoming_nodes: list[list[int]] = [[] for _ in range(node_count)]
    for source, target in arcs:
        incoming_nodes[target - 1].append(source - 1)
    nodes = _route_nodes(graph.candidates)
    costs = {(u, v): _route_cost(nodes, u, v) for u, v in graph.arcs}
    path_bounds: list[tuple[int, int, int] | None] = [None] * node_count
    path_bounds[graph.source] = (0, 0, 0)
    for node in range(graph.source + 1, graph.sink + 1):
        options = [
            (
                prior[0] + costs[previous, node][1],
                prior[1] + costs[previous, node][2],
                prior[2] + costs[previous, node][3],
            )
            for previous in incoming_nodes[node]
            if (prior := path_bounds[previous]) is not None
        ]
        if options:
            path_bounds[node] = (
                max(option[0] for option in options),
                max(option[1] for option in options),
                max(option[2] for option in options),
            )
    maximums = path_bounds[graph.sink]
    if maximums is None:
        raise ValueError("Mission 1 candidate graph has no route")
    maneuver_bound = maximums[0] + 1
    duration_bound = maximums[1] + 1
    tie_break_bound = maximums[2] + 1
    # Reweight network edges by node potentials before conversion to floating
    # point. Potentials telescope to a route-independent constant, preserving
    # every lexicographic preference without removing any feasible route.
    potentials = [0] * node_count
    for node in range(graph.source + 1, graph.sink + 1):
        potentials[node] = max(
            (potentials[previous]
             + costs[previous, node][0] * maneuver_bound * duration_bound * tie_break_bound
             - costs[previous, node][1] * duration_bound * tie_break_bound
             - costs[previous, node][2] * tie_break_bound - costs[previous, node][3]
             for previous in incoming_nodes[node]),
            default=0,
        )
    assignments: dict[str, int] = {
        "candidate_count": len(graph.candidates),
        "node_count": node_count,
        "arc_count": len(arcs),
        "source_node": graph.source + 1,
        "sink_node": graph.sink + 1,
        "time_scale": TIME_SCALE,
        "score_scale": SCORE_SCALE,
        "duration_bound": duration_bound,
        "maneuver_bound": maneuver_bound,
        "tie_break_bound": tie_break_bound,
        "report_id_count": len(report_ids),
    }
    lines = [f"{name} = {value};" for name, value in assignments.items()]
    int_arrays = {
        "arc_from": [source for source, _ in arcs],
        "arc_to": [target for _, target in arcs],
        "outgoing_start": offsets(outgoing_counts),
        "incoming_start": offsets(incoming_counts),
        "incoming_edge": [index + 1 for index in incoming_order],
        "node_objective_potential": potentials,
        "candidate_mode": [
            1 if item.mode == "fixed_view" else 2 for item in graph.candidates
        ],
        "candidate_entity_id": [item.entity_id or 0 for item in graph.candidates],
        "candidate_start": [
            round(item.start_s * TIME_SCALE) for item in graph.candidates
        ],
        "candidate_duration": durations,
        "candidate_x": [round(item.x) for item in graph.candidates],
        "candidate_y": [round(item.y) for item in graph.candidates],
        "candidate_observation_delay": [round(item.observation_delay_s * TIME_SCALE) for item in graph.candidates],
        "candidate_arrival_direction": [
            -1 if item.arrival_direction is None else item.arrival_direction
            for item in graph.candidates
        ],
        "candidate_recall": [
            round(item.recall_utility * SCORE_SCALE) for item in graph.candidates
        ],
        "candidate_estimation": [
            round(item.estimation_utility * SCORE_SCALE) for item in graph.candidates
        ],
        "candidate_omission": [
            round(item.omission_yield * SCORE_SCALE) for item in graph.candidates
        ],
        "candidate_target_risk": [
            round(item.target_posterior_risk * SCORE_SCALE) for item in graph.candidates
        ],
        "candidate_omission_probability": [
            round(item.expected_omission_probability * SCORE_SCALE)
            for item in graph.candidates
        ],
        "candidate_public_report_rate": [
            round(item.public_report_rate * SCORE_SCALE) for item in graph.candidates
        ],
        "candidate_report_span": [
            round(item.report_span_s * TIME_SCALE) for item in graph.candidates
        ],
        "candidate_report_start": report_offsets,
    }
    lines.extend(
        f"{name} = {_dzn_ints(values)};" for name, values in int_arrays.items()
    )
    lines.append(
        "candidate_id = "
        f"{_dzn_strings([item.candidate_id for item in graph.candidates])};"
    )
    lines.append(f"candidate_report_id = {_dzn_strings(report_ids)};")
    return "\n".join(lines) + "\n"


__all__ = [
    "AdvisoryRoute",
    "CandidateDAG",
    "CandidateUtility",
    "Mission1ReplanGate",
    "ObservationOpportunity",
    "ReplanGateDecision",
    "SurveillanceCandidate",
    "allocate_route_information",
    "build_candidate_dag",
    "expand_information_states",
    "holding_exposures",
    "information_curve",
    "longest_path_oracle",
    "merge_time_intervals",
    "public_report_rates",
    "route_information_oracle",
    "route_information_tables",
    "sample_fixed_viewpoints",
    "score_candidate_opportunities",
    "score_fixed_view_opportunities",
    "score_holding_exposure",
    "serialize_minizinc_data",
]
