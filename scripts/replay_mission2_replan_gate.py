#!/usr/bin/env python
"""Replay the Mission 2 replan gate from recorded, public run artifacts."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onr.application.mission2_planning import (
    MISSION2_ADVISORY_IMPROVEMENT_THRESHOLD,
    MISSION2_CONTACT_TIME_DELTA_S,
    MISSION2_CPA_POSITION_DELTA_M,
    MISSION2_PROBABILITY_DELTA,
    Mission2ReplanGate,
)
from onr.contracts.fsm import FSMStatus


_TRIGGER_RE = re.compile(r"^mission2-gate:(?P<reason>[^:]+):(?P<run_id>.+):(?P<sequence>\d+)$")


@dataclass(frozen=True)
class Incident:
    outcome_event_id: str
    trigger_identity: str
    plan_revision: int
    run_id: str
    prediction_sequence: int
    alert_ids: tuple[str, ...]
    added_pairs: tuple[tuple[int, int], ...]
    removed_planned_pair: tuple[int, int] | None
    materially_moved_pairs: tuple[tuple[int, int], ...]
    matching_pairs: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_event_id": self.outcome_event_id,
            "trigger_identity": self.trigger_identity,
            "plan_revision": self.plan_revision,
            "run_id": self.run_id,
            "prediction_sequence": self.prediction_sequence,
            "alert_ids": list(self.alert_ids),
            "added_pairs": [list(pair) for pair in self.added_pairs],
            "removed_planned_pair": (
                list(self.removed_planned_pair)
                if self.removed_planned_pair is not None
                else None
            ),
            "materially_moved_pairs": [
                list(pair) for pair in self.materially_moved_pairs
            ],
        }


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact is not an object: {path}")
    return value


def _load_events(root: Path, relative_pattern: str) -> list[dict[str, Any]]:
    return [_load_json(path) for path in sorted(root.glob(relative_pattern))]


def _event_index(run_root: Path, event_groups: Iterable[Iterable[Mapping[str, Any]]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for group in event_groups:
        for event in group:
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                index[event_id] = event
    for path in sorted((run_root / "transport" / "identity").glob("*.json")):
        event = _load_json(path)
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            index[event_id] = event
    return index


def _canonical_pair(value: object) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return None
    first, second = sorted((value[0], value[1]))
    return first, second


def _prediction(environment: Mapping[str, Any]) -> Mapping[str, Any]:
    world = environment.get("world_model_info")
    prediction = world.get("perception_predictions") if isinstance(world, Mapping) else None
    if not isinstance(prediction, Mapping):
        raise ValueError("environment event has no Mission 2 prediction evidence")
    return prediction


def _environment_key(event: Mapping[str, Any]) -> tuple[float, int, int]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("environment transport event has no payload")
    return (
        float(payload["mission_time_seconds"]),
        int(payload["state_version"]),
        int(event.get("sequence", 0)),
    )


def ordered_environment_events(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Sort by mission time/state version and retain the latest duplicate tuple."""

    ordered = sorted(events, key=_environment_key)
    result: list[Mapping[str, Any]] = []
    previous_key: tuple[float, int] | None = None
    for event in ordered:
        time_s, state_version, _ = _environment_key(event)
        key = (time_s, state_version)
        if key == previous_key:
            result[-1] = event
        else:
            result.append(event)
            previous_key = key
    return result


def _snapshots_by_environment(
    snapshots: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in snapshots:
        payload = event.get("payload")
        reference = payload.get("environment_data") if isinstance(payload, Mapping) else None
        if isinstance(reference, str):
            result[reference].append(event)
    for matching in result.values():
        matching.sort(
            key=lambda event: (
                int(event.get("payload", {}).get("version", 0)),
                int(event.get("sequence", 0)),
            )
        )
    return result


def _resolved_status(
    snapshot_event: Mapping[str, Any], event_index: Mapping[str, Mapping[str, Any]]
) -> FSMStatus:
    payload = snapshot_event.get("payload")
    reference = payload.get("fsm_status") if isinstance(payload, Mapping) else None
    event = event_index.get(reference) if isinstance(reference, str) else None
    if not isinstance(event, Mapping) or event.get("event_kind") != "fsm-status":
        raise ValueError(f"unresolved FSM status reference: {reference!r}")
    status_payload = event.get("payload")
    if not isinstance(status_payload, Mapping):
        raise ValueError("resolved FSM status has no payload")
    return FSMStatus.from_dict(status_payload)


def _planned_pairs(run_root: Path) -> dict[int, tuple[tuple[int, int], ...]]:
    result: dict[int, tuple[tuple[int, int], ...]] = {}
    for path in sorted(run_root.glob("planner-artifacts/revision-*/workspace/*/statechart.json")):
        match = re.search(r"revision-(\d+)", str(path))
        if match is None:
            continue
        chart = _load_json(path)
        contexts = chart.get("state_context", {})
        pairs: set[tuple[int, int]] = set()
        if isinstance(contexts, Mapping):
            for context in contexts.values():
                pair = _canonical_pair(context.get("risk_pair")) if isinstance(context, Mapping) else None
                if pair is not None:
                    pairs.add(pair)
        result[int(match.group(1))] = tuple(sorted(pairs))
    return result


def _active_pair_rows(prediction: Mapping[str, Any]) -> dict[tuple[int, int], Mapping[str, Any]]:
    result: dict[tuple[int, int], Mapping[str, Any]] = {}
    rows = prediction.get("active_pairs", ())
    if isinstance(rows, (list, tuple)):
        for row in rows:
            pair = _canonical_pair(row.get("ship_ids")) if isinstance(row, Mapping) else None
            if pair is not None:
                result[pair] = row
    return result


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pair_materially_moved(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    old_contact = _finite(old.get("predicted_contact_at_s"))
    new_contact = _finite(new.get("predicted_contact_at_s"))
    if old_contact is not None and new_contact is not None:
        contact_delta = abs(new_contact - old_contact)
        if contact_delta > MISSION2_CONTACT_TIME_DELTA_S or math.isclose(
            contact_delta, MISSION2_CONTACT_TIME_DELTA_S
        ):
            return True
    old_position = old.get("position")
    new_position = new.get("position")
    if isinstance(old_position, Mapping) and isinstance(new_position, Mapping):
        old_x, old_y = _finite(old_position.get("x")), _finite(old_position.get("y"))
        new_x, new_y = _finite(new_position.get("x")), _finite(new_position.get("y"))
        if None not in (old_x, old_y, new_x, new_y):
            distance = math.hypot(new_x - old_x, new_y - old_y)
            if distance > MISSION2_CPA_POSITION_DELTA_M or math.isclose(
                distance, MISSION2_CPA_POSITION_DELTA_M
            ):
                return True
    old_probability, new_probability = _finite(old.get("probability")), _finite(new.get("probability"))
    if (old_probability is None) != (new_probability is None):
        return True
    return bool(
        old_probability is not None
        and new_probability is not None
        and (
            abs(new_probability - old_probability) > MISSION2_PROBABILITY_DELTA
            or math.isclose(
                abs(new_probability - old_probability), MISSION2_PROBABILITY_DELTA
            )
        )
    )


def _prediction_timeline(
    environment_events: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, int], Mapping[str, Any]]:
    timeline: dict[tuple[str, int], Mapping[str, Any]] = {}
    for event in ordered_environment_events(environment_events):
        environment = event["payload"]
        prediction = _prediction(environment)
        key = (str(prediction["run_id"]), int(prediction["sequence"]))
        timeline.setdefault(key, environment)
    return timeline


def _previous_prediction(
    timeline: Mapping[tuple[str, int], Mapping[str, Any]], run_id: str, sequence: int
) -> Mapping[str, Any] | None:
    previous = [key for key in timeline if key[0] == run_id and key[1] < sequence]
    return timeline[max(previous, key=lambda item: item[1])] if previous else None


def recorded_replan_incidents(
    outcomes: Iterable[Mapping[str, Any]],
    environment_events: Iterable[Mapping[str, Any]],
    planned_pairs: Mapping[int, tuple[tuple[int, int], ...]],
) -> list[Incident]:
    timeline = _prediction_timeline(environment_events)
    incidents: list[Incident] = []
    for event in sorted(outcomes, key=lambda item: int(item.get("sequence", 0))):
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("disposition") != "replan":
            continue
        for identity in payload.get("trigger_identities", ()):
            match = _TRIGGER_RE.match(identity) if isinstance(identity, str) else None
            if match is None:
                continue
            run_id = match.group("run_id")
            sequence = int(match.group("sequence"))
            environment = timeline.get((run_id, sequence))
            if environment is None:
                continue
            prediction = _prediction(environment)
            previous_environment = _previous_prediction(timeline, run_id, sequence)
            previous_prediction = _prediction(previous_environment) if previous_environment else {}
            current_rows = _active_pair_rows(prediction)
            previous_rows = _active_pair_rows(previous_prediction)
            previous_alerts = {
                row.get("event_id")
                for row in previous_prediction.get("alerts", ())
                if isinstance(row, Mapping) and isinstance(row.get("event_id"), str)
            }
            alert_ids = tuple(
                sorted(
                    row["event_id"]
                    for row in prediction.get("alerts", ())
                    if isinstance(row, Mapping)
                    and isinstance(row.get("event_id"), str)
                    and row["event_id"] not in previous_alerts
                )
            )
            plan_revision = int(payload["plan_revision"])
            plan_pairs = set(planned_pairs.get(plan_revision, ()))
            added = tuple(sorted(current_rows.keys() - plan_pairs))
            moved = tuple(
                sorted(
                    pair
                    for pair in current_rows.keys() & previous_rows.keys()
                    if _pair_materially_moved(previous_rows[pair], current_rows[pair])
                )
            )
            removed = next(
                (pair for pair in plan_pairs if pair not in current_rows),
                None,
            )
            incidents.append(
                Incident(
                    str(event.get("event_id", "")),
                    identity,
                    plan_revision,
                    run_id,
                    sequence,
                    alert_ids,
                    added,
                    removed,
                    moved,
                    tuple(sorted(set(current_rows) | plan_pairs)),
                )
            )
    return incidents


def _json_signature(signature: tuple[object, ...]) -> list[Any]:
    return json.loads(json.dumps(signature))


def _match_incident(incident: Incident, triggers: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    incident_pairs = set(incident.matching_pairs)
    candidates: list[Mapping[str, Any]] = []
    for trigger in triggers:
        if trigger.get("risk_run_id") != incident.run_id:
            continue
        sequence = int(trigger["prediction_sequence"])
        if sequence > incident.prediction_sequence + 2:
            continue
        fresh = set(trigger.get("fresh_alert_ids", ()))
        affected = {
            pair
            for value in trigger.get("affected_pairs", ())
            if (pair := _canonical_pair(value)) is not None
        }
        same_alert = bool(fresh & set(incident.alert_ids))
        same_pair = bool(affected & incident_pairs)
        cleared = (
            incident.removed_planned_pair is not None
            and incident.removed_planned_pair in affected
            and trigger.get("reason") == "planned_risk_cleared"
        )
        if same_alert or same_pair or cleared:
            candidates.append(trigger)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda trigger: (
            abs(int(trigger["prediction_sequence"]) - incident.prediction_sequence),
            -int(trigger["prediction_sequence"]),
        ),
    )


def replay_run(run_root: Path) -> dict[str, Any]:
    run_root = Path(run_root)
    environments = _load_events(run_root, "transport/topics/environment-data/**/*.json")
    snapshots = _load_events(run_root, "transport/topics/mission-snapshots/**/*.json")
    statuses = _load_events(run_root, "transport/topics/fsm-status/**/*.json")
    outcomes = _load_events(run_root, "transport/topics/hyper-heartbeat-outcomes/**/*.json")
    if not environments:
        raise ValueError("run has no environment-data events")
    event_index = _event_index(run_root, (environments, snapshots, statuses, outcomes))
    snapshot_index = _snapshots_by_environment(snapshots)
    ordered = ordered_environment_events(environments)
    gate = Mission2ReplanGate()
    last_signature: tuple[object, ...] | None = None
    first_plan_revision = next(
        (
            int(snapshot["payload"]["plan_revision"])
            for event in ordered
            if event.get("event_id") in snapshot_index
            for snapshot in snapshot_index[event.get("event_id")]
            if snapshot.get("payload", {}).get("plan_revision") is not None
        ),
        None,
    )
    if first_plan_revision is None:
        raise ValueError("no replayed Mission Snapshot has an accepted plan revision")
    active_plan_revision = first_plan_revision
    material_revisions = 0
    triggers: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()

    for event in ordered:
        event_id = event.get("event_id")
        matching = snapshot_index.get(event_id, ()) if isinstance(event_id, str) else ()
        if not matching:
            raise ValueError(f"no Mission Snapshot references environment event {event_id!r}")
        active_matches = [
            item
            for item in matching
            if item.get("payload", {}).get("plan_revision") == active_plan_revision
        ]
        snapshot = active_matches[-1] if active_matches else matching[-1]
        snapshot_payload = snapshot["payload"]
        status = _resolved_status(snapshot, event_index)
        plan_revision = active_plan_revision
        environment = event["payload"]
        decision = gate.assess(environment, status)
        revision = decision.risk_revision
        if revision.material_changed:
            material_revisions += 1
        signature = (
            plan_revision,
            revision.run_id,
            revision.revision,
            status.active_state,
            decision.current_candidate_id,
            decision.advisory_candidate_ids,
            decision.reason,
        )
        if decision.trigger and signature != last_signature:
            last_signature = signature
            row = {
                "mission_time_seconds": float(environment["mission_time_seconds"]),
                "prediction_sequence": revision.prediction_sequence,
                "plan_revision": plan_revision,
                "risk_revision": revision.revision,
                "risk_run_id": revision.run_id,
                "reason": decision.reason,
                "affected_pairs": [list(pair) for pair in decision.affected_pairs],
                "fresh_alert_ids": list(revision.fresh_alert_ids),
                "current_candidate_id": decision.current_candidate_id,
                "advisory_candidate_ids": list(decision.advisory_candidate_ids),
                "dedup_signature": _json_signature(signature),
            }
            triggers.append(row)
            reason_counts[decision.reason] += 1
        accepted_revision = max(
            int(item.get("payload", {}).get("plan_revision") or plan_revision)
            for item in matching
        )
        if accepted_revision != active_plan_revision:
            active_plan_revision = accepted_revision
            last_signature = None

    planned_pairs = _planned_pairs(run_root)
    incidents = recorded_replan_incidents(outcomes, environments, planned_pairs)
    preserved: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for incident in incidents:
        row = incident.to_dict()
        match = _match_incident(incident, triggers)
        if match is None:
            missing.append(row)
        else:
            row["matched_replay_trigger"] = match
            preserved.append(row)

    result_path = run_root / "closed-loop-result.json"
    recorded_result = _load_json(result_path) if result_path.exists() else {}
    supervisor_count = recorded_result.get("hyper_heartbeat_count", len(outcomes))
    tick_count = recorded_result.get("tick_count", len(ordered))
    replan_count = sum(
        isinstance(event.get("payload"), Mapping)
        and event["payload"].get("disposition") == "replan"
        for event in outcomes
    )
    return {
        "run_root": str(run_root),
        "thresholds": {
            "contact_time_delta_s": MISSION2_CONTACT_TIME_DELTA_S,
            "cpa_position_delta_m": MISSION2_CPA_POSITION_DELTA_M,
            "probability_delta": MISSION2_PROBABILITY_DELTA,
            "advisory_improvement": MISSION2_ADVISORY_IMPROVEMENT_THRESHOLD,
        },
        "recorded": {
            "ticks": int(tick_count),
            "supervisor_invocations": int(supervisor_count),
            "replans": int(replan_count),
        },
        "replayed": {
            "material_risk_revisions": material_revisions,
            "supervisor_invocations": len(triggers),
            "trigger_reason_counts": dict(sorted(reason_counts.items())),
            "trigger_prediction_sequences": [
                row["prediction_sequence"] for row in triggers
            ],
            "triggers": triggers,
        },
        "preserved_replan_incidents": preserved,
        "missing_replan_incidents": missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = replay_run(args.run_root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"replay failed: {exc}\n")
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_output is None:
        print(rendered, end="")
    else:
        args.json_output.write_text(rendered, encoding="utf-8")
    return 0 if not report["missing_replan_incidents"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
