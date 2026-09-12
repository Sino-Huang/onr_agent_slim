"""Code-owned Mission 1 surveillance candidates, DAG, oracle, and gate."""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cache
from itertools import pairwise
from typing import Any, cast

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

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


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
) -> str:
    identity: dict[str, object] = {"mode": mode, "report_ids": list(report_ids)}
    if viewpoint is not None:
        identity["viewpoint"] = viewpoint
    if arrival_direction is not None:
        identity["arrival_direction"] = arrival_direction
    if observation_delay_s:
        identity["observation_delay_s"] = observation_delay_s
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
    views = environment.get("surveillance_views")
    if views is not None:
        by_id = {item.report_id: item for item in opportunities}
        return {
            report_id for view in views
            if all(view[key] == parameters.get(key) for key in ("x", "y", "arrival_direction"))
            for report_id in view["report_ids"]
            if report_id in by_id
            and (start_s is None or by_id[report_id].time_s + view.get("observation_delay_s", 0) >= start_s)
            and (end_s is None or by_id[report_id].time_s + view.get("observation_delay_s", 0) < end_s)
        }
    return {
        item.report_id for item in opportunities
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
    raw: list[tuple[str, int, float, float, float, float, float]] = []
    for report in _public_reports(environment, belief):
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
    return tuple(
        ObservationOpportunity(
            report_id=report_id,
            entity_id=entity_id,
            time_s=time_s,
            x=x,
            y=y,
            recall=recall,
            estimation=estimation,
            utility=0.5 * recall
            + 0.5 * (estimation / max_estimation if max_estimation > 0.0 else 0.0),
        )
        for report_id, entity_id, time_s, x, y, recall, estimation in raw
    )


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
    estimation = math.fsum(item.utility - 0.5 * item.recall for item in ordered)
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


def _score_units(utility: CandidateUtility) -> int:
    return sum(
        round(component * SCORE_SCALE)
        for component in (utility.recall, utility.estimation, utility.omission_yield)
    )


def score_fixed_view_opportunities(
    covered: Sequence[ObservationOpportunity],
) -> CandidateUtility:
    """Round each report-time block once, also when rescoring a sustained view."""
    by_time: dict[float, list[ObservationOpportunity]] = {}
    for item in covered:
        by_time.setdefault(item.time_s, []).append(item)
    utilities = [score_candidate_opportunities(items) for items in by_time.values()]
    return CandidateUtility(
        sum(round(item.recall * SCORE_SCALE) for item in utilities) / SCORE_SCALE,
        sum(round(item.estimation * SCORE_SCALE) for item in utilities) / SCORE_SCALE,
        0.0,
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
) -> SurveillanceCandidate:
    ordered = tuple(sorted(covered, key=lambda item: (item.time_s, item.report_id)))
    report_ids = tuple(item.report_id for item in ordered)
    utility = (
        score_fixed_view_opportunities(ordered)
        if mode == "fixed_view" else score_candidate_opportunities(
            ordered,
            expected_omission_probability=expected_omission_probability,
            public_report_rate=public_report_rate,
        )
    )
    report_span = ordered[-1].time_s - ordered[0].time_s
    return SurveillanceCandidate(
        candidate_id=_candidate_id(
            mode, report_ids,
            viewpoint=(round(x), round(y)) if mode == "fixed_view" else None,
            arrival_direction=arrival_direction,
            observation_delay_s=observation_delay_s,
        ),
        mode=mode,
        entity_id=entity_id,
        start_s=ordered[0].time_s + observation_delay_s,
        end_s=ordered[-1].time_s + observation_delay_s + OBSERVATION_DWELL_SECONDS,
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
    )


def _candidate_arcs(
    candidates: Sequence[SurveillanceCandidate], speed: float,
    quarter_turn_seconds: float = 0.0,
    chronological_reports: bool = False,
) -> tuple[tuple[int, int], ...]:
    """Build the same reduced arcs without materializing the dense closure.

    Generated candidates end with observation dwell; disjoint time windows
    cannot repeat a report. Backward traversal reuses each target's reachable
    successors: a compatible path through a positive-utility intermediate
    dominates a direct arc to the same successor. Zero-utility candidates
    must not erase a shorter equivalent route.
    """
    sink = len(candidates) + 1
    sink_bit = 1 << sink
    all_nodes = (1 << (sink + 1)) - 1
    reachable = [0] * (sink + 1)
    starts = [candidate.start_s + 1e-9 for candidate in candidates]
    reports = [frozenset(candidate.report_ids) for candidate in candidates]
    positive = [False] + [
        _score_units(_candidate_utility(candidate)) > 0 for candidate in candidates
    ] + [False]
    arcs: list[tuple[int, int]] = []
    for source in range(sink - 1, 0, -1):
        left = candidates[source - 1]
        first = bisect_left(starts, left.end_s) + 1
        pending = all_nodes & ~((1 << first) - 1)
        successors = sink_bit
        while pending:
            bit = pending & -pending
            pending ^= bit
            target = bit.bit_length() - 1
            if target == sink:
                arcs.append((source, sink))
                break
            right = candidates[target - 1]
            if chronological_reports and (
                right.start_s - right.observation_delay_s
                <= left.end_s - OBSERVATION_DWELL_SECONDS - left.observation_delay_s + 1e-9
            ):
                # Window alternatives can otherwise repeat an older report
                # after an intervening visit. Ordered report epochs preclude
                # both adjacent and nonadjacent reuse without relaxing flow
                # integrality. One view per co-timed batch remains the scope.
                continue
            if not reports[source - 1].isdisjoint(reports[target - 1]):
                continue
            if right.start_s + 1e-9 < left.end_s + _navigation_time(
                left.end_x, left.end_y, right.x, right.y, speed,
                left.arrival_direction, right.arrival_direction, quarter_turn_seconds,
            ):
                continue
            arcs.append((source, target))
            successors |= bit | reachable[target]
            if positive[target]:
                pending &= ~reachable[target]
        reachable[source] = successors
    # Every candidate has already passed initial-pose/time admission.
    pending = all_nodes & ~1
    while pending:
        bit = pending & -pending
        pending ^= bit
        target = bit.bit_length() - 1
        arcs.append((0, target))
        if positive[target]:
            pending &= ~reachable[target]
    ordered_arcs = tuple(sorted(arcs))
    return (
        _prune_terminal_alternatives(candidates, ordered_arcs)
        if chronological_reports else ordered_arcs
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
                candidate = _candidate(
                    "fixed_view", covered, x=x, y=y, end_x=x, end_y=y,
                    entity_id=None, arrival_direction=direction,
                    observation_delay_s=float(view.get("observation_delay_s", 0.0)),
                )
                candidates[candidate.candidate_id] = candidate
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
    environment: Mapping[str, object], belief: ReportingReliabilitySnapshot
) -> CandidateDAG:
    """Build feasible sampled fixed views and contiguous pursuits in one DAG."""

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
    start_x, start_y = float(position["x"]), float(position["y"])
    opportunities = _opportunities(environment, belief)
    report_rates = public_report_rates(environment, belief)
    candidates: dict[str, SurveillanceCandidate] = {}

    for item in _fixed_view_candidates(
        opportunities, fov, (start_x, start_y),
        cast(Any, environment.get("surveillance_views")),
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

    ordered_candidates = tuple(
        sorted(
            candidates.values(),
            key=lambda item: (item.start_s, item.end_s, item.mode, item.candidate_id),
        )
    )
    source = 0
    sink = len(ordered_candidates) + 1
    return CandidateDAG(
        ordered_candidates,
        _candidate_arcs(ordered_candidates, speed, turn_seconds, observation_window > 0),
        source,
        sink,
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
    nodes: Sequence[_RouteNode], arcs: Sequence[tuple[int, int]],
) -> tuple[int, int, int, int, tuple[int, ...]]:
    """Exact lexicographic path, with fixed-view holding charged on transitions."""
    sink = len(nodes) + 1
    incoming: list[list[int]] = [[] for _ in range(sink + 1)]
    for source, target in arcs:
        incoming[target].append(source)
    best: list[tuple[int, int, int, int, tuple[int, ...]] | None] = [None] * (
        sink + 1
    )
    best[0] = (0, 0, 0, 0, ())
    for node in range(1, sink + 1):
        for previous in incoming[node]:
            prior = best[previous]
            if prior is None:
                continue
            if node == sink:
                candidate = prior
            else:
                score, maneuvers, duration, order = _route_cost(nodes, previous, node)
                candidate = (
                    prior[0] + score,
                    prior[1] + maneuvers,
                    prior[2] + duration,
                    prior[3] + order,
                    prior[4] + (node - 1,),
                )
            current = best[node]
            candidate_key = (
                candidate[0],
                -candidate[1],
                -candidate[2],
                -candidate[3],
                tuple(-value for value in reversed(candidate[4])),
            )
            current_key = (
                None
                if current is None
                else (
                    current[0],
                    -current[1],
                    -current[2],
                    -current[3],
                    tuple(-value for value in reversed(current[4])),
                )
            )
            if current_key is None or candidate_key > current_key:
                best[node] = candidate
    result = best[sink]
    if result is None:
        raise ValueError("Mission 1 candidate graph has no route")
    return result


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
        result.append(replace(
            first, candidate_id=first.candidate_id + "--" + last.candidate_id,
            end_s=last.end_s,
            report_ids=tuple(r for c in group for r in c.report_ids),
            report_span_s=(last.start_s - last.observation_delay_s + last.report_span_s
                           - first.start_s + first.observation_delay_s),
            recall_utility=recall, estimation_utility=estimation,
            combined_score=recall + estimation,
        ))
    return tuple(result)


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

    def __init__(self, relative_improvement_threshold: float = 0.10) -> None:
        self.relative_improvement_threshold = float(relative_improvement_threshold)

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
        graph = build_candidate_dag(environment, belief)
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
            if mode == "fixed_view" and "surveillance_views" in environment:
                visible_ids = (
                    _fixed_view_report_ids(environment, parameters, covered,
                                           start_s=max(now, start_s), end_s=end_s)
                    if isinstance(parameters, Mapping) else set()
                )
                covered = tuple(item for item in covered if item.report_id in visible_ids)
            continuing_pursuit = (
                mode == "pursue_ship"
                and identity == active_candidate_id
                and now < end_s
                and (start_s <= now or acquired_pursuit)
            )
            ship = by_ship.get(entity_id) if isinstance(entity_id, int) else None
            utility = (
                score_fixed_view_opportunities(covered)
                if mode == "fixed_view" else score_candidate_opportunities(
                    covered,
                    expected_omission_probability=(
                        ship.expected_omission_probability if ship is not None else 0.0
                    ),
                    public_report_rate=(report_rates[ship.entity_id] if ship is not None else 0.0),
                    observation_start_s=max(now, start_s) if continuing_pursuit else None,
                )
            )
            current_score += _score_units(utility) / SCORE_SCALE
            scored_reports.update(item.report_id for item in covered)
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
                report_ids and not continuing_pursuit and not continuing_fixed_view
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
    "build_candidate_dag",
    "longest_path_oracle",
    "public_report_rates",
    "sample_fixed_viewpoints",
    "score_candidate_opportunities",
    "score_fixed_view_opportunities",
    "serialize_minizinc_data",
]
