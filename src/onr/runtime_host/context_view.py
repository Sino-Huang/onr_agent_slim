"""Operator-view ``context`` section: Context Coordination's latest view of a run.

Reads only the newest event of each transport topic under
``<transport_root>/topics/<topic>/missions/<mission>/``. Stream file names
start with the zero-padded publication sequence, so the newest event is the
greatest file name; listing a directory is cheap and each immutable event file
is decoded once.

The Active Maneuver comes from the newest ``environment-data`` event's
``maneuver_lifecycle``: that is the source a Mission Snapshot references as
``active_maneuver`` and it advances every environment tick, whereas
``maneuver-feedback`` carries only lifecycle transitions. Pending perceptions
live only in Context Coordination memory and are not projected.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote

from onr.contracts.transport import TransportEvent
from onr.runtime_host.run_files import JsonFileCache, json_file_names

_EVENT_MAX_BYTES = 8 * 1024 * 1024
# Newest files to try when the newest is unreadable or of another kind.
_LATEST_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class _Event:
    sequence: int
    event_kind: str
    mission_id: str
    payload: Mapping[str, object]


def _decode_event(document: Mapping[str, object]) -> _Event:
    # Validate the envelope with the transport contract, but keep the plain
    # JSON payload (the contract freezes arrays into tuples).
    event = TransportEvent.from_dict(document)
    payload = cast(Mapping[str, object], document["payload"])
    return _Event(event.sequence, event.event_kind, event.mission_id, payload)


_EVENTS: JsonFileCache[_Event] = JsonFileCache(
    _decode_event, max_bytes=_EVENT_MAX_BYTES
)


def context_section(transport_root: Path, mission_id: str) -> dict[str, object]:
    """Project the latest Mission Snapshot, FSM Status, maneuver, intent, and Hyper outcome."""

    def latest(topic: str, event_kind: str) -> _Event | None:
        return _latest_event(Path(transport_root), topic, mission_id, event_kind)

    return {
        "mission_snapshot": _mission_snapshot(
            latest("mission-snapshots", "mission-snapshot")
        ),
        "fsm_status": _fsm_status(latest("fsm-status", "fsm-status")),
        "active_maneuver": _active_maneuver(
            latest("environment-data", "environment_data")
        ),
        "latest_transition_intent": _transition_intent(
            latest("transition-intents", "transition-intent")
        ),
        "latest_hyper_outcome": _hyper_outcome(
            latest("hyper-heartbeat-outcomes", "hyper-heartbeat-decision")
        ),
    }


def _latest_event(
    transport_root: Path, topic: str, mission_id: str, event_kind: str
) -> _Event | None:
    # Same stream layout and component encoding as FileTransport.
    stream = (
        transport_root
        / "topics"
        / quote(topic, safe="._-")
        / "missions"
        / quote(mission_id, safe="._-")
    )
    names = sorted(json_file_names(stream), reverse=True)
    for name in names[:_LATEST_ATTEMPTS]:
        event = _EVENTS.get(stream / name)
        if (
            event is not None
            and event.event_kind == event_kind
            and event.mission_id == mission_id
        ):
            return event
    return None


def _mission_snapshot(event: _Event | None) -> dict[str, object] | None:
    if event is None:
        return None
    payload = event.payload
    return {
        "version": _int(payload.get("version")),
        "plan_revision": _int(payload.get("plan_revision")),
        "created_at": _text(payload.get("created_at")),
        "sequence": event.sequence,
        "source_health": _scalar_map(payload.get("source_health"), str),
        "source_freshness": _scalar_map(payload.get("source_freshness"), bool),
        "source_revisions": _scalar_map(payload.get("source_revisions"), int),
        "missing_sources": _texts(payload.get("missing_sources")),
    }


def _fsm_status(event: _Event | None) -> dict[str, object] | None:
    if event is None:
        return None
    payload = event.payload
    candidates = payload.get("transition_candidates")
    return {
        "active_state": _text(payload.get("active_state")),
        "status": _text(payload.get("status")),
        "plan_revision": _int(payload.get("plan_revision")),
        "statechart_revision": _int(payload.get("statechart_revision")),
        "last_applied_event": _text(payload.get("last_applied_event")),
        "sequence": event.sequence,
        # FSM Status publishes only enabled Transition Candidates.
        "transition_candidates": [
            {
                "event": _text(candidate.get("event")),
                "source": _text(candidate.get("source")),
                "target": _text(candidate.get("target")),
                "condition": _text(
                    _mapping(candidate.get("transition_context")).get("desired_outcome")
                ),
                "readiness": _optional_mapping(
                    _mapping(candidate.get("transition_context")).get("readiness")
                ),
            }
            for candidate in (candidates if isinstance(candidates, list) else [])
            if isinstance(candidate, Mapping)
        ],
    }


def _active_maneuver(event: _Event | None) -> dict[str, object] | None:
    if event is None:
        return None
    lifecycle = event.payload.get("maneuver_lifecycle")
    if not isinstance(lifecycle, Mapping):
        return None
    progress = _mapping(lifecycle.get("progress"))
    deadline = _float(_mapping(lifecycle.get("parameters")).get("deadline_time"))
    mission_time = _float(event.payload.get("mission_time_seconds"))
    return {
        "maneuver_id": _text(lifecycle.get("maneuver_id")),
        "action": _text(lifecycle.get("action")),
        "status": _text(lifecycle.get("lifecycle")),
        "phase": _text(lifecycle.get("phase")),
        "progress": _progress_fraction(progress),
        "progress_detail": dict(progress),
        "deadline_seconds": deadline,
        "remaining_seconds": (
            None
            if deadline is None or mission_time is None
            else deadline - mission_time
        ),
        "mission_time_seconds": mission_time,
        "plan_revision": _int(lifecycle.get("plan_revision")),
    }


def _progress_fraction(progress: Mapping[str, object]) -> float | None:
    """Return completion in [0, 1] only where the maneuver reports one (search_area cells)."""

    cleared = _int(progress.get("cleared_cells"))
    total = _int(progress.get("total_cells"))
    if cleared is None or total is None or total <= 0:
        return None
    return min(1.0, max(0.0, cleared / total))


def _transition_intent(event: _Event | None) -> dict[str, object] | None:
    if event is None:
        return None
    payload = event.payload
    return {
        "intent_id": _text(payload.get("intent_id")),
        "status": _text(payload.get("status")),
        "source_state": _text(payload.get("source_state")),
        "target": _text(payload.get("target_state")),
        "condition": _text(_mapping(payload.get("condition")).get("desired_outcome")),
        "rationale": _text(payload.get("rationale")),
        "selected_at_seconds": _float(payload.get("selected_at")),
        "plan_revision": _int(payload.get("plan_revision")),
        "sequence": event.sequence,
    }


def _hyper_outcome(event: _Event | None) -> dict[str, object] | None:
    if event is None:
        return None
    payload = event.payload
    return {
        "disposition": _text(payload.get("disposition")),
        "evidence_summary": _text(payload.get("evidence_summary")),
        "plan_revision": _int(payload.get("plan_revision")),
        "trigger_identities": _texts(payload.get("trigger_identities")),
        "request_identities": _texts(payload.get("request_identities")),
        "sequence": event.sequence,
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _optional_mapping(value: object) -> dict[str, object] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _texts(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _scalar_map(value: object, kind: type) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, kind):
            continue
        if kind is int and isinstance(item, bool):
            continue
        result[key] = item
    return result


__all__ = ["context_section"]
