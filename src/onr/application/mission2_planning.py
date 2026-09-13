"""Public collision-observation candidates and advisory replan triggers.

These are planning inputs, not execution authority. Hyper still obtains an
external Planner Plan and an accepted Statechart; Maneuver Control selects actions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass


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


def collision_observation_candidates(environment: Mapping) -> tuple[CollisionObservationCandidate, ...]:
    """Rank feasible refresh opportunities using public positions and travel time."""
    world = environment.get("world_model_info", {})
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
    standoff = min(50.0, float(vehicle["fov_radius"]) / 2)
    candidates = []
    for pair in predictions["active_pairs"]:
        probability = pair["probability"]
        if probability is not None and (not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError("invalid collision probability")
        contact = float(pair["predicted_contact_at_s"])
        deadline = min(end, contact if contact > now else now + 1.0)
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


class Mission2ReplanGate:
    """Coalesce duplicate snapshots and trigger on meaningful risk changes."""

    def __init__(self):
        self._signature = None
        self._run_id = None
        self._alert_ids: set[str] = set()

    def assess(self, environment: Mapping) -> str | None:
        world = environment.get("world_model_info", {})
        if world.get("mission_mode") not in {"mission2", "joint"}:
            return None
        prediction = world.get("perception_predictions")
        if not isinstance(prediction, Mapping):
            raise ValueError("Mission 2 enabled without prediction evidence")
        if prediction.get("schema_version") != 1:
            raise ValueError("unsupported Mission 2 prediction schema")
        now = float(environment["mission_time_seconds"])
        if self._run_id != prediction["run_id"]:
            self._run_id = prediction["run_id"]
            self._signature = None
            self._alert_ids = set()
        state = "stale" if now > prediction["valid_until_s"] else prediction["status"]
        candidates = collision_observation_candidates(environment)
        risks = tuple(sorted((tuple(row["ship_ids"]),
            sum(float(row["predicted_contact_at_s"]) - now > limit for limit in (0, 5, 10, 20)))
            for row in prediction["active_pairs"])) if state in {"ready", "partial"} else ()
        signature = (state, risks, candidates[0].candidate_id if candidates else None,
                     now >= float(world["mission_end_time_s"]))
        fresh_alerts = {row["event_id"] for row in prediction["alerts"]} - self._alert_ids
        self._alert_ids.update(fresh_alerts)
        changed = self._signature is not None and signature != self._signature
        initial_risk = self._signature is None and bool(risks)
        self._signature = signature
        reason = "new_warning" if fresh_alerts else ("risk_changed" if changed or initial_risk else None)
        if reason is None:
            return None
        return f"mission2-gate:{reason}:{prediction['run_id']}:{prediction['sequence']}"


def main(argv=None) -> int:
    """Inspect authorized planner evidence and retain candidate provenance."""
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", type=Path)
    parser.add_argument("--output", type=Path)
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
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "candidates"}))
    else:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
