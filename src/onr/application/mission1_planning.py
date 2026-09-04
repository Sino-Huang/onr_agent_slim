"""Code-owned Mission 1 surveillance candidates, DAG, oracle, and gate."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


def _candidate_id(mode: str, report_ids: Sequence[str]) -> str:
    encoded = json.dumps(
        {"mode": mode, "report_ids": list(report_ids)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"candidate-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _travel_time(ax: float, ay: float, bx: float, by: float, speed: float) -> float:
    return math.hypot(bx - ax, by - ay) / speed


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
    environment: Mapping[str, object], belief: ReportingReliabilitySnapshot
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
    vehicle = cast(Mapping[str, object], environment["controlled_vehicle"])
    position = cast(Mapping[str, object], vehicle["position"])
    start_x, start_y = float(cast(Any, position["x"])), float(cast(Any, position["y"]))
    speed = float(cast(Any, vehicle["max_velocity"]))
    raw: list[tuple[str, int, float, float, float, float, float]] = []
    for report in _public_reports(environment, belief):
        if (
            report.report_id in checked
            or report.time_s < now
            or now + _travel_time(start_x, start_y, report.x, report.y, speed)
            > report.time_s + 1e-9
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
) -> CandidateUtility:
    """Score public reports and one pursuit interval's expected hidden yield."""

    ordered = tuple(sorted(covered, key=lambda item: (item.time_s, item.report_id)))
    recall = math.fsum(0.5 * item.recall for item in ordered)
    estimation = math.fsum(item.utility - 0.5 * item.recall for item in ordered)
    report_span = ordered[-1].time_s - ordered[0].time_s if len(ordered) >= 2 else 0.0
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
) -> SurveillanceCandidate:
    ordered = tuple(sorted(covered, key=lambda item: (item.time_s, item.report_id)))
    report_ids = tuple(item.report_id for item in ordered)
    utility = score_candidate_opportunities(
        ordered,
        expected_omission_probability=expected_omission_probability,
        public_report_rate=public_report_rate,
    )
    report_span = ordered[-1].time_s - ordered[0].time_s
    return SurveillanceCandidate(
        candidate_id=_candidate_id(mode, report_ids),
        mode=mode,
        entity_id=entity_id,
        start_s=ordered[0].time_s,
        end_s=ordered[-1].time_s + OBSERVATION_DWELL_SECONDS,
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
    )


def build_candidate_dag(
    environment: Mapping[str, object], belief: ReportingReliabilitySnapshot
) -> CandidateDAG:
    """Build all feasible fixed-view and pursuit candidates and one shared DAG."""

    if belief.belief_kind != "reporting_reliability":
        raise ValueError("Mission 1 planning requires a reporting reliability belief")
    vehicle = environment.get("controlled_vehicle")
    if not isinstance(vehicle, Mapping) or not isinstance(
        vehicle.get("position"), Mapping
    ):
        raise ValueError("Mission 1 planning requires controlled vehicle state")
    position = vehicle["position"]
    speed = float(vehicle["max_velocity"])
    fov = float(vehicle["fov_radius"])
    now = float(cast(Any, environment["mission_time_seconds"]))
    start_x, start_y = float(position["x"]), float(position["y"])
    opportunities = _opportunities(environment, belief)
    report_rates = public_report_rates(environment, belief)
    candidates: dict[tuple[str, tuple[str, ...]], SurveillanceCandidate] = {}

    for anchor in opportunities:
        covered = tuple(
            item
            for item in opportunities
            if abs(item.time_s - anchor.time_s) <= 0.5
            and math.hypot(item.x - anchor.x, item.y - anchor.y) <= fov
        )
        item = _candidate(
            "fixed_view",
            covered,
            x=anchor.x,
            y=anchor.y,
            end_x=anchor.x,
            end_y=anchor.y,
            entity_id=None,
        )
        if (
            now + _travel_time(start_x, start_y, item.x, item.y, speed)
            <= item.start_s + 1e-9
        ):
            candidates[(item.mode, item.report_ids)] = item

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
                now + _travel_time(start_x, start_y, first.x, first.y, speed)
                > first.time_s + 1e-9
            ):
                continue
            for end_index in range(start_index + 1, len(ordered)):
                previous = ordered[end_index - 1]
                following = ordered[end_index]
                if (
                    _travel_time(
                        previous.x, previous.y, following.x, following.y, speed
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
                candidates[(item.mode, item.report_ids)] = item

    ordered_candidates = tuple(
        sorted(
            candidates.values(),
            key=lambda item: (item.start_s, item.end_s, item.mode, item.candidate_id),
        )
    )
    source = 0
    sink = len(ordered_candidates) + 1
    arcs: set[tuple[int, int]] = {(source, sink)}
    for index, candidate in enumerate(ordered_candidates, start=1):
        arcs.add((source, index))
        arcs.add((index, sink))
    for left_index, left in enumerate(ordered_candidates, start=1):
        for right_index, right in enumerate(ordered_candidates, start=1):
            if left_index == right_index or set(left.report_ids) & set(
                right.report_ids
            ):
                continue
            if right.start_s + 1e-9 >= left.end_s + _travel_time(
                left.end_x, left.end_y, right.x, right.y, speed
            ):
                arcs.add((left_index, right_index))
    return CandidateDAG(
        ordered_candidates,
        _prune_dominated_arcs(arcs, ordered_candidates, sink),
        source,
        sink,
    )


def longest_path_oracle(graph: CandidateDAG) -> AdvisoryRoute:
    incoming: list[list[int]] = [[] for _ in range(graph.sink + 1)]
    for source, target in graph.arcs:
        incoming[target].append(source)
    best: list[tuple[int, int, int, int, tuple[int, ...]] | None] = [None] * (
        graph.sink + 1
    )
    best[graph.source] = (0, 0, 0, 0, ())
    for node in range(graph.source + 1, graph.sink + 1):
        for previous in incoming[node]:
            prior = best[previous]
            if prior is None:
                continue
            if node == graph.sink:
                candidate = prior
            else:
                item = graph.candidates[node - 1]
                candidate = (
                    prior[0] + _score_units(_candidate_utility(item)),
                    prior[1] + 1,
                    prior[2] + round(item.duration_s * TIME_SCALE),
                    prior[3] + node,
                    prior[4] + (node - 1,),
                )
            current = best[node]
            candidate_key = (
                candidate[0],
                -candidate[1],
                -candidate[2],
                -candidate[3],
                tuple(-value for value in candidate[4]),
            )
            current_key = (
                None
                if current is None
                else (
                    current[0],
                    -current[1],
                    -current[2],
                    -current[3],
                    tuple(-value for value in current[4]),
                )
            )
            if current_key is None or candidate_key > current_key:
                best[node] = candidate
    result = best[graph.sink]
    if result is None:
        raise ValueError("Mission 1 candidate graph has no route")
    selected = tuple(graph.candidates[index] for index in result[4])
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
        next_key: tuple[str, int | None, tuple[str, ...]] | None = None
        next_start = math.inf
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
            ship = by_ship.get(entity_id) if isinstance(entity_id, int) else None
            utility = score_candidate_opportunities(
                covered,
                expected_omission_probability=(
                    ship.expected_omission_probability
                    if mode == "pursue_ship" and ship is not None
                    else 0.0
                ),
                public_report_rate=(
                    report_rates[ship.entity_id]
                    if mode == "pursue_ship" and ship is not None
                    else 0.0
                ),
            )
            current_score += _score_units(utility) / SCORE_SCALE
            scored_reports.update(newly_scored)
            key = (
                str(mode),
                entity_id if isinstance(entity_id, int) else None,
                report_ids,
            )
            if report_ids and start_s < next_start:
                next_start = start_s
                next_key = key
        next_feasible = next_key is None or next_key in candidate_keys
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
    path_bounds: list[tuple[int, int, int] | None] = [None] * node_count
    path_bounds[graph.source] = (0, 0, 0)
    for node in range(graph.source + 1, graph.sink + 1):
        additions = (
            (0, 0, 0)
            if node == graph.sink
            else (1, durations[node - 1], node)
        )
        options = [
            (
                prior[0] + additions[0],
                prior[1] + additions[1],
                prior[2] + additions[2],
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
    "score_candidate_opportunities",
    "serialize_minizinc_data",
]
