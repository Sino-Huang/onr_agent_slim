"""Evidence-driven, bounded Mission 3 fleet inspection policy."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

EntityId = int | str

_SELECTION_MODEL = """int: selected_index;
constraint selected_index >= 0 /\\ selected_index <= 1;
solve maximize selected_index;
output ["{\\\"selected_index\\\":", show(selected_index), "}"];
"""


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Mission3Description:
    mission_id: str
    mission_text: str
    source_authority: str
    target_ids: tuple[EntityId, ...] | None = None
    area: Mapping[str, float] | None = None
    mission_time_budget_s: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Mission3Description:
        required = {
            "schema_version",
            "mission_id",
            "mission_text",
            "source_authority",
        }
        optional = {"target_ids", "area", "mission_time_budget_s"}
        if not required <= set(value) or not set(value) <= required | optional:
            raise ValueError("Mission 3 description fields are invalid")
        if value["schema_version"] != 1:
            raise ValueError("Mission 3 description schema is unsupported")
        for name in ("mission_id", "mission_text", "source_authority"):
            if not isinstance(value[name], str) or not cast(str, value[name]).strip():
                raise ValueError(f"Mission 3 {name} must be non-empty text")
        raw_ids = value.get("target_ids")
        if raw_ids is not None and (
            not isinstance(raw_ids, Sequence)
            or isinstance(raw_ids, (str, bytes, bytearray))
        ):
            raise TypeError("Mission 3 target_ids must be an array")
        target_ids = None
        if raw_ids is not None:
            normalized = []
            for item in raw_ids:
                if isinstance(item, bool) or not (
                    (isinstance(item, int) and item > 0)
                    or (isinstance(item, str) and bool(item.strip()))
                ):
                    raise ValueError("Mission 3 target ID is invalid")
                if item not in normalized:
                    normalized.append(item)
            target_ids = tuple(cast(list[EntityId], normalized))
        raw_area = value.get("area")
        area = None
        if raw_area is not None:
            if not isinstance(raw_area, Mapping):
                raise TypeError("Mission 3 area must be an object")
            names = {"north_min_m", "north_max_m", "east_min_m", "east_max_m"}
            if set(raw_area) != names:
                raise ValueError("Mission 3 area fields are invalid")
            area = {name: float(raw_area[name]) for name in names}
            if not all(math.isfinite(item) for item in area.values()):
                raise ValueError("Mission 3 area bounds must be finite")
            if (
                area["north_min_m"] > area["north_max_m"]
                or area["east_min_m"] > area["east_max_m"]
            ):
                raise ValueError("Mission 3 area bounds are reversed")
        budget = value.get("mission_time_budget_s")
        if budget is not None and (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not math.isfinite(float(budget))
            or float(budget) <= 0.0
        ):
            raise ValueError("Mission 3 time budget must be positive and finite")
        return cls(
            mission_id=cast(str, value["mission_id"]),
            mission_text=cast(str, value["mission_text"]),
            source_authority=cast(str, value["source_authority"]),
            target_ids=target_ids,
            area=area,
            mission_time_budget_s=None if budget is None else float(budget),
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> Mission3Description:
        value = json.loads(payload)
        if not isinstance(value, Mapping):
            raise TypeError("Mission 3 description must be a JSON object")
        return cls.from_mapping(value)

    def runtime_selection(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.target_ids is not None:
            result["target_ids"] = list(self.target_ids)
        if self.area is not None:
            result["area"] = dict(self.area)
        return result


@dataclass(frozen=True, slots=True)
class Mission3Decision:
    action: Literal["navigate", "pursue", "investigate", "report"]
    entity_id: EntityId | None
    parameters: Mapping[str, object]
    reason: str
    supporting_evidence_ids: tuple[str, ...] = ()
    report: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _mission3(environment: Mapping[str, object]) -> Mapping[str, object] | None:
    world = environment.get("world_model_info")
    if not isinstance(world, Mapping) or world.get("mission_mode") != "mission3":
        return None
    inspection = world.get("mission3")
    if not isinstance(inspection, Mapping) or inspection.get("schema_version") != 1:
        raise ValueError("Mission 3 enabled without supported inspection evidence")
    return inspection


class Mission3AdaptivePlanner:
    """Nearest-feasible screening with evidence-priority investigation/revisits."""

    def __init__(
        self,
        *,
        mission_time_budget_s: float | None = None,
        maximum_investigations_per_ship: int = 2,
        movement_replan_distance_m: float = 10.0,
    ) -> None:
        self.mission_time_budget_s = mission_time_budget_s
        self.maximum_investigations_per_ship = maximum_investigations_per_ship
        self.movement_replan_distance_m = movement_replan_distance_m
        self._last_signature: str | None = None
        self._handled_terminal_commands: set[str] = set()
        self._investigation_attempts: dict[EntityId, int] = {}
        self._failed_at_evidence: dict[EntityId, tuple[int, float | None]] = {}
        self._last_planned_positions: dict[EntityId, tuple[float, float, float]] = {}
        self.decisions: list[Mission3Decision] = []

    def decide(self, environment: Mapping[str, object]) -> Mission3Decision | None:
        inspection = _mission3(environment)
        if inspection is None:
            return None
        signature = self._signature(environment, inspection)
        if signature == self._last_signature:
            return None
        self._last_signature = signature
        ships = self._ships(inspection)
        selected = tuple(inspection.get("selected_ship_ids", ()))
        if set(ships) != set(selected):
            raise ValueError("Mission 3 ship ledger does not match selected roster")
        lifecycle = environment.get("maneuver_lifecycle")
        active_target = self._update_terminal_attempts(lifecycle, ships, inspection)
        now = float(environment["mission_time_seconds"])
        world = cast(Mapping[str, object], environment["world_model_info"])
        recording_end = float(world["mission_end_time_s"])
        budget_end = (
            recording_end
            if self.mission_time_budget_s is None
            else min(recording_end, self.mission_time_budget_s)
        )
        if not selected:
            return self._record(self._report_decision(inspection, "empty_selection"))
        if now >= budget_end:
            return self._record(self._report_decision(inspection, "mission_budget"))

        unresolved = [
            ship_id
            for ship_id in selected
            if ships[ship_id]["resolution"]["status"] != "resolved"
        ]
        if not unresolved:
            if self._lifecycle_active(lifecycle):
                return self._record(
                    self._cancel_with_hold(environment, "early_verdict_cancel")
                )
            return self._record(self._report_decision(inspection, "all_resolved"))

        maximum_age = inspection.get("target_observation_max_age_s")
        observations = {
            item["ship_id"]: item
            for item in inspection.get("target_observations", ())
            if isinstance(item, Mapping)
            and item.get("ship_id") in selected
            and (
                maximum_age is None
                or float(item.get("age_s", math.inf)) <= float(maximum_age)
            )
        }
        if self._lifecycle_active(lifecycle):
            assert active_target is not None
            active_ship = ships.get(active_target)
            if active_ship is None:
                raise ValueError("active Mission 3 maneuver targets an unselected ship")
            if active_ship["resolution"]["status"] == "resolved":
                return self._record(
                    self._next_action(
                        environment,
                        inspection,
                        ships,
                        unresolved,
                        observations,
                        exclude={active_target},
                        reason_prefix="early_verdict",
                    )
                    or self._cancel_with_hold(environment, "early_verdict_cancel")
                )
            lifecycle_action = cast(str, lifecycle["action"])
            if lifecycle_action in {"navigate", "pursue"} and self._needs_investigation(
                active_ship
            ):
                return self._record(
                    self._investigate(
                        active_target, active_ship, "new_inconclusive_evidence"
                    )
                )
            observation = observations.get(active_target)
            if observation is not None and self._target_moved(
                active_target, observation
            ):
                return self._record(
                    self._screen_action(
                        environment,
                        active_target,
                        observation,
                        reason="target_moved",
                    )
                )
            return None

        decision = self._next_action(
            environment,
            inspection,
            ships,
            unresolved,
            observations,
            exclude=set(),
            reason_prefix="adaptive_tour",
        )
        if decision is None:
            return self._record(
                self._report_decision(inspection, "no_feasible_targets")
            )
        return self._record(decision)

    def _next_action(
        self,
        environment: Mapping[str, object],
        inspection: Mapping[str, object],
        ships: Mapping[EntityId, Mapping[str, object]],
        unresolved: list[EntityId],
        observations: Mapping[EntityId, Mapping[str, object]],
        *,
        exclude: set[EntityId],
        reason_prefix: str,
    ) -> Mission3Decision | None:
        deeper = [
            ship_id
            for ship_id in unresolved
            if ship_id not in exclude
            and self._needs_investigation(ships[ship_id])
            and self._investigation_attempts.get(ship_id, 0) == 0
            and self._failure_recovered(
                ship_id, ships[ship_id], observations.get(ship_id)
            )
        ]
        if deeper:
            target = self._nearest(environment, deeper, observations)
            if target is not None:
                return self._investigate(
                    target,
                    ships[target],
                    f"{reason_prefix}:investigate_public_evidence",
                )
        screening = [
            ship_id
            for ship_id in unresolved
            if ship_id not in exclude
            and not self._needs_investigation(ships[ship_id])
            and self._failure_recovered(
                ship_id, ships[ship_id], observations.get(ship_id)
            )
        ]
        target = self._nearest(environment, screening, observations)
        if target is not None:
            return self._screen_action(
                environment,
                target,
                observations[target],
                reason=f"{reason_prefix}:nearest_screening_target",
            )
        revisits = [
            ship_id
            for ship_id in unresolved
            if ship_id not in exclude
            and self._investigation_attempts.get(ship_id, 0)
            < self.maximum_investigations_per_ship
            and self._failure_recovered(
                ship_id, ships[ship_id], observations.get(ship_id)
            )
        ]
        target = self._nearest(environment, revisits, observations)
        if target is not None:
            return self._investigate(
                target, ships[target], f"{reason_prefix}:bounded_revisit"
            )
        _ = inspection
        return None

    @staticmethod
    def _ships(
        inspection: Mapping[str, object],
    ) -> dict[EntityId, Mapping[str, object]]:
        raw = inspection.get("ships")
        if not isinstance(raw, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in raw
        ):
            raise TypeError("Mission 3 ship ledger must be an array of objects")
        return {cast(EntityId, item["ship_id"]): item for item in raw}

    @staticmethod
    def _lifecycle_active(lifecycle: object) -> bool:
        return isinstance(lifecycle, Mapping) and lifecycle.get("lifecycle") in {
            "accepted",
            "active",
        }

    def _update_terminal_attempts(
        self,
        lifecycle: object,
        ships: Mapping[EntityId, Mapping[str, object]],
        inspection: Mapping[str, object],
    ) -> EntityId | None:
        if not isinstance(lifecycle, Mapping):
            return None
        parameters = lifecycle.get("parameters")
        target = (
            parameters.get("entity_id") if isinstance(parameters, Mapping) else None
        )
        if target not in ships:
            return None
        command_id = lifecycle.get("command_id")
        state = lifecycle.get("lifecycle")
        if (
            isinstance(command_id, str)
            and command_id not in self._handled_terminal_commands
            and state in {"completed", "failed", "cancelled"}
        ):
            self._handled_terminal_commands.add(command_id)
            if lifecycle.get("action") == "investigate":
                self._investigation_attempts[cast(EntityId, target)] = (
                    self._investigation_attempts.get(cast(EntityId, target), 0) + 1
                )
            if state == "failed":
                ship = ships[cast(EntityId, target)]
                observation = next(
                    (
                        item
                        for item in inspection.get("target_observations", ())
                        if isinstance(item, Mapping) and item.get("ship_id") == target
                    ),
                    None,
                )
                self._failed_at_evidence[cast(EntityId, target)] = (
                    len(cast(Sequence[object], ship.get("evidence_ids", ()))),
                    None if observation is None else float(observation["sampled_at_s"]),
                )
        return cast(EntityId, target)

    @staticmethod
    def _needs_investigation(ship: Mapping[str, object]) -> bool:
        screening = cast(Mapping[str, object], ship["screening"])
        investigation = cast(Mapping[str, object], ship["investigation"])
        return screening.get("status") in {"inconclusive", "suspected"} or (
            investigation.get("status") in {"inconclusive", "suspected"}
        )

    def _failure_recovered(
        self,
        ship_id: EntityId,
        ship: Mapping[str, object],
        observation: Mapping[str, object] | None,
    ) -> bool:
        failed = self._failed_at_evidence.get(ship_id)
        if failed is None:
            return True
        evidence_count, sampled_at = failed
        current_count = len(cast(Sequence[object], ship.get("evidence_ids", ())))
        current_sample = (
            None if observation is None else float(observation["sampled_at_s"])
        )
        recovered = current_count > evidence_count or (
            current_sample is not None
            and (sampled_at is None or current_sample > sampled_at)
        )
        if recovered:
            del self._failed_at_evidence[ship_id]
        return recovered

    def _nearest(
        self,
        environment: Mapping[str, object],
        candidates: list[EntityId],
        observations: Mapping[EntityId, Mapping[str, object]],
    ) -> EntityId | None:
        vehicle = cast(Mapping[str, object], environment["controlled_vehicle"])
        position = cast(Mapping[str, object], vehicle["position"])
        ranked = []
        for ship_id in candidates:
            observation = observations.get(ship_id)
            if observation is None:
                continue
            target = cast(Mapping[str, object], observation["estimated_position"])
            distance = math.hypot(
                float(target["x"]) - float(position["x"]),
                float(target["y"]) - float(position["y"]),
            )
            ranked.append((distance, str(ship_id), ship_id))
        return None if not ranked else min(ranked)[2]

    def _screen_action(
        self,
        environment: Mapping[str, object],
        ship_id: EntityId,
        observation: Mapping[str, object],
        *,
        reason: str,
    ) -> Mission3Decision:
        vehicle = cast(Mapping[str, object], environment["controlled_vehicle"])
        position = cast(Mapping[str, object], vehicle["position"])
        target = cast(Mapping[str, object], observation["estimated_position"])
        dx = float(target["x"]) - float(position["x"])
        dy = float(target["y"]) - float(position["y"])
        distance = math.hypot(dx, dy)
        fov = float(vehicle["fov_radius"])
        standoff = min(25.0, fov / 2.0)
        source = str(observation["source"])
        estimated = (float(target["x"]), float(target["y"]), float(target["z"]))
        self._last_planned_positions[ship_id] = estimated
        if source.endswith("camera") and distance <= fov:
            return Mission3Decision(
                "pursue",
                ship_id,
                {"entity_id": ship_id, "standoff_distance": standoff},
                reason + ":current_camera_sighting",
            )
        fraction = max(0.0, distance - standoff) / distance if distance else 0.0
        x = float(position["x"]) + dx * fraction
        y = float(position["y"]) + dy * fraction
        bearing = math.degrees(math.atan2(dy, dx)) % 360.0
        arrival_direction = (3, 0, 1, 2)[int((bearing + 45.0) // 90.0) % 4]
        return Mission3Decision(
            "navigate",
            ship_id,
            {
                "x": x,
                "y": y,
                "z": float(position["z"]),
                "arrival_direction": arrival_direction,
            },
            reason + ":public_position_approach",
        )

    def _investigate(
        self, ship_id: EntityId, ship: Mapping[str, object], reason: str
    ) -> Mission3Decision:
        return Mission3Decision(
            "investigate",
            ship_id,
            {"entity_id": ship_id, "standoff_distance": 3.0, "speed": 10.0},
            reason,
            tuple(cast(Sequence[str], ship.get("evidence_ids", ()))),
        )

    def _target_moved(
        self, ship_id: EntityId, observation: Mapping[str, object]
    ) -> bool:
        previous = self._last_planned_positions.get(ship_id)
        target = cast(Mapping[str, object], observation["estimated_position"])
        current = (float(target["x"]), float(target["y"]), float(target["z"]))
        return (
            previous is not None
            and math.dist(previous, current) >= self.movement_replan_distance_m
        )

    @staticmethod
    def _cancel_with_hold(
        environment: Mapping[str, object], reason: str
    ) -> Mission3Decision:
        position = cast(
            Mapping[str, object],
            cast(Mapping[str, object], environment["controlled_vehicle"])["position"],
        )
        return Mission3Decision(
            "navigate",
            None,
            {"x": position["x"], "y": position["y"], "z": position["z"]},
            reason,
        )

    @staticmethod
    def final_report(
        inspection: Mapping[str, object], reason: str
    ) -> dict[str, object]:
        ships = Mission3AdaptivePlanner._ships(inspection)
        selected = tuple(inspection.get("selected_ship_ids", ()))
        results = []
        for ship_id in selected:
            ship = ships[cast(EntityId, ship_id)]
            verdict = ship.get("verdict")
            resolution = cast(Mapping[str, object], ship["resolution"])
            results.append(
                {
                    "ship_id": ship_id,
                    "verdict": None if verdict is None else verdict["value"],
                    "supporting_evidence_ids": (
                        []
                        if verdict is None
                        else list(verdict["supporting_evidence_ids"])
                    ),
                    "resolution": resolution["status"],
                    "incomplete_reason": resolution.get("incomplete_reason")
                    or (None if resolution["status"] == "resolved" else reason),
                }
            )
        resolved = sum(item["resolution"] == "resolved" for item in results)
        return {
            "schema_version": 1,
            "selected_ship_ids": list(selected),
            "inspection_complete": bool(selected) and resolved == len(selected),
            "resolved_ship_count": resolved,
            "unresolved_ship_count": len(selected) - resolved,
            "termination_reason": reason,
            "ships": results,
        }

    def _report_decision(
        self, inspection: Mapping[str, object], reason: str
    ) -> Mission3Decision:
        return Mission3Decision(
            "report",
            None,
            {},
            reason,
            report=self.final_report(inspection, reason),
        )

    def _record(self, decision: Mission3Decision) -> Mission3Decision:
        self.decisions.append(decision)
        return decision

    @staticmethod
    def _signature(
        environment: Mapping[str, object], inspection: Mapping[str, object]
    ) -> str:
        lifecycle = environment.get("maneuver_lifecycle")
        selected = {
            "state_version": environment.get("state_version"),
            "mission_time_seconds": environment.get("mission_time_seconds"),
            "lifecycle": lifecycle,
            "ships": inspection.get("ships"),
            "target_observations": inspection.get("target_observations"),
        }
        return json.dumps(_plain(selected), sort_keys=True, separators=(",", ":"))


class Mission3ReplanGate:
    """Wake Hyper only for a new bounded Mission 3 policy decision."""

    def __init__(self) -> None:
        self.planner = Mission3AdaptivePlanner()
        self.last_decision: Mission3Decision | None = None
        self._last_trigger: str | None = None

    def assess(self, environment: Mapping[str, object]) -> str | None:
        decision = self.planner.decide(environment)
        if decision is None:
            return None
        self.last_decision = decision
        target = "none" if decision.entity_id is None else str(decision.entity_id)
        trigger = f"mission3-gate:{decision.reason}:{decision.action}:{target}"
        if trigger == self._last_trigger:
            return None
        self._last_trigger = trigger
        return trigger


def write_minizinc_problem(
    decision: Mission3Decision | None, model_path: Path, data_path: Path
) -> None:
    """Write the code-owned MiniZinc receipt for one adaptive decision."""
    model_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(_SELECTION_MODEL, encoding="utf-8")
    data_path.write_text(
        f"selected_index = {0 if decision is None else 1};\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mission-time-budget-s", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", type=Path)
    parser.add_argument("--data", type=Path)
    args = parser.parse_args(argv)
    environment = json.loads(args.environment.read_text(encoding="utf-8"))
    planner = Mission3AdaptivePlanner(mission_time_budget_s=args.mission_time_budget_s)
    decision = planner.decide(environment)
    result = {
        "source": "public_world_model",
        "dry_run": args.dry_run,
        "mission_time_seconds": environment.get("mission_time_seconds"),
        "mission_end_time_s": environment.get("world_model_info", {}).get(
            "mission_end_time_s"
        ),
        "decision": None if decision is None else decision.to_dict(),
    }
    if args.output is not None and not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if bool(args.model) != bool(args.data):
        parser.error("--model and --data must be supplied together")
    if args.model is not None and not args.dry_run:
        write_minizinc_problem(decision, args.model, args.data)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Mission3AdaptivePlanner",
    "Mission3Decision",
    "Mission3Description",
    "Mission3ReplanGate",
    "write_minizinc_problem",
]
