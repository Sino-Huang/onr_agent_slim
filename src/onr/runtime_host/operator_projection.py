"""Incremental operator-facing projection of one Mission Run.

The projection joins the Runtime Host's durable public observation log with the
same debug loaders and step parser used by the read-only viewer.  Recorded model
reasoning remains explicitly non-authoritative and is omitted when runtime debug
recording is disabled.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from urllib.parse import quote

from onr.contracts.environment import (
    environment_controlled_vehicle,
    environment_maneuver_lifecycle,
    environment_mission_time,
    environment_world_model_info,
)
from onr.runtime_host.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    ArtifactNotFoundError,
    ArtifactUnavailableError,
    PublicArtifactInbox,
    _open_confined,
    _same_file_state,
    _snap_end_backward,
    _snap_start_forward,
)
from onr.runtime_host.observations import InvalidCursorError
from onr.viewer.debug import load_debug_artifacts, load_llm_conversations
from onr.viewer.steps import Step, StepProjection

OPERATOR_VIEW_SCHEMA_VERSION = 1
OPERATOR_DEFAULT_LIMIT = 50
OPERATOR_MAX_LIMIT = 100
PLANNER_MAX_BYTES = 1024 * 1024
PLANNER_PREVIEW_BYTES = 4096

OperatorSection = Literal["overview", "agents", "environment", "artifacts"]

_AGENT_ROLES = {"hyper-agent", "maneuver-control"}
_PLANNER_LABELS = {
    "model.mzn": "MiniZinc model",
    "data.dzn": "MiniZinc data",
    "domain.pddl": "PDDL domain",
    "problem.pddl": "PDDL problem",
    "minizinc.plan": "MiniZinc plan",
    "sas_plan": "Fast Downward plan",
    "generate_statechart.py": "Statechart generator",
    "statechart.json": "Statechart draft",
    "accepted-statechart.json": "Accepted Statechart",
    "statechart-error.txt": "Statechart diagnostic",
}
_FILTERED_ENVIRONMENT_KINDS = {
    "environment-data",
    "fsm-execution-record",
    "fsm-status",
    "mission-snapshot",
    "source-fact",
    "statechart",
}


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _signature(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cursor(section: str, mission_run_id: str, sequence: int) -> str:
    payload = json.dumps(
        {"v": 1, "run": mission_run_id, "section": section, "seq": sequence},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(
    value: str,
    *,
    section: str,
    mission_run_id: str,
    maximum: int,
) -> int:
    try:
        if not value or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            raise ValueError
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
            raise ValueError
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "run",
            "section",
            "seq",
        }:
            raise ValueError
        sequence = payload["seq"]
        if (
            payload["v"] != 1
            or payload["run"] != mission_run_id
            or payload["section"] != section
            or type(sequence) is not int
            or sequence < 0
            or sequence > maximum
        ):
            raise ValueError
        return cast(int, sequence)
    except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidCursorError from exc


@dataclass(slots=True)
class _SectionState:
    sequence: int = 0
    current: dict[str, dict[str, object]] = field(default_factory=dict)
    signatures: dict[str, str] = field(default_factory=dict)
    changed_at: dict[str, int] = field(default_factory=dict)
    journal: list[tuple[int, dict[str, object]]] = field(default_factory=list)

    def update(self, records: Iterable[tuple[str, Mapping[str, object]]]) -> None:
        for identity, record in records:
            selected = cast(dict[str, object], _plain(record))
            signature = _signature(selected)
            if self.signatures.get(identity) == signature:
                continue
            self.sequence += 1
            self.current[identity] = selected
            self.signatures[identity] = signature
            self.changed_at[identity] = self.sequence
            self.journal.append((self.sequence, selected))

    def page(
        self,
        *,
        section: str,
        mission_run_id: str,
        limit: int,
        cursor: str | None,
        before: str | None,
    ) -> tuple[list[dict[str, object]], str, str | None, bool]:
        maximum = self.sequence
        if cursor is not None:
            after = _decode_cursor(
                cursor,
                section=section,
                mission_run_id=mission_run_id,
                maximum=maximum,
            )
            pending = [
                (sequence, item) for sequence, item in self.journal if sequence > after
            ]
            selected = pending[:limit]
            high_water = selected[-1][0] if selected else after
            return (
                [dict(item) for _, item in selected],
                _cursor(section, mission_run_id, high_water),
                None,
                len(pending) > len(selected),
            )

        if before is not None:
            boundary = _decode_cursor(
                before,
                section=section,
                mission_run_id=mission_run_id,
                maximum=maximum,
            )
            eligible = [item for item in self.journal if item[0] < boundary]
            selected = eligible[-limit:]
            previous = (
                _cursor(section, mission_run_id, selected[0][0])
                if selected and len(eligible) > len(selected)
                else None
            )
            return (
                [dict(item) for _, item in selected],
                _cursor(section, mission_run_id, maximum),
                previous,
                len(eligible) > len(selected),
            )

        ordered = sorted(
            self.current,
            key=lambda identity: (self.changed_at[identity], identity),
        )
        selected_ids = ordered[-limit:]
        previous = (
            _cursor(section, mission_run_id, self.changed_at[selected_ids[0]])
            if len(ordered) > len(selected_ids) and selected_ids
            else None
        )
        return (
            [dict(self.current[identity]) for identity in selected_ids],
            _cursor(section, mission_run_id, maximum),
            previous,
            len(ordered) > len(selected_ids),
        )


@dataclass(slots=True)
class _RunState:
    observations: dict[int, dict[str, object]] = field(default_factory=dict)
    sections: dict[str, _SectionState] = field(default_factory=dict)
    environment: dict[str, object] | None = None
    agent_observation_sequence: int = 0
    environment_observation_sequence: int = 0

    def section(self, name: str) -> _SectionState:
        return self.sections.setdefault(name, _SectionState())


def _flatten_steps(steps: Iterable[Step]) -> list[Step]:
    result: list[Step] = []
    for step in steps:
        result.append(step)
        result.extend(_flatten_steps(step.children))
    return result


def _observation_item(entry: Mapping[str, object]) -> dict[str, object] | None:
    item = entry.get("item")
    return dict(item) if isinstance(item, Mapping) else None


def _agent_progress(
    entry: Mapping[str, object],
) -> tuple[str, dict[str, object]] | None:
    item = _observation_item(entry)
    if item is None or item.get("component") not in _AGENT_ROLES:
        return None
    role = cast(str, item["component"])
    event_id = (
        _text(item.get("event_id"))
        or f"observation-{entry.get('observation_sequence', 0)}"
    )
    event_kind = _text(item.get("event_kind")) or "progress"
    payload = item.get("payload")
    decision = {
        "event_kind": event_kind,
        "outcome": item.get("outcome"),
        "details": _plain(payload) if isinstance(payload, Mapping) else {},
    }
    status = "error" if item.get("outcome") in {"error", "failed", "rejected"} else "ok"
    stable_id = f"evidence:{event_id}"
    return stable_id, {
        "stable_id": stable_id,
        "invocation_id": event_id,
        "parent_id": item.get("parent_id"),
        "role": role,
        "phase": event_kind,
        "kind": "progress",
        "name": event_kind,
        "status": status,
        "completion_state": "complete",
        "started_at": item.get("occurred_at"),
        "updated_at": item.get("occurred_at"),
        "finished_at": item.get("occurred_at"),
        "duration_ms": 0,
        "revision": 1,
        "outcome": item.get("outcome"),
        "content": None,
        "decision": decision,
        "recorded_debug_reasoning": {
            "label": "Recorded Debug Reasoning",
            "authority": "non-authoritative",
            "disposition": "debug_evidence_unavailable",
            "content": None,
        },
        "tool_calls": [],
        "debug_payload_disposition": "debug_evidence_unavailable",
    }


def _agent_records(
    *,
    mission_id: str,
    storage_root: Path,
    debug: bool,
    observations: Sequence[Mapping[str, object]],
) -> list[tuple[str, dict[str, object]]]:
    records: dict[str, dict[str, object]] = {}
    for entry in observations:
        progress = _agent_progress(entry)
        if progress is not None:
            records[progress[0]] = progress[1]

    if not debug:
        return list(records.items())

    _, invocations = load_debug_artifacts(storage_root, mission_id)
    conversations = load_llm_conversations(storage_root, mission_id)
    view = StepProjection().project(
        mission_id,
        agent_invocations=invocations,
        llm_records=conversations,
    )
    invocation_by_role_sequence = {
        (record.get("role", record.get("agent_role")), record.get("sequence")): record
        for record in invocations
    }
    for step in _flatten_steps(view.steps):
        if step.role not in _AGENT_ROLES:
            continue
        raw = invocation_by_role_sequence.get((step.role, step.seq), {})
        invocation_id = _text(raw.get("invocation_id")) or step.step_id
        parent_id = _text(raw.get("parent_id"))
        stable_id = f"{step.role}:{invocation_id}"
        records[stable_id] = {
            "stable_id": stable_id,
            "invocation_id": invocation_id,
            "parent_id": parent_id,
            "role": step.role,
            "phase": step.phase,
            "kind": step.kind,
            "name": step.name,
            "status": step.status,
            "completion_state": step.completion_state,
            "started_at": step.started_at,
            "updated_at": step.updated_at,
            "finished_at": step.finished_at,
            "duration_ms": step.duration_ms,
            "revision": step.revision,
            "outcome": step.outcome,
            "content": step.content,
            "decision": _plain(step.decision),
            "recorded_debug_reasoning": {
                "label": "Recorded Debug Reasoning",
                "authority": "non-authoritative",
                "disposition": (
                    "available" if step.reasoning is not None else "not_recorded"
                ),
                "content": step.reasoning,
            },
            "tool_calls": [call.to_dict() for call in step.tool_calls],
            "debug_payload_disposition": "available",
        }
    return list(records.items())


def _environment_record(
    entry: Mapping[str, object], *, raw: bool
) -> tuple[str, dict[str, object]] | None:
    item = _observation_item(entry)
    if item is None:
        return None
    event_kind = _text(item.get("event_kind")) or "unknown"
    disposition = _text(item.get("replay_disposition")) or "normal"
    if not raw and (
        disposition != "normal"
        or "heartbeat" in event_kind
        or event_kind in _FILTERED_ENVIRONMENT_KINDS
    ):
        return None
    sequence = _integer(entry.get("observation_sequence")) or 0
    event_id = _text(item.get("event_id")) or f"observation-{sequence}"
    stable_id = f"observation:{sequence}:{event_id}"
    missing = item.get("missing_fields")
    warnings = list(missing) if isinstance(missing, (list, tuple)) else []
    return stable_id, {
        "stable_id": stable_id,
        "observation_sequence": sequence,
        "event_id": event_id,
        "occurred_at": item.get("occurred_at"),
        "component": item.get("component"),
        "authority": item.get("authority"),
        "event_kind": event_kind,
        "status": item.get("status"),
        "outcome": item.get("outcome"),
        "correlation_id": item.get("correlation_id"),
        "replay_disposition": disposition,
        "payload": _plain(item.get("payload")),
        "warnings": warnings,
    }


def _latest_item(
    observations: Sequence[Mapping[str, object]], event_kinds: set[str]
) -> dict[str, object] | None:
    for entry in reversed(observations):
        item = _observation_item(entry)
        if item is not None and item.get("event_kind") in event_kinds:
            return item
    return None


def _safe_environment(root: Path, mission_id: str) -> dict[str, object] | None:
    path = root / quote(mission_id, safe="._-") / "environment.json"
    descriptor: int | None = None
    try:
        descriptor = _open_confined(path, root)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > PLANNER_MAX_BYTES:
            return None
        data = os.read(descriptor, PLANNER_MAX_BYTES + 1)
        if len(data) > PLANNER_MAX_BYTES:
            return None
        value = json.loads(data.decode("utf-8"))
        return cast(dict[str, object], value) if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _current_environment(
    environment: Mapping[str, object] | None,
    observations: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    evidence = environment if isinstance(environment, Mapping) else {}
    try:
        controlled_vehicle = environment_controlled_vehicle(evidence)
        mission_time = environment_mission_time(evidence)
        maneuver_lifecycle = environment_maneuver_lifecycle(evidence)
        world_model_info = environment_world_model_info(evidence)
    except (TypeError, ValueError):
        controlled_vehicle = {}
        mission_time = None
        maneuver_lifecycle = None
        world_model_info = {}
    fsm = _latest_item(observations, {"fsm-status", "fsm-execution-record"})
    fsm_payload = fsm.get("payload") if isinstance(fsm, Mapping) else None
    fsm_payload = fsm_payload if isinstance(fsm_payload, Mapping) else {}
    feedback = _latest_item(observations, {"maneuver-feedback"})
    feedback_payload = (
        feedback.get("payload") if isinstance(feedback, Mapping) else None
    )
    beliefs = [
        item
        for entry in observations
        if (item := _observation_item(entry)) is not None
        and item.get("event_kind")
        in {"belief.updated", "belief.constraints", "risk.observed"}
    ]
    warnings: list[str] = []
    for entry in observations:
        item = _observation_item(entry)
        missing = item.get("missing_fields") if item is not None else None
        if isinstance(missing, (list, tuple)):
            warnings.extend(str(value) for value in missing)
    return {
        "authority": "Runtime Host Mission Run state and environment evidence",
        "position": _plain(controlled_vehicle.get("position")),
        "velocity": _plain(
            controlled_vehicle.get(
                "velocity", controlled_vehicle.get("speed_mps")
            )
        ),
        "mission_time_seconds": mission_time,
        "fsm_state": fsm_payload.get("active_state", fsm_payload.get("state")),
        "fsm_status": fsm.get("status") if fsm is not None else None,
        "active_maneuver": _plain(maneuver_lifecycle),
        "maneuver_feedback": _plain(feedback_payload),
        "world_model_info": _plain(world_model_info),
        "perceptions": _plain(evidence.get("perceptions", [])),
        "belief_changes": [_plain(item) for item in beliefs[-10:]],
        "warnings": list(dict.fromkeys(warnings))[-20:],
    }


def _planner_label(name: str, ref: str) -> str:
    base = _PLANNER_LABELS[name]
    for component in PurePosixPath(ref).parts[:-1]:
        if component.isdigit():
            return f"{base} (attempt {int(component)})"
    return base


def _planner_artifact_id(ref: str) -> str:
    encoded = base64.urlsafe_b64encode(ref.encode("utf-8")).decode("ascii").rstrip("=")
    return f"planner-{encoded}"


def _planner_ref(artifact_id: str) -> str | None:
    try:
        encoded = artifact_id.removeprefix("planner-")
        if not encoded or artifact_id == encoded:
            return None
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != encoded:
            return None
        ref = decoded.decode("utf-8")
        return ref if _valid_planner_ref(ref) else None
    except (UnicodeError, ValueError):
        return None


def _valid_planner_ref(ref: str) -> bool:
    if not ref or "\\" in ref or "\x00" in ref:
        return False
    relative = PurePosixPath(ref)
    return (
        not relative.is_absolute()
        and all(part not in {"", ".", ".."} for part in relative.parts)
        and relative.name in _PLANNER_LABELS
    )


def _planner_artifacts(root: Path) -> list[dict[str, object]]:
    try:
        root_mode = root.lstat().st_mode
    except OSError:
        return []
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        return []
    pending = [root]
    result: list[dict[str, object]] = []
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(directory.iterdir())
        except OSError:
            continue
        for path in sorted(entries, key=lambda item: item.name):
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISLNK(mode):
                continue
            if stat.S_ISDIR(mode):
                pending.append(path)
                continue
            if path.name not in _PLANNER_LABELS or not stat.S_ISREG(mode):
                continue
            ref = path.relative_to(root).as_posix()
            if not _valid_planner_ref(ref):
                continue
            metadata = path.stat()
            if metadata.st_size > PLANNER_MAX_BYTES:
                continue
            media_type = {
                ".json": "application/json",
                ".py": "text/x-python",
                ".pddl": "text/plain",
                ".mzn": "text/plain",
                ".dzn": "text/plain",
            }.get(path.suffix.lower(), "text/plain")
            result.append(
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "artifact_id": _planner_artifact_id(ref),
                    "kind": path.name,
                    "media_type": media_type,
                    "byte_size": metadata.st_size,
                    "content_digest": None,
                    "display": {
                        "title": _planner_label(path.name, ref),
                        "summary": ref,
                    },
                    "published_at": datetime.fromtimestamp(
                        metadata.st_mtime, UTC
                    ).isoformat(),
                    "classification": "text",
                    "source": "planner",
                    "ref": ref,
                }
            )
    return sorted(result, key=lambda item: cast(str, item["ref"]))


def _public_artifacts(
    inbox: PublicArtifactInbox, mission_id: str, mission_run_id: str
) -> list[dict[str, object]]:
    cursor: str | None = None
    result: list[dict[str, object]] = []
    while True:
        page = inbox.artifacts(
            mission_id,
            mission_run_id,
            cursor=cursor,
            limit=OPERATOR_MAX_LIMIT,
        )
        raw = page.get("artifacts")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping):
                    result.append(
                        {
                            **cast(dict[str, object], _plain(item)),
                            "source": "public_inbox",
                        }
                    )
        cursor = _text(page.get("next_cursor"))
        if cursor is None:
            return result


class OperatorRunProjection:
    """Stateful incremental projection shared by all operator-view sections."""

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}

    def view(
        self,
        *,
        run: Mapping[str, object],
        observations: Sequence[Mapping[str, object]],
        storage_root: Path,
        environment_root: Path,
        planner_root: Path,
        artifact_inbox: PublicArtifactInbox,
        narrative: Mapping[str, object],
        debug: bool,
        section: OperatorSection,
        limit: int,
        cursor: str | None,
        before: str | None,
        raw: bool,
    ) -> dict[str, object]:
        mission_id = cast(str, run["mission_id"])
        mission_run_id = cast(str, run["mission_run_id"])
        state = self._runs.setdefault(mission_run_id, _RunState())
        for entry in observations:
            sequence = _integer(entry.get("observation_sequence"))
            if sequence is not None:
                state.observations.setdefault(sequence, dict(entry))
        ordered_observations = [
            state.observations[key] for key in sorted(state.observations)
        ]

        current_environment: dict[str, object] | None = None
        if section in {"overview", "environment"}:
            environment = _safe_environment(environment_root, mission_id)
            if environment is not None:
                state.environment = environment
            current_environment = _current_environment(
                state.environment, ordered_observations
            )

        section_key = (
            section if section != "environment" else f"environment:{str(raw).lower()}"
        )
        selected_state = state.section(section_key)
        if section in {"overview", "agents"}:
            agent_observations = [
                entry
                for entry in ordered_observations
                if cast(int, entry["observation_sequence"])
                > state.agent_observation_sequence
            ]
            agents = _agent_records(
                mission_id=mission_id,
                storage_root=storage_root,
                debug=debug,
                observations=agent_observations,
            )
            state.section("agents").update(agents)
            if agent_observations:
                state.agent_observation_sequence = cast(
                    int, agent_observations[-1]["observation_sequence"]
                )
        if section in {"overview", "environment"}:
            environment_observations = [
                entry
                for entry in ordered_observations
                if cast(int, entry["observation_sequence"])
                > state.environment_observation_sequence
            ]
            filtered_records = [
                record
                for entry in environment_observations
                if (record := _environment_record(entry, raw=False)) is not None
            ]
            raw_records = [
                record
                for entry in environment_observations
                if (record := _environment_record(entry, raw=True)) is not None
            ]
            state.section("environment:false").update(filtered_records)
            state.section("environment:true").update(raw_records)
            state.section("overview").update(filtered_records)
            if environment_observations:
                state.environment_observation_sequence = cast(
                    int, environment_observations[-1]["observation_sequence"]
                )
        if section in {"overview", "artifacts"}:
            artifacts = [
                *_public_artifacts(artifact_inbox, mission_id, mission_run_id),
                *_planner_artifacts(planner_root),
            ]
            state.section("artifacts").update(
                (cast(str, artifact["artifact_id"]), artifact) for artifact in artifacts
            )

        records, next_cursor, before_cursor, has_more = selected_state.page(
            section=section_key,
            mission_run_id=mission_run_id,
            limit=limit,
            cursor=cursor,
            before=before,
        )
        response: dict[str, object] = {
            "schema_version": OPERATOR_VIEW_SCHEMA_VERSION,
            "mission_id": mission_id,
            "mission_run_id": mission_run_id,
            "run_status": run.get("status"),
            "section": section,
            "debug": {
                "enabled": debug,
                "reasoning_label": "Recorded Debug Reasoning",
                "reasoning_authority": "non-authoritative",
                "disposition": "available" if debug else "debug_evidence_unavailable",
            },
            "next_cursor": next_cursor,
            "before_cursor": before_cursor,
            "has_more": has_more,
        }
        if section == "agents":
            response["agents"] = records
        elif section == "environment":
            assert current_environment is not None
            response["environment"] = {
                **current_environment,
                "raw": raw,
                "timeline": records,
            }
        elif section == "artifacts":
            response["artifacts"] = records
        else:
            assert current_environment is not None
            agent_values = list(state.section("agents").current.values())
            latest_agents: dict[str, object] = {}
            agent_state = state.section("agents")
            for role, key in (
                ("hyper_agent", "hyper-agent"),
                ("maneuver_control", "maneuver-control"),
            ):
                matching = [
                    (identity, item)
                    for identity, item in agent_state.current.items()
                    if item.get("role") == key
                ]
                latest_agents[role] = (
                    max(
                        matching,
                        key=lambda pair: agent_state.changed_at[pair[0]],
                    )[1]
                    if matching
                    else None
                )
            artifact_values = list(state.section("artifacts").current.values())
            response["overview"] = {
                "authority": "Runtime Host Mission Run Record",
                "latest_agents": latest_agents,
                "fsm": {
                    "state": current_environment["fsm_state"],
                    "status": current_environment["fsm_status"],
                },
                "environment": {
                    key: current_environment[key]
                    for key in ("position", "velocity", "mission_time_seconds")
                },
                "active_maneuver": current_environment["active_maneuver"],
                "recent_events": records,
                "counts": {
                    "agents": len(agent_values),
                    "environment_events": len(
                        state.section("environment:false").current
                    ),
                    "artifacts": len(artifact_values),
                    "warnings": len(
                        cast(list[object], current_environment["warnings"])
                    ),
                },
                "narrative": _plain(narrative),
                "hitl": {
                    "status": (
                        "awaiting_human_decision"
                        if run.get("status") == "awaiting_human_decision"
                        else "none"
                    ),
                    "requires_action": run.get("status") == "awaiting_human_decision",
                },
            }
        return response

    def planner_artifact_content(
        self,
        *,
        mission_id: str,
        mission_run_id: str,
        planner_root: Path,
        artifact_id: str,
        offset: int | None,
        limit: int | None,
    ) -> dict[str, object]:
        ref = _planner_ref(artifact_id)
        if ref is None:
            raise ArtifactNotFoundError
        descriptors = {
            cast(str, item["artifact_id"]): item
            for item in _planner_artifacts(planner_root)
        }
        descriptor = descriptors.get(artifact_id)
        if descriptor is None:
            raise ArtifactNotFoundError
        requested_offset = 0 if offset is None else offset
        requested_limit = PLANNER_PREVIEW_BYTES if limit is None else limit
        if requested_offset < 0 or not 1 <= requested_limit <= PLANNER_PREVIEW_BYTES:
            raise ValueError("invalid planner Artifact content window")

        file_descriptor: int | None = None
        try:
            relative = PurePosixPath(ref)
            file_descriptor = _open_confined(
                planner_root.joinpath(*relative.parts), planner_root
            )
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > PLANNER_MAX_BYTES:
                raise OSError("planner Artifact is unavailable")
            if requested_offset > before.st_size:
                raise ValueError("Artifact offset exceeds content size")
            adjusted_offset = _snap_start_forward(
                file_descriptor, requested_offset, before.st_size
            )
            requested_end = min(before.st_size, requested_offset + requested_limit)
            adjusted_end = max(
                adjusted_offset,
                _snap_end_backward(file_descriptor, requested_end, adjusted_offset),
            )
            window = os.pread(
                file_descriptor, adjusted_end - adjusted_offset, adjusted_offset
            )
            content = window.decode("utf-8", errors="strict")
            if len(window) != adjusted_end - adjusted_offset or not _same_file_state(
                before, os.fstat(file_descriptor)
            ):
                raise OSError("planner Artifact changed during read")
            eof = adjusted_end == before.st_size
            return {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "mission_id": mission_id,
                "mission_run_id": mission_run_id,
                "artifact_id": artifact_id,
                "classification": "text",
                "media_type": descriptor["media_type"],
                "byte_size": before.st_size,
                "offset": adjusted_offset,
                "next_offset": None if eof else adjusted_end,
                "eof": eof,
                "truncated": adjusted_end < requested_end,
                "content": content,
            }
        except ValueError:
            raise
        except (OSError, UnicodeError, TypeError, KeyError) as exc:
            raise ArtifactUnavailableError from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)


__all__ = [
    "OPERATOR_DEFAULT_LIMIT",
    "OPERATOR_MAX_LIMIT",
    "OPERATOR_VIEW_SCHEMA_VERSION",
    "PLANNER_MAX_BYTES",
    "PLANNER_PREVIEW_BYTES",
    "OperatorRunProjection",
    "OperatorSection",
]
