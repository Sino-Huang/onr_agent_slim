"""Public collision-observation candidates and advisory replan triggers.

These are planning inputs, not execution authority. Hyper still obtains an
external Planner Plan and an accepted Statechart; Maneuver Control selects actions.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from onr.contracts.fsm import FSMStatus


@dataclass(frozen=True)
class CollisionObservationCandidate:
    candidate_id: str
    ship_ids: tuple[int, int]
    target_entity_id: int
    mode: str
    x: float
    y: float
    z: float
    arrival_direction: int
    start_s: float
    end_s: float
    travel_seconds: float
    predicted_contact_at_s: float
    sampled_at_s: float
    probability: float | None
    prediction_source: str
    prediction_run_id: str
    prediction_sequence: int

    def to_dict(self) -> dict:
        return asdict(self)


MISSION2_CONTACT_TIME_DELTA_S = 0.5
MISSION2_CPA_POSITION_DELTA_M = 25.0
MISSION2_PROBABILITY_DELTA = 0.10
MISSION2_ARRIVAL_SLACK_TOLERANCE_S = 0.5
MISSION2_LOCATION_MATCH_TOLERANCE_M = 25.0
MISSION2_ADVISORY_IMPROVEMENT_THRESHOLD = 0.10


@dataclass(frozen=True)
class Mission2PairRisk:
    ship_ids: tuple[int, int]
    predicted_contact_at_s: float
    cpa_x: float | None
    cpa_y: float | None
    probability: float | None


@dataclass(frozen=True)
class Mission2RiskRevision:
    revision: int
    run_id: str
    prediction_sequence: int
    forecast_state: str
    active_pairs: tuple[Mission2PairRisk, ...]
    feasible_candidate_ids: tuple[str, ...]
    fresh_alert_ids: tuple[str, ...]
    material_causes: tuple[str, ...]
    mission_end_reached: bool
    material_changed: bool


@dataclass(frozen=True)
class Mission2GateDecision:
    trigger: bool
    reason: str
    risk_revision: Mission2RiskRevision
    affected_pairs: tuple[tuple[int, int], ...]
    advisory_candidate_ids: tuple[str, ...]
    current_candidate_id: str | None


@dataclass(frozen=True)
class _Mission2RiskBaseline:
    forecast_state: str
    active_pairs: tuple[Mission2PairRisk, ...]
    feasible_candidate_ids: tuple[str, ...]
    mission_end_reached: bool


@dataclass(frozen=True)
class _Mission2Assignment:
    ship_ids: tuple[int, int]
    candidate_id: str
    target_entity_id: int
    mode: str
    prediction_run_id: str
    observation_start_s: float
    observation_end_s: float
    location: tuple[float, float]
    planner_item: Mapping


@dataclass(frozen=True)
class _Mission2LifecycleMatch:
    matched: bool
    completed: bool = False
    active_pursuit: bool = False


def collision_observation_candidates(environment: Mapping) -> tuple[CollisionObservationCandidate, ...]:
    """Rank feasible refresh opportunities using public positions and travel time."""
    world = environment.get("world_model_info", {})
    if world.get("mission_mode") not in {"mission2", "joint"}:
        return ()
    predictions = world.get("perception_predictions")
    if not isinstance(predictions, Mapping):
        return ()
    if predictions.get("schema_version") != 1:
        raise ValueError("unsupported Mission 2 prediction schema")
    now = float(environment["mission_time_seconds"])
    if predictions["status"] not in {"ready", "partial"} or now > predictions["valid_until_s"]:
        return ()
    vehicle = environment["controlled_vehicle"]
    position = vehicle["position"]
    speed = float(vehicle["max_velocity"])
    if speed <= 0:
        return ()
    visible = set(world.get("visible_ship_ids", ()))
    end = min(now + 30.0, float(world["mission_end_time_s"]))
    standoff = min(25.0, float(vehicle["fov_radius"]) / 2)
    candidates = []
    for pair in predictions["active_pairs"]:
        probability = pair["probability"]
        if probability is not None and (not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError("invalid collision probability")
        contact = float(pair["predicted_contact_at_s"])
        # The 10 m entry is urgency evidence, not the 1 m actual-contact truth
        # or an expiry. Monitoring remains useful after that first risk entry.
        deadline = end
        for ship in pair["ship_ids"]:
            trajectory = predictions["trajectories"].get(str(ship))
            if not trajectory or not trajectory["ready"]:
                continue
            selected = None
            for point in trajectory["points"]:
                at = float(point["time_s"])
                if at < now or at + 1 > deadline:
                    continue
                target = point["position"]
                dx, dy = target["x"] - position["x"], target["y"] - position["y"]
                distance = math.hypot(dx, dy)
                travel = max(0.0, distance - standoff) / speed
                if travel <= at - now + 1e-9:
                    fraction = max(0.0, distance - standoff) / distance if distance else 0.0
                    selected = (position["x"] + dx * fraction, position["y"] + dy * fraction, at, travel)
                    break
            if selected is None:
                continue
            x, y, at, travel = selected
            bearing = math.degrees(math.atan2(target["y"] - y, target["x"] - x)) % 360
            direction = (3, 0, 1, 2)[int((bearing + 45) // 90) % 4]
            first, second = sorted(pair["ship_ids"])
            candidates.append(CollisionObservationCandidate(
                f"collision-view:{first}:{second}:{ship}", (first, second), ship,
                "pursue_ship" if ship in visible else "fixed_view", x, y, position["z"], direction,
                at, min(deadline, at + 1.0), travel, contact,
                float(trajectory["sampled_at_s"]), probability, predictions["source"],
                predictions["run_id"], predictions["sequence"]))
    return tuple(sorted(candidates, key=lambda c: (c.predicted_contact_at_s,
        c.sampled_at_s, c.travel_seconds, c.candidate_id)))


def joint_observation_priority(*, configured_priority: str, last_served: str | None,
                               mission1_available: bool, mission2_available: bool) -> str | None:
    """Explicit scheduling preference, with separate mission scores.

    Balanced alternates when both have feasible work. This helper is advisory;
    the Mission Intent and verified plan remain the scheduling authority.
    """
    if configured_priority not in {"balanced", "mission1", "mission2"}:
        raise ValueError("joint priority must be balanced, mission1 or mission2")
    available = [name for name, ready in (("mission1", mission1_available), ("mission2", mission2_available)) if ready]
    if len(available) < 2:
        return available[0] if available else None
    if configured_priority != "balanced":
        return configured_priority
    return "mission1" if last_served == "mission2" else "mission2"


def _candidate_value(candidate, name: str):
    if isinstance(candidate, Mapping):
        return candidate[name]
    return getattr(candidate, name)


def _mission2_candidate_weight(candidate, now: float) -> int:
    contact = float(_candidate_value(candidate, "predicted_contact_at_s"))
    urgency = max(0.0, 30.0 - max(0.0, contact - now)) / 30.0
    probability = _candidate_value(candidate, "probability")
    probability_term = 0.5 if probability is None else float(probability)
    return round(1000 * (urgency + probability_term))


def _mission2_travel_millis(candidate) -> int:
    return round(1000 * float(_candidate_value(candidate, "travel_seconds")))


def mission2_candidate_score(candidate, now: float) -> int:
    weight = _mission2_candidate_weight(candidate, now)
    travel_millis = _mission2_travel_millis(candidate)
    return weight * 100000 - travel_millis


def mission2_advisory_candidate(candidates, now: float):
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda candidate: (
            -mission2_candidate_score(candidate, now),
            float(_candidate_value(candidate, "travel_seconds")),
            str(_candidate_value(candidate, "candidate_id")),
        ),
    )


_MISSION2_MODEL = """int: n;
int: monitor_until_s;
array[1..n] of int: candidate_weight;
array[1..n] of int: travel_millis;
array[1..n] of var bool: selected;
constraint sum(i in 1..n)(bool2int(selected[i])) <= 1;
solve maximize sum(i in 1..n)(bool2int(selected[i]) *
    (candidate_weight[i] * 100000 - travel_millis[i]));
output ["{\\\"selected_indices\\\":[",
    join(",", [show(i) | i in 1..n where fix(selected[i])]),
    "],\\\"monitor_until_s\\\":", show(monitor_until_s), "}"];
"""


def write_minizinc_problem(report: Mapping, model_path, data_path) -> None:
    """Write the verified Mission 2 selection model and current public data."""
    from pathlib import Path

    candidates = report["candidates"]
    now = float(report["mission_time_seconds"])
    monitor_until = min(float(report["valid_until_s"]), float(report["mission_end_time_s"]))
    weights = []
    travel = []
    for candidate in candidates:
        weights.append(_mission2_candidate_weight(candidate, now))
        travel.append(_mission2_travel_millis(candidate))
    model = Path(model_path)
    data = Path(data_path)
    model.parent.mkdir(parents=True, exist_ok=True)
    data.parent.mkdir(parents=True, exist_ok=True)
    model.write_text(_MISSION2_MODEL, encoding="utf-8")
    data.write_text(
        "\n".join(
            (
                f"n = {len(candidates)};",
                f"monitor_until_s = {math.ceil(monitor_until)};",
                f"candidate_weight = {json.dumps(weights)};",
                f"travel_millis = {json.dumps(travel)};",
                "",
            )
        ),
        encoding="utf-8",
    )


def _finite_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _risk_pair(value) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return None
    first, second = sorted(value)
    return first, second


def _pair_risk(row: Mapping) -> Mission2PairRisk:
    ship_ids = _risk_pair(row.get("ship_ids"))
    contact = _finite_number(row.get("predicted_contact_at_s"))
    if ship_ids is None or contact is None:
        raise ValueError("invalid Mission 2 active pair")
    position = row.get("position")
    cpa_x = _finite_number(position.get("x")) if isinstance(position, Mapping) else None
    cpa_y = _finite_number(position.get("y")) if isinstance(position, Mapping) else None
    probability = _finite_number(row.get("probability"))
    return Mission2PairRisk(ship_ids, contact, cpa_x, cpa_y, probability)


def _active_mission2_assignment(status: FSMStatus) -> _Mission2Assignment | None:
    if status.active_state == "collision-monitoring-active":
        return None
    context = status.active_state_context
    pair = _risk_pair(context.get("risk_pair"))
    candidate_id = context.get("candidate_id")
    target = context.get("target_entity_id")
    mode = context.get("surveillance_mode")
    run_id = context.get("prediction_run_id")
    window = context.get("observation_window")
    desired = context.get("desired_outcome")
    planner_item = context.get("planner_item")
    if (
        pair is None
        or not isinstance(candidate_id, str)
        or not candidate_id
        or isinstance(target, bool)
        or not isinstance(target, int)
        or mode not in {"fixed_view", "pursue_ship"}
        or not isinstance(run_id, str)
        or not run_id
        or not isinstance(window, Mapping)
        or not isinstance(desired, Mapping)
        or not isinstance(planner_item, Mapping)
    ):
        return None
    location = desired.get("location")
    if not isinstance(location, Mapping):
        return None
    start = _finite_number(window.get("start_s"))
    end = _finite_number(window.get("end_s"))
    x = _finite_number(location.get("x"))
    y = _finite_number(location.get("y"))
    if None in {start, end, x, y} or end < start:
        return None
    return _Mission2Assignment(
        pair,
        candidate_id,
        target,
        mode,
        run_id,
        start,
        end,
        (x, y),
        planner_item,
    )


def _xy_distance(first: Mapping, second: tuple[float, float]) -> float | None:
    x = _finite_number(first.get("x"))
    y = _finite_number(first.get("y"))
    if x is None or y is None:
        return None
    return math.hypot(x - second[0], y - second[1])


def _matching_lifecycle(
    environment: Mapping, status: FSMStatus, assignment: _Mission2Assignment
) -> _Mission2LifecycleMatch:
    lifecycle = environment.get("maneuver_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return _Mission2LifecycleMatch(False)
    revision = lifecycle.get("plan_revision")
    if (
        isinstance(revision, bool)
        or revision != status.plan_revision
        or lifecycle.get("superseded") is True
        or lifecycle.get("lifecycle") == "superseded"
    ):
        return _Mission2LifecycleMatch(False)
    action = lifecycle.get("action")
    state = lifecycle.get("lifecycle")
    parameters = lifecycle.get("parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    if (
        action == "pursue"
        and state == "active"
        and parameters.get("entity_id") == assignment.target_entity_id
    ):
        return _Mission2LifecycleMatch(True, active_pursuit=True)
    if action == "navigate" and state in {"active", "completed"}:
        destination = _xy_distance(parameters, assignment.location)
        if destination is not None and destination <= MISSION2_LOCATION_MATCH_TOLERANCE_M:
            return _Mission2LifecycleMatch(True, completed=state == "completed")
    vehicle = environment.get("controlled_vehicle")
    position = vehicle.get("position") if isinstance(vehicle, Mapping) else None
    distance = _xy_distance(position, assignment.location) if isinstance(position, Mapping) else None
    if distance is not None and distance <= MISSION2_LOCATION_MATCH_TOLERANCE_M:
        return _Mission2LifecycleMatch(True)
    return _Mission2LifecycleMatch(False)


def _assignment_geometry_covered(
    environment: Mapping,
    status: FSMStatus,
    assignment: _Mission2Assignment,
    risk_revision: Mission2RiskRevision,
    candidates: tuple[CollisionObservationCandidate, ...] | None = None,
) -> str:
    now = float(environment["mission_time_seconds"])
    if now >= assignment.observation_end_s:
        return "window_complete"
    pair_by_id = {risk.ship_ids: risk for risk in risk_revision.active_pairs}
    if assignment.ship_ids not in pair_by_id:
        return "not_covered"
    if assignment.prediction_run_id != risk_revision.run_id:
        return "not_covered"
    available = candidates if candidates is not None else collision_observation_candidates(environment)
    matching = tuple(
        candidate
        for candidate in available
        if candidate.ship_ids == assignment.ship_ids
        and candidate.target_entity_id == assignment.target_entity_id
    )
    if not matching:
        return "not_covered"

    lifecycle = _matching_lifecycle(environment, status, assignment)
    vehicle = environment.get("controlled_vehicle")
    position = vehicle.get("position") if isinstance(vehicle, Mapping) else None
    speed = _finite_number(vehicle.get("max_velocity")) if isinstance(vehicle, Mapping) else None
    distance = _xy_distance(position, assignment.location) if isinstance(position, Mapping) else None
    remaining_time = assignment.observation_end_s - now
    arrival_covered = lifecycle.completed or (
        distance is not None
        and speed is not None
        and speed > 0
        and distance / speed
        <= remaining_time + MISSION2_ARRIVAL_SLACK_TOLERANCE_S
    )
    if not arrival_covered:
        return "not_covered"

    if lifecycle.active_pursuit:
        return "covered"
    for candidate in matching:
        if candidate.candidate_id != assignment.candidate_id:
            continue
        if math.hypot(candidate.x - assignment.location[0], candidate.y - assignment.location[1]) <= (
            MISSION2_LOCATION_MATCH_TOLERANCE_M
        ):
            return "covered"

    world = environment.get("world_model_info")
    prediction = world.get("perception_predictions") if isinstance(world, Mapping) else None
    trajectories = prediction.get("trajectories") if isinstance(prediction, Mapping) else None
    trajectory = trajectories.get(str(assignment.target_entity_id)) if isinstance(trajectories, Mapping) else None
    points = trajectory.get("points", ()) if isinstance(trajectory, Mapping) else ()
    fov = _finite_number(vehicle.get("fov_radius")) if isinstance(vehicle, Mapping) else None
    interval_start = max(now, assignment.observation_start_s)
    if fov is None or fov < 0 or not isinstance(points, (list, tuple)):
        return "not_covered"
    for point in points:
        if not isinstance(point, Mapping):
            continue
        at = _finite_number(point.get("time_s"))
        point_position = point.get("position")
        if at is None or not interval_start <= at <= assignment.observation_end_s:
            continue
        point_distance = (
            _xy_distance(point_position, assignment.location)
            if isinstance(point_position, Mapping)
            else None
        )
        if point_distance is not None and point_distance <= fov:
            return "covered"
    return "not_covered"


def _decision(
    trigger: bool,
    reason: str,
    revision: Mission2RiskRevision,
    *,
    affected_pairs=(),
    advisory=None,
    current_candidate_id: str | None = None,
) -> Mission2GateDecision:
    advisory_ids = () if advisory is None else (advisory.candidate_id,)
    return Mission2GateDecision(
        trigger,
        reason,
        revision,
        tuple(sorted(set(affected_pairs))),
        advisory_ids,
        current_candidate_id,
    )


def assess_mission2_replan(
    environment: Mapping, status: FSMStatus, risk_revision: Mission2RiskRevision
) -> Mission2GateDecision:
    world = environment.get("world_model_info", {})
    assignment = _active_mission2_assignment(status)
    current_id = assignment.candidate_id if assignment is not None else None
    if world.get("mission_mode") not in {"mission2", "joint"}:
        return _decision(False, "not_mission2", risk_revision, current_candidate_id=current_id)
    if risk_revision.mission_end_reached or status.active_state == "scenario-recording-end":
        return _decision(False, "mission_complete", risk_revision, current_candidate_id=current_id)
    if risk_revision.fresh_alert_ids:
        alert_pairs = []
        prediction = world.get("perception_predictions", {})
        for alert in prediction.get("alerts", ()) if isinstance(prediction, Mapping) else ():
            if isinstance(alert, Mapping) and alert.get("event_id") in risk_revision.fresh_alert_ids:
                pair = _risk_pair(alert.get("ship_ids"))
                if pair is not None:
                    alert_pairs.append(pair)
        return _decision(
            True,
            "new_warning",
            risk_revision,
            affected_pairs=alert_pairs,
            current_candidate_id=current_id,
        )
    now = float(environment["mission_time_seconds"])
    if assignment is not None and now >= assignment.observation_end_s:
        return _decision(
            False,
            "observation_window_complete",
            risk_revision,
            affected_pairs=(assignment.ship_ids,),
            current_candidate_id=current_id,
        )
    if risk_revision.forecast_state not in {"ready", "partial"}:
        if assignment is not None:
            return _decision(
                True,
                "forecast_authority_changed",
                risk_revision,
                affected_pairs=(assignment.ship_ids,),
                current_candidate_id=current_id,
            )
        return _decision(False, "no_serviceable_risk", risk_revision)
    active_pairs = {risk.ship_ids for risk in risk_revision.active_pairs}
    if assignment is not None and assignment.ship_ids not in active_pairs:
        return _decision(
            True,
            "planned_risk_cleared",
            risk_revision,
            affected_pairs=(assignment.ship_ids,),
            current_candidate_id=current_id,
        )
    candidates = collision_observation_candidates(environment)
    coverage = None
    if assignment is not None:
        coverage = _assignment_geometry_covered(
            environment, status, assignment, risk_revision, candidates
        )
        if coverage == "window_complete":
            return _decision(
                False,
                "observation_window_complete",
                risk_revision,
                affected_pairs=(assignment.ship_ids,),
                current_candidate_id=current_id,
            )
        if coverage != "covered":
            return _decision(
                True,
                "coverage_lost",
                risk_revision,
                affected_pairs=(assignment.ship_ids,),
                current_candidate_id=current_id,
            )
    advisory = mission2_advisory_candidate(candidates, now)
    if advisory is None:
        return _decision(
            False, "no_serviceable_risk", risk_revision, current_candidate_id=current_id
        )
    if assignment is None:
        return _decision(
            True,
            "new_serviceable_risk",
            risk_revision,
            affected_pairs=(advisory.ship_ids,),
            advisory=advisory,
        )
    if assignment.ship_ids == advisory.ship_ids:
        return _decision(
            False,
            "current_plan_covered",
            risk_revision,
            affected_pairs=(assignment.ship_ids,),
            advisory=advisory,
            current_candidate_id=current_id,
        )
    current_candidates = tuple(
        candidate for candidate in candidates if candidate.ship_ids == assignment.ship_ids
    )
    current = mission2_advisory_candidate(current_candidates, now)
    current_score = mission2_candidate_score(current, now) if current is not None else 0
    advisory_score = mission2_candidate_score(advisory, now)
    improvement = (
        current_score <= 0 < advisory_score
        or (
            current_score > 0
            and (advisory_score - current_score) / current_score
            >= MISSION2_ADVISORY_IMPROVEMENT_THRESHOLD
        )
    )
    if improvement:
        return _decision(
            True,
            "higher_priority_risk",
            risk_revision,
            affected_pairs=(assignment.ship_ids, advisory.ship_ids),
            advisory=advisory,
            current_candidate_id=current_id,
        )
    return _decision(
        False,
        "current_plan_preferred",
        risk_revision,
        affected_pairs=(assignment.ship_ids, advisory.ship_ids),
        advisory=advisory,
        current_candidate_id=current_id,
    )


class Mission2ReplanGate:
    """Retain the last material public-risk baseline and issue advisory decisions."""

    def __init__(self):
        self._run_id: str | None = None
        self._revision = 0
        self._baseline: _Mission2RiskBaseline | None = None
        self._alert_ids: set[str] = set()

    def _observe_risk(self, environment: Mapping) -> Mission2RiskRevision:
        world = environment["world_model_info"]
        prediction = world["perception_predictions"]
        now = float(environment["mission_time_seconds"])
        run_id = str(prediction["run_id"])
        causes = []
        if self._run_id != run_id:
            self._run_id = run_id
            self._revision = 0
            self._baseline = None
            self._alert_ids.clear()
            causes.append("prediction_run_changed")
        state = "stale" if now > float(prediction["valid_until_s"]) else str(prediction["status"])
        risks = tuple(
            sorted(
                (_pair_risk(row) for row in prediction["active_pairs"]),
                key=lambda risk: risk.ship_ids,
            )
        )
        candidates = collision_observation_candidates(environment)
        candidate_ids = tuple(sorted(candidate.candidate_id for candidate in candidates))
        current_alert_ids = {
            row["event_id"]
            for row in prediction["alerts"]
            if isinstance(row, Mapping) and isinstance(row.get("event_id"), str)
        }
        fresh_alert_ids = tuple(sorted(current_alert_ids - self._alert_ids))
        self._alert_ids.update(current_alert_ids)
        mission_end = now >= float(world["mission_end_time_s"])
        baseline = self._baseline
        if baseline is not None:
            if state != baseline.forecast_state:
                causes.append("forecast_state_changed")
            previous = {risk.ship_ids: risk for risk in baseline.active_pairs}
            current = {risk.ship_ids: risk for risk in risks}
            if current.keys() - previous.keys():
                causes.append("pair_added")
            if previous.keys() - current.keys():
                causes.append("pair_removed")
            if fresh_alert_ids:
                causes.append("fresh_alert")
            contact_changed = False
            cpa_changed = False
            probability_changed = False
            for pair in previous.keys() & current.keys():
                old = previous[pair]
                new = current[pair]
                contact_delta = abs(
                    new.predicted_contact_at_s - old.predicted_contact_at_s
                )
                if contact_delta > MISSION2_CONTACT_TIME_DELTA_S or math.isclose(
                    contact_delta, MISSION2_CONTACT_TIME_DELTA_S
                ):
                    contact_changed = True
                old_cpa = old.cpa_x is not None and old.cpa_y is not None
                new_cpa = new.cpa_x is not None and new.cpa_y is not None
                if old_cpa != new_cpa or (
                    old_cpa
                    and new_cpa
                    and (
                        math.hypot(new.cpa_x - old.cpa_x, new.cpa_y - old.cpa_y)
                        > MISSION2_CPA_POSITION_DELTA_M
                        or math.isclose(
                            math.hypot(
                                new.cpa_x - old.cpa_x, new.cpa_y - old.cpa_y
                            ),
                            MISSION2_CPA_POSITION_DELTA_M,
                        )
                    )
                ):
                    cpa_changed = True
                if (old.probability is None) != (new.probability is None) or (
                    old.probability is not None
                    and new.probability is not None
                    and (
                        abs(new.probability - old.probability)
                        > MISSION2_PROBABILITY_DELTA
                        or math.isclose(
                            abs(new.probability - old.probability),
                            MISSION2_PROBABILITY_DELTA,
                        )
                    )
                ):
                    probability_changed = True
            if contact_changed:
                causes.append("contact_time_moved")
            if cpa_changed:
                causes.append("cpa_moved")
            if probability_changed:
                causes.append("probability_changed")
            if candidate_ids != baseline.feasible_candidate_ids:
                causes.append("candidate_feasibility_changed")
            if not baseline.mission_end_reached and mission_end:
                causes.append("mission_end_reached")
        elif fresh_alert_ids:
            causes.append("fresh_alert")
        material_changed = bool(causes)
        if material_changed:
            self._revision += 1
            self._baseline = _Mission2RiskBaseline(
                state, risks, candidate_ids, mission_end
            )
        return Mission2RiskRevision(
            self._revision,
            run_id,
            int(prediction["sequence"]),
            state,
            risks,
            candidate_ids,
            fresh_alert_ids,
            tuple(causes),
            mission_end,
            material_changed,
        )

    def assess(self, environment: Mapping, status: FSMStatus) -> Mission2GateDecision:
        world = environment.get("world_model_info", {})
        if world.get("mission_mode") not in {"mission2", "joint"}:
            revision = Mission2RiskRevision(
                self._revision,
                self._run_id or "",
                0,
                "unavailable",
                (),
                (),
                (),
                (),
                False,
                False,
            )
            return assess_mission2_replan(environment, status, revision)
        prediction = world.get("perception_predictions")
        if not isinstance(prediction, Mapping):
            raise ValueError("Mission 2 enabled without prediction evidence")
        if prediction.get("schema_version") != 1:
            raise ValueError("unsupported Mission 2 prediction schema")
        revision = self._observe_risk(environment)
        return assess_mission2_replan(environment, status, revision)


def mission2_trigger_identity(decision: Mission2GateDecision) -> str:
    revision = decision.risk_revision
    return (
        f"mission2-gate:{decision.reason}:{revision.run_id}:"
        f"{revision.prediction_sequence}"
    )


def main(argv=None) -> int:
    """Inspect authorized planner evidence and retain candidate provenance."""
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--joint-priority", choices=("balanced", "mission1", "mission2"), default="balanced")
    args = parser.parse_args(argv)
    environment = json.loads(args.environment.read_text())
    world = environment.get("world_model_info", {})
    prediction = world.get("perception_predictions")
    if not prediction:
        parser.error("environment has no Mission 2 prediction evidence")
    candidates = collision_observation_candidates(environment)
    report = {"mission_time_seconds": environment["mission_time_seconds"],
        "mission_end_time_s": world["mission_end_time_s"], "mission_mode": world["mission_mode"],
        "prediction_run_id": prediction["run_id"], "prediction_sequence": prediction["sequence"],
        "prediction_source": prediction["source"], "prediction_status": prediction["status"],
        "valid_until_s": prediction["valid_until_s"], "joint_priority": args.joint_priority,
        "candidate_count": len(candidates), "candidates": [c.to_dict() for c in candidates]}
    if bool(args.model) != bool(args.data):
        parser.error("--model and --data must be supplied together")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "candidates"}))
    else:
        print(json.dumps(report, indent=2))
    if args.model is not None:
        write_minizinc_problem(report, args.model, args.data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
