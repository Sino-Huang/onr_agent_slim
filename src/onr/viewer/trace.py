"""Strict public-record projection for read-only trace viewers."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Literal, cast


ReplayDisposition = Literal[
    "normal", "duplicate", "replayed", "stale", "gap", "resynchronized",
    "conflict", "malformed",
]
_REPLAY_DISPOSITIONS = {
    "normal", "duplicate", "replayed", "stale", "gap", "resynchronized",
    "conflict", "malformed",
}
_ERROR_MESSAGES = {
    "malformed_json": "The public observation was not valid JSON.",
    "non_mapping": "The public observation was not an object.",
    "unsupported_schema": "The public observation schema version is unsupported.",
    "unknown_fields": "The public observation fields are not part of the v1 contract.",
    "invalid_record": "The public observation failed v1 contract validation.",
    "unsupported_shape": "The public observation shape is not a documented v1 record.",
    "envelope_required": "Heterogeneous public observations require a v1 observation envelope.",
}
_ERROR_CATEGORIES = set(_ERROR_MESSAGES)
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_ -])(?:raw[_ -]?)?(?:prompt|completion|reasoning|analysis|messages?|text|"
    r"secret|token|password|authorization|api[_ -]?key|mission[_ -]?memory|memory|"
    r"private|credential)(?:$|[_ -])",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:api[_ -]?key|secret|token|password|authorization)\s*[:=]\s*[^\s,;]+|"
    r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b|Bearer\s+[A-Za-z0-9._-]+"
)

_TRANSPORT_FIELDS = {
    "schema_version", "event_id", "mission_id", "sequence", "event_kind", "payload",
}
_COMMAND_FIELDS = {
    "schema_version", "command_id", "correlation_id", "mission_id", "target_service",
    "command_kind", "payload",
}
_RECEIPT_FIELDS = {
    "schema_version", "command_id", "correlation_id", "mission_id", "target_service", "status",
}
_OUTCOME_FIELDS = {
    "schema_version", "command_id", "correlation_id", "mission_id", "status", "payload",
}
_LOG_FIELDS = {
    "schema_version", "record_id", "mission_id", "sequence", "event_time", "source",
    "event_kind", "outcome", "details",
}
_SUMMARY_FIELDS = {
    "schema_version", "summary_id", "mission_id", "sequence", "created_at",
    "input_start_sequence", "input_end_sequence", "prior_summary_ids", "summary",
}
_SNAPSHOT_FIELDS = {
    "schema_version", "mission_id", "version", "created_at", "plan_revision",
    "plan_reference", "operational_scene_graph", "bayesian_belief_snapshot", "fsm_status",
    "active_maneuver", "source_revisions", "source_references", "source_hashes", "source_health",
    "source_freshness", "missing_sources",
}
_STATECHART_FIELDS = {
    "schema_version", "mission_id", "plan_revision", "mission_snapshot_id",
    "planning_profile", "normalized_plan_sha256", "entry_state", "states", "transitions",
    "timers", "trusted",
}
_FSM_STATUS_FIELDS = {
    "schema_version", "mission_id", "plan_revision", "statechart_revision", "active_state",
    "transition_candidates", "timer_due", "status", "superseded_plan_revision",
    "superseded_maneuver_ids", "last_applied_event", "timer_due_markers",
    "lifecycle_facts", "retained_maneuver_ids",
}
_FSM_EXECUTION_FIELDS = {
    "schema_version", "mission_id", "plan_revision", "statechart_revision", "active_state",
    "active_configuration", "last_applied_event", "transition_history",
    "superseded_plan_revision", "superseded_maneuver_ids", "retained_maneuver_ids",
    "record_revision", "last_applied_event_identity", "applied_event_identities",
    "timer_due_markers", "lifecycle_facts",
}
_MANEUVER_FEEDBACK_FIELDS = {
    "schema_version", "feedback_id", "mission_id", "maneuver_id", "lifecycle",
    "source_sequence", "source", "command_id", "correlation_id", "parent_id",
    "plan_revision", "snapshot_id",
}
_REPLAN_REQUEST_FIELDS = {
    "schema_version", "request_id", "mission_id", "reason", "requester",
    "observed_plan_revision", "source_revisions", "source_sequence",
    "correlation_id", "parent_id", "status", "snapshot_id",
}

# Component and authority are projection policy, never source-controlled identity.
_IDENTITY = {
    "transport_event": ("transport", "transport"),
    "command": ("command-source", "command"),
    "command_receipt": ("transport", "transport"),
    "command_outcome": ("command-target", "command-outcome"),
    "operational_log": ("runtime", "operational-log"),
    "summary": ("mission-log-summarizer", "non-authoritative-summary"),
    "mission_snapshot": ("context-coordination", "derived-snapshot"),
    "statechart": ("fsm-runner", "declarative-statechart"),
    "fsm_status": ("fsm-runner", "fsm-status"),
    "fsm_execution": ("fsm-runner", "durable-fsm-state"),
    "maneuver_feedback": ("environment", "environment-feedback"),
    "replan_request": ("hyper-agent", "hyper-agent"),
}
_LOG_COMPONENTS = {
    "hyper-agent": "hyper-agent", "context-coordination": "context-coordination",
    "fsm-runner": "fsm-runner", "maneuver-control": "maneuver-control",
    "maneuver-adapter": "maneuver-adapter", "transport": "transport",
    "environment": "environment", "runtime": "runtime", "planner": "planner",
}
_LOG_DETAIL_FIELDS = {
    "adapter_submission", "command_id", "correlation_id", "environment", "error_type",
    "event_id", "event_kind", "lifecycle", "maneuver_id", "operation", "plan_revision",
    "planner", "request_id", "revision", "sequence", "service", "snapshot_id", "source",
    "state", "status", "target_service", "topic", "transition", "transport_event_id",
    "transport_sequence", "timer_due",
}
_COMMON_EVENT_PAYLOAD_FIELDS = {
    "action", "active_maneuver", "active_state", "all_physical_actions", "associations", "catalogue",
    "backend", "belief_id", "choice", "command_id", "component", "content_hash", "correlation_id",
    "constraints", "content_sha256", "edges", "event", "event_id", "event_kind",
    "feedback_loop", "fresh", "from", "health", "immutable_versions", "intent", "kind",
    "input_event_id", "input_revision", "likelihood_given_risk", "likelihood_given_safe",
    "maneuver_id", "mission_memory_isolated", "missing", "missing_fields", "nodes",
    "marginals", "non_physical_choice", "normalized_plan", "objective", "operation", "outcome",
    "parameters", "physical_actions", "plan_revision", "planner", "planner_choice",
    "probability",
    "prior_summary_ids", "question", "question_id",
    "redacted_fields", "reference", "resume_sequence", "revision", "role_skills",
    "risk_type", "scene_graph", "snapshot_id", "source_freshness", "source_hashes", "source_health", "source_references",
    "source_revisions", "state", "status", "summary", "target_service", "target_services",
    "skills", "source", "to", "topic", "transition", "translation", "trusted", "version",
}
_TRANSPORT_IDENTITIES = {
    "mission-overview": ("runtime", "mission-overview"),
    "hyper-agent": ("hyper-agent", "hyper-agent"),
    "mission-specification": ("hyper-agent", "hyper-agent"),
    "planner-selection": ("planner", "planner"),
    "planner-execution": ("planner", "planner"),
    "normalized-plan": ("planner", "planner"),
    "context-coordination": ("context-coordination", "context-coordination"),
    "mission-snapshot": ("context-coordination", "derived-snapshot"),
    "bayesian-belief": ("environment", "bayesian-belief-source"),
    "risk.observed": ("environment", "bayesian-belief-source"),
    "belief.constraints": ("environment", "bayesian-belief-source"),
    "belief.updated": ("environment", "bayesian-belief-source"),
    "statechart": ("fsm-runner", "declarative-statechart"),
    "fsm-status": ("fsm-runner", "fsm-status"),
    "fsm-execution-record": ("fsm-runner", "durable-fsm-state"),
    "maneuver-control": ("maneuver-control", "maneuver-control"),
    "maneuver-decision": ("maneuver-control", "maneuver-control"),
    "maneuver-adapter": ("maneuver-adapter", "maneuver-adapter"),
    "operational-scene-graph": ("environment", "environment"),
    "environment-to-fsm-feedback": ("environment", "environment"),
    "control-to-hyper-replan": ("hyper-agent", "hyper-agent"),
    "role-skills-advisory": ("advisory-context", "advisory-context"),
    "mission-memory-isolation": ("advisory-context", "mission-memory-isolation"),
    "human-question": ("advisory-context", "human-question"),
    "physical-action-catalogue": ("maneuver-control", "maneuver-control"),
    "non-physical-choice": ("maneuver-control", "maneuver-control"),
    "transport-fan-out": ("transport", "transport"),
    "redaction-evidence": ("runtime", "operational-log"),
}
_ENVELOPE_FIELDS = {"schema_version", "observation_sequence", "observed_at", "record"}
_COMMAND_PAYLOAD_FIELDS = {
    "action", "intent", "maneuver_id", "mission_snapshot_id", "normalized_plan",
    "parameters", "plan_revision", "planner_choice", "revision", "snapshot_id",
}
_OUTCOME_PAYLOAD_FIELDS = {
    "action", "command_id", "correlation_id", "event_id", "lifecycle", "maneuver_id",
    "operation", "plan_revision", "result", "revision", "snapshot_id", "status",
}


def _json_safe(value: object, path: str = "payload") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            frozen[key] = _json_safe(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_json_safe(item, f"{path}[]") for item in value)
    raise ValueError(f"{path} contains a non-JSON value")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def sanitize_payload(
    payload: Mapping[str, object] | None,
    *,
    redacted_fields: Iterable[str] = (),
) -> tuple[Mapping[str, object], tuple[str, ...]]:
    """Remove sensitive paths and redact credential-shaped public strings."""
    found = {item for item in redacted_fields if isinstance(item, str)}
    drop = object()

    def visit(value: object, path: str) -> object:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for raw_key, child in sorted(value.items(), key=lambda pair: str(pair[0])):
                key = str(raw_key)
                child_path = f"{path}.{key}" if path else key
                if not isinstance(raw_key, str) or _SENSITIVE_KEY.search(key):
                    found.add(child_path)
                    continue
                safe = visit(child, child_path)
                if safe is not drop:
                    result[key] = safe
            return result
        if isinstance(value, (list, tuple)):
            result_list: list[object] = []
            for index, child in enumerate(value):
                safe = visit(child, f"{path}[{index}]")
                if safe is not drop:
                    result_list.append(safe)
            return result_list
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            found.add(path)
            return drop
        if isinstance(value, str):
            redacted = _SECRET_VALUE.sub("[REDACTED]", value)
            if redacted != value:
                found.add(path)
            return redacted[:512]
        found.add(path)
        return drop

    safe_payload = visit(payload or {}, "")
    if not isinstance(safe_payload, dict):
        safe_payload = {}
    return cast(Mapping[str, object], _json_safe(safe_payload)), tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class TraceViewItem:
    """Immutable JSON-safe evidence suitable for a read-only mission viewer."""

    schema_version: int = 1
    trace_id: str = ""
    event_id: str = ""
    mission_id: str = ""
    sequence: int = 0
    occurred_at: str = ""
    component: str = ""
    authority: str = ""
    event_kind: str = ""
    status: str | None = None
    outcome: str | None = None
    correlation_id: str | None = None
    parent_id: str | None = None
    replay_disposition: ReplayDisposition = "normal"
    observation_sequence: int | None = None
    observed_at: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    redacted_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("unsupported trace view schema version")
        for value, label in (
            (self.trace_id, "trace ID"), (self.event_id, "event ID"),
            (self.mission_id, "mission ID"), (self.occurred_at, "occurred_at"),
            (self.component, "component"), (self.authority, "authority"),
            (self.event_kind, "event kind"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"trace {label} must be non-empty")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("trace sequence must be a non-negative integer")
        for value, label in (
            (self.status, "status"), (self.outcome, "outcome"),
            (self.correlation_id, "correlation ID"), (self.parent_id, "parent ID"),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"trace {label} must be a string or null")
        if self.replay_disposition not in _REPLAY_DISPOSITIONS:
            raise ValueError("trace replay disposition is invalid")
        if self.observation_sequence is not None and (
            isinstance(self.observation_sequence, bool)
            or not isinstance(self.observation_sequence, int)
            or self.observation_sequence < 1
        ):
            raise ValueError("trace observation sequence must be positive or null")
        if self.observed_at is not None and (not isinstance(self.observed_at, str) or not self.observed_at.strip()):
            raise ValueError("trace observed_at must be a non-empty string or null")
        if not isinstance(self.payload, Mapping):
            raise ValueError("trace payload must be a mapping")
        object.__setattr__(self, "payload", _json_safe(self.payload))
        object.__setattr__(self, "redacted_fields", tuple(sorted(set(self.redacted_fields))))
        object.__setattr__(self, "missing_fields", tuple(sorted(set(self.missing_fields))))
        for label, values in (("redacted_fields", self.redacted_fields), ("missing_fields", self.missing_fields)):
            if any(not isinstance(item, str) for item in values):
                raise ValueError(f"trace {label} must contain strings")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "trace_id": self.trace_id,
            "event_id": self.event_id, "mission_id": self.mission_id,
            "sequence": self.sequence, "occurred_at": self.occurred_at,
            "component": self.component, "authority": self.authority,
            "event_kind": self.event_kind, "status": self.status, "outcome": self.outcome,
            "correlation_id": self.correlation_id, "parent_id": self.parent_id,
            "replay_disposition": self.replay_disposition, "payload": _plain(self.payload),
            "observation_sequence": self.observation_sequence, "observed_at": self.observed_at,
            "redacted_fields": list(self.redacted_fields), "missing_fields": list(self.missing_fields),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "TraceViewItem":
        data = dict(value)
        if "event_id" not in data and "trace_id" in data:
            data["event_id"] = data["trace_id"]
        if "trace_id" not in data and "event_id" in data:
            data["trace_id"] = data["event_id"]
        return cls(**cast(Any, {key: data[key] for key in cls.__dataclass_fields__ if key in data}))


@dataclass(frozen=True, slots=True)
class _CanonicalRecord:
    item: TraceViewItem
    source_kind: str
    ordered: bool = False
    resync_sequence: int | None = None
    source_fingerprint: str = ""


class _RecordError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in _ERROR_CATEGORIES else "invalid_record"


def _digest(value: object) -> str:
    try:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def _error(
    code: str,
    *,
    evidence_key: object,
    disposition: ReplayDisposition = "malformed",
    category: str = "invalid_record",
) -> TraceViewItem:
    safe_code = code if code in _ERROR_CATEGORIES else "invalid_record"
    safe_category = category if category in _ERROR_CATEGORIES else "invalid_record"
    identifier = f"{disposition}:{_digest((safe_code, safe_category, evidence_key))}"
    body: dict[str, object] = {
        "error_code": safe_code,
        "message": _ERROR_MESSAGES[safe_code],
        "category": safe_category,
        "evidence_hash": _digest(evidence_key),
    }
    return TraceViewItem(
        trace_id=identifier, event_id=identifier, mission_id="unknown", sequence=0,
        occurred_at="unknown", component="viewer", authority="non-authoritative",
        event_kind="error", status="error", outcome="invalid",
        replay_disposition=disposition, payload=body, missing_fields=("source_record",),
    )


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _RecordError("invalid_record")
    return value


def _integer(raw: Mapping[str, object], key: str, *, minimum: int = 0) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _RecordError("invalid_record")
    return value


def _exact(raw: Mapping[str, object], expected: set[str], label: str) -> None:
    keys = set(raw)
    if keys != expected:
        missing = expected - keys
        unknown = keys - expected
        if missing or unknown:
            raise _RecordError("unknown_fields")


def _mapping(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise _RecordError("invalid_record")
    return cast(Mapping[str, object], value)


def _allow(payload: Mapping[str, object], fields: set[str]) -> dict[str, object]:
    return {key: payload[key] for key in sorted(fields & set(payload))}


def _safe_payload(
    payload: Mapping[str, object], fields: set[str]
) -> tuple[Mapping[str, object], tuple[str, ...]]:
    selected = _allow(payload, fields)
    isolation_marker = selected.pop("mission_memory_isolated", None)
    supplied = selected.get("redacted_fields", ())
    redactions = supplied if isinstance(supplied, (list, tuple)) else ()
    safe, found = sanitize_payload(selected, redacted_fields=cast(Iterable[str], redactions))
    if isinstance(isolation_marker, bool):
        safe = cast(Mapping[str, object], _json_safe({
            **cast(dict[str, object], _plain(safe)),
            "mission_memory_isolated": isolation_marker,
        }))
    return safe, found


class TraceProjection:
    """Adapt documented public v1 records into deterministic viewer evidence."""

    def project(
        self,
        records: Iterable[Mapping[str, object] | str] | Mapping[str, object] | str,
    ) -> tuple[TraceViewItem, ...]:
        if isinstance(records, Mapping) or isinstance(records, str):
            records = cast(Any, records.splitlines() if isinstance(records, str) and "\n" in records else (records,))

        canonical: list[_CanonicalRecord] = []
        failures: list[TraceViewItem] = []
        raw_source_kinds: set[str] = set()
        saw_envelope = False
        failure_counts: Counter[tuple[str, str]] = Counter()
        for raw_input in records:
            raw: object = raw_input
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    key = ("malformed_json", _digest(raw))
                    failure_counts[key] += 1
                    failures.append(_error(
                        "malformed_json",
                        evidence_key=(*key, failure_counts[key]),
                        category="malformed_json",
                    ))
                    continue
            if not isinstance(raw, Mapping):
                key = ("nonmapping", _digest(raw))
                failure_counts[key] += 1
                failures.append(_error(
                    "non_mapping",
                    evidence_key=(*key, failure_counts[key]),
                    category="non_mapping",
                ))
                continue
            raw = cast(Mapping[str, object], raw)
            try:
                if set(raw) == _ENVELOPE_FIELDS:
                    canonical.append(self._observation(raw))
                    saw_envelope = True
                else:
                    adapted = self._adapt(raw)
                    adapted = replace(
                        adapted,
                        source_fingerprint=self._source_fingerprint(adapted.item),
                    )
                    canonical.append(adapted)
                    raw_source_kinds.add(adapted.source_kind)
            except _RecordError as exc:
                key = (exc.code, _digest(raw))
                failure_counts[key] += 1
                failures.append(_error(
                    exc.code,
                    evidence_key=(*key, failure_counts[key]),
                    category=exc.code,
                ))

        if raw_source_kinds and (saw_envelope or len(raw_source_kinds) > 1):
            retained: list[_CanonicalRecord] = []
            for record in canonical:
                if record.item.observation_sequence is not None:
                    retained.append(record)
                    continue
                failures.append(_error(
                    "envelope_required",
                    evidence_key=(record.source_kind, _digest(record.item.to_dict())),
                    category="envelope_required",
                ))
            canonical = retained

        canonical.sort(key=lambda record: self._canonical_key(record.item))
        selected = self._resolve_identities(canonical)
        selected = self._mark_replay_and_stale(selected)
        gaps = self._gap_evidence(canonical)
        items = [record.item for record in selected] + failures + gaps
        return tuple(sorted(items, key=self._sort_key))

    def project_jsonl(self, text: str) -> tuple[TraceViewItem, ...]:
        return self.project(text)

    @staticmethod
    def _canonical_key(item: TraceViewItem) -> tuple[str, str]:
        return item.event_id, json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _source_fingerprint(item: TraceViewItem) -> str:
        source = item.to_dict()
        source.pop("observation_sequence", None)
        source.pop("observed_at", None)
        source.pop("replay_disposition", None)
        return _digest(source)

    @staticmethod
    def _sort_key(item: TraceViewItem) -> tuple[str, int, int, str, str]:
        if item.observation_sequence is not None:
            return item.mission_id, 0, item.observation_sequence, item.observed_at or "", item.event_id
        return item.mission_id, 1, item.sequence, item.occurred_at, item.event_id

    def _resolve_identities(self, records: list[_CanonicalRecord]) -> list[_CanonicalRecord]:
        grouped: dict[str, list[_CanonicalRecord]] = defaultdict(list)
        for record in records:
            grouped[record.item.event_id].append(record)
        selected: list[_CanonicalRecord] = []
        for event_id in sorted(grouped):
            variants: dict[str, list[_CanonicalRecord]] = defaultdict(list)
            for record in grouped[event_id]:
                fingerprint = record.source_fingerprint or self._source_fingerprint(record.item)
                variants[fingerprint].append(record)
            winner_key = min(variants)
            matching = sorted(variants[winner_key], key=lambda record: self._sort_key(record.item))
            winner = matching[0]
            selected.append(winner)
            for index, record in enumerate(matching[1:], 1):
                duplicate_id = f"duplicate:{event_id}:{index}"
                duplicate = replace(
                    record.item, trace_id=duplicate_id, event_id=duplicate_id,
                    replay_disposition="duplicate",
                    payload={**cast(dict[str, object], _plain(record.item.payload)), "source_event_id": event_id},
                )
                selected.append(replace(record, item=duplicate))
            for fingerprint in sorted(set(variants) - {winner_key}):
                conflict = _error(
                    "invalid_record",
                    evidence_key=(event_id, fingerprint), disposition="conflict",
                    category="invalid_record",
                )
                selected.append(_CanonicalRecord(conflict, winner.source_kind))
        return selected

    def _mark_replay_and_stale(self, records: list[_CanonicalRecord]) -> list[_CanonicalRecord]:
        resync_floor: dict[tuple[str, str], int] = {}
        for record in records:
            if record.resync_sequence is not None:
                stream_kind = "observation" if record.item.observation_sequence is not None else record.source_kind
                key = (record.item.mission_id, stream_kind)
                resync_floor[key] = max(resync_floor.get(key, 0), record.resync_sequence)

        streams: dict[tuple[str, str, int], list[int]] = defaultdict(list)
        marked = list(records)
        for index, record in enumerate(marked):
            item = record.item
            if record.resync_sequence is not None:
                marked[index] = replace(record, item=replace(item, replay_disposition="resynchronized"))
                continue
            stream_kind = "observation" if item.observation_sequence is not None else record.source_kind
            source_sequence = item.observation_sequence if item.observation_sequence is not None else item.sequence
            floor = resync_floor.get((item.mission_id, stream_kind))
            if (
                record.ordered and floor is not None and source_sequence < floor
                and item.replay_disposition == "normal"
            ):
                marked[index] = replace(record, item=replace(item, replay_disposition="stale"))
            if record.ordered and item.replay_disposition == "normal":
                streams[(item.mission_id, stream_kind, source_sequence)].append(index)
        for indexes in streams.values():
            if len(indexes) > 1:
                for index in sorted(indexes, key=lambda value: marked[value].item.event_id)[1:]:
                    item = marked[index].item
                    if item.replay_disposition == "normal":
                        marked[index] = replace(marked[index], item=replace(item, replay_disposition="replayed"))
        return marked

    def _gap_evidence(self, records: list[_CanonicalRecord]) -> list[TraceViewItem]:
        streams: dict[tuple[str, str], set[int]] = defaultdict(set)
        resync_floor: dict[tuple[str, str], int] = {}
        for record in records:
            if record.resync_sequence is not None:
                stream_kind = "observation" if record.item.observation_sequence is not None else record.source_kind
                key = (record.item.mission_id, stream_kind)
                resync_floor[key] = max(resync_floor.get(key, 1), record.resync_sequence)
            source_sequence = record.item.observation_sequence if record.item.observation_sequence is not None else record.item.sequence
            stream_kind = "observation" if record.item.observation_sequence is not None else record.source_kind
            if record.ordered and record.item.event_kind != "error" and source_sequence > 0:
                streams[(record.item.mission_id, stream_kind)].add(source_sequence)
        gaps: list[TraceViewItem] = []
        for (mission_id, source_kind), seen in sorted(streams.items()):
            floor = resync_floor.get((mission_id, source_kind), 1)
            relevant = {sequence for sequence in seen if sequence >= floor}
            if not relevant:
                continue
            missing = sorted(set(range(floor, max(relevant) + 1)) - relevant)
            if not missing:
                continue
            gaps.append(_error(
                "invalid_record",
                evidence_key=(mission_id, source_kind, missing), disposition="gap",
                category="invalid_record",
            ))
        return gaps

    def _observation(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _ENVELOPE_FIELDS, "PublicObservation")
        if raw.get("schema_version") != 1:
            raise _RecordError("unsupported_schema")
        observation_sequence = _integer(raw, "observation_sequence", minimum=1)
        observed_at = _text(raw, "observed_at")
        try:
            timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _RecordError("invalid_record") from exc
        if timestamp.tzinfo is None:
            raise _RecordError("invalid_record")
        adapted = self._adapt(_mapping(raw, "record"))
        return replace(
            adapted,
            item=replace(
                adapted.item,
                observation_sequence=observation_sequence,
                observed_at=observed_at,
            ),
            ordered=True,
            resync_sequence=(observation_sequence if adapted.resync_sequence is not None else None),
            source_fingerprint=self._source_fingerprint(adapted.item),
        )

    def _adapt(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        if raw.get("schema_version") != 1:
            raise _RecordError("unsupported_schema")
        keys = set(raw)
        if "record_id" in keys:
            return self._operational_log(raw)
        if "summary_id" in keys:
            return self._summary(raw)
        if "feedback_id" in keys:
            return self._maneuver_feedback(raw)
        if "request_id" in keys and "requester" in keys:
            return self._replan_request(raw)
        if "command_id" in keys and "command_kind" in keys:
            return self._command(raw)
        if "command_id" in keys and "target_service" in keys:
            return self._receipt(raw)
        if "command_id" in keys:
            return self._outcome(raw)
        if "event_id" in keys:
            return self._transport_event(raw)
        if "version" in keys or "source_references" in keys:
            return self._snapshot(raw)
        if "entry_state" in keys or "transitions" in keys and "states" in keys:
            return self._statechart(raw)
        if "record_revision" in keys or "active_configuration" in keys:
            return self._fsm_execution(raw)
        if "transition_candidates" in keys:
            return self._fsm_status(raw)
        raise _RecordError("unsupported_shape")

    def _base(
        self,
        source_kind: str,
        *,
        event_id: str,
        mission_id: str,
        sequence: int,
        occurred_at: str,
        event_kind: str,
        payload: Mapping[str, object],
        redactions: tuple[str, ...] = (),
        missing: tuple[str, ...] = (),
        status: str | None = None,
        outcome: str | None = None,
        correlation_id: str | None = None,
        parent_id: str | None = None,
        component: str | None = None,
    ) -> TraceViewItem:
        mapped_component, authority = _IDENTITY[source_kind]
        return TraceViewItem(
            trace_id=event_id, event_id=event_id, mission_id=mission_id, sequence=sequence,
            occurred_at=occurred_at, component=component or mapped_component, authority=authority,
            event_kind=event_kind, status=status, outcome=outcome,
            correlation_id=correlation_id, parent_id=parent_id, payload=payload,
            redacted_fields=redactions, missing_fields=missing,
        )

    def _transport_event(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _TRANSPORT_FIELDS, "TransportEvent")
        from onr.contracts.transport import TransportEvent

        try:
            TransportEvent.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise _RecordError("invalid_record") from exc
        event_id = _text(raw, "event_id")
        mission_id = _text(raw, "mission_id")
        sequence = _integer(raw, "sequence")
        event_kind = _text(raw, "event_kind")
        source_payload = _mapping(raw, "payload")
        payload, redactions = _safe_payload(source_payload, _COMMON_EVENT_PAYLOAD_FIELDS)
        status = payload.get("status") if isinstance(payload.get("status"), str) else None
        outcome = payload.get("outcome") if isinstance(payload.get("outcome"), str) else None
        correlation = payload.get("correlation_id") if isinstance(payload.get("correlation_id"), str) else None
        supplied_missing = payload.get("missing_fields", ())
        missing = tuple(item for item in supplied_missing if isinstance(item, str)) if isinstance(supplied_missing, tuple) else ()
        resync: int | None = None
        if event_kind in {"resync", "resynchronized", "stream-resynchronized"}:
            candidate = payload.get("resume_sequence", sequence)
            if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
                raise _RecordError("invalid_record")
            resync = candidate
        item = self._base(
            "transport_event", event_id=event_id, mission_id=mission_id, sequence=sequence,
            occurred_at="unknown", event_kind=event_kind, payload=payload,
            redactions=redactions, missing=missing, status=cast(str | None, status),
            outcome=cast(str | None, outcome), correlation_id=cast(str | None, correlation),
        )
        component, authority = _TRANSPORT_IDENTITIES.get(event_kind, _IDENTITY["transport_event"])
        item = replace(item, component=component, authority=authority)
        snapshot_version = source_payload.get("version")
        if (
            event_kind == "mission-snapshot"
            and isinstance(snapshot_version, int)
            and not isinstance(snapshot_version, bool)
            and event_id == f"mission-snapshot:{mission_id}:{snapshot_version}"
        ):
            item = replace(
                item,
                trace_id=f"transport:{event_id}",
                event_id=f"transport:{event_id}",
            )
        if event_kind in {"stale", "late-record", "stale-delivery"}:
            item = replace(item, replay_disposition="stale")
        elif event_kind in {"replayed", "replayed-delivery"}:
            item = replace(item, replay_disposition="replayed")
        return _CanonicalRecord(item, "transport_event", True, resync)

    def _command(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _COMMAND_FIELDS, "Command")
        from onr.contracts.transport import Command

        try:
            Command.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise _RecordError("invalid_record") from exc
        command_id = _text(raw, "command_id")
        correlation = _text(raw, "correlation_id")
        mission_id = _text(raw, "mission_id")
        target = _text(raw, "target_service")
        kind = _text(raw, "command_kind")
        payload, redactions = _safe_payload(_mapping(raw, "payload"), _COMMAND_PAYLOAD_FIELDS)
        public = {"target_service": target, "command_kind": kind, **cast(dict[str, object], _plain(payload))}
        event_id = f"command:{command_id}"
        item = self._base(
            "command", event_id=event_id, mission_id=mission_id, sequence=0,
            occurred_at="unknown", event_kind="command", payload=public,
            redactions=redactions, correlation_id=correlation,
        )
        return _CanonicalRecord(item, "command")

    def _receipt(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _RECEIPT_FIELDS, "CommandReceipt")
        from onr.contracts.transport import CommandReceipt

        try:
            CommandReceipt.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise _RecordError("invalid_record") from exc
        command_id = _text(raw, "command_id")
        correlation = _text(raw, "correlation_id")
        mission_id = _text(raw, "mission_id")
        target = _text(raw, "target_service")
        status = _text(raw, "status")
        if status != "accepted":
            raise _RecordError("invalid_record")
        item = self._base(
            "command_receipt", event_id=f"receipt:{command_id}", mission_id=mission_id,
            sequence=0, occurred_at="unknown", event_kind="command-receipt", status=status,
            outcome="accepted", correlation_id=correlation, parent_id=f"command:{command_id}",
            payload={"command_id": command_id, "target_service": target},
        )
        return _CanonicalRecord(item, "command_receipt")

    def _outcome(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _OUTCOME_FIELDS, "CommandOutcome")
        from onr.contracts.transport import CommandOutcome

        try:
            CommandOutcome.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise _RecordError("invalid_record") from exc
        command_id = _text(raw, "command_id")
        correlation = _text(raw, "correlation_id")
        mission_id = _text(raw, "mission_id")
        status = _text(raw, "status")
        if status not in {"accepted", "completed", "failed"}:
            raise _RecordError("invalid_record")
        payload, redactions = _safe_payload(_mapping(raw, "payload"), _OUTCOME_PAYLOAD_FIELDS)
        item = self._base(
            "command_outcome", event_id=f"outcome:{command_id}", mission_id=mission_id,
            sequence=0, occurred_at="unknown", event_kind="command-outcome", status=status,
            outcome=status, correlation_id=correlation, parent_id=f"command:{command_id}",
            payload=payload, redactions=redactions,
        )
        return _CanonicalRecord(item, "command_outcome")

    def _operational_log(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _LOG_FIELDS, "OperationalLogRecord")
        from onr.ports.operational_log import OperationalLogRecord

        try:
            OperationalLogRecord.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise _RecordError("invalid_record") from exc
        event_id = _text(raw, "record_id")
        mission_id = _text(raw, "mission_id")
        sequence = _integer(raw, "sequence", minimum=1)
        occurred = _text(raw, "event_time")
        source = _text(raw, "source")
        event_kind = _text(raw, "event_kind")
        outcome = _text(raw, "outcome")
        payload, redactions = _safe_payload(_mapping(raw, "details"), _LOG_DETAIL_FIELDS)
        missing = ("summary",) if event_kind in {"summary-unavailable", "summary-missing"} else ()
        status = payload.get("status") if isinstance(payload.get("status"), str) else None
        correlation = payload.get("correlation_id") if isinstance(payload.get("correlation_id"), str) else None
        item = self._base(
            "operational_log", event_id=event_id, mission_id=mission_id, sequence=sequence,
            occurred_at=occurred, event_kind=event_kind, payload=payload,
            redactions=redactions, missing=missing, status=cast(str | None, status),
            outcome=outcome, correlation_id=cast(str | None, correlation),
            component=_LOG_COMPONENTS.get(source, "runtime"),
        )
        return _CanonicalRecord(item, "operational_log", True)

    def _summary(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _SUMMARY_FIELDS, "SummaryArtifact")
        from onr.ports.mission_log_summarizer import SummaryArtifact

        try:
            SummaryArtifact.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise _RecordError("invalid_record") from exc
        event_id = _text(raw, "summary_id")
        mission_id = _text(raw, "mission_id")
        sequence = _integer(raw, "sequence", minimum=1)
        occurred = _text(raw, "created_at")
        start = _integer(raw, "input_start_sequence", minimum=1)
        end = _integer(raw, "input_end_sequence", minimum=start)
        summary = _text(raw, "summary")
        prior = raw.get("prior_summary_ids")
        if not isinstance(prior, (list, tuple)) or any(not isinstance(item, str) or not item for item in prior):
            raise _RecordError("invalid_record")
        payload, redactions = sanitize_payload({
            "summary": summary, "input_start_sequence": start, "input_end_sequence": end,
            "prior_summary_ids": list(prior),
        })
        item = self._base(
            "summary", event_id=event_id, mission_id=mission_id, sequence=sequence,
            occurred_at=occurred, event_kind="summary", outcome="available",
            payload=payload, redactions=redactions,
        )
        return _CanonicalRecord(item, "summary", True)

    def _maneuver_feedback(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _MANEUVER_FEEDBACK_FIELDS, "PublicManeuverFeedback")
        from onr.contracts.fsm import ManeuverFeedback

        feedback_id = _text(raw, "feedback_id")
        mission_id = _text(raw, "mission_id")
        maneuver_id = _text(raw, "maneuver_id")
        lifecycle = _text(raw, "lifecycle")
        source_sequence = _integer(raw, "source_sequence")
        source = _text(raw, "source")
        if source not in {"environment", "maneuver-control"}:
            raise _RecordError("invalid_record")
        optional_text: dict[str, str] = {}
        for key in ("command_id", "correlation_id", "parent_id", "snapshot_id"):
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise _RecordError("invalid_record")
                optional_text[key] = value
        plan_revision = raw.get("plan_revision")
        if plan_revision is not None and (
            isinstance(plan_revision, bool)
            or not isinstance(plan_revision, int)
            or plan_revision < 0
        ):
            raise _RecordError("invalid_record")
        try:
            ManeuverFeedback.from_dict({
                "schema_version": 1,
                "feedback_id": feedback_id,
                "mission_id": mission_id,
                "maneuver_id": maneuver_id,
                "lifecycle": lifecycle,
                "payload": {
                    **optional_text,
                    "source": source,
                    **({"plan_revision": plan_revision} if plan_revision is not None else {}),
                },
            })
        except (TypeError, ValueError) as exc:
            raise _RecordError("invalid_record") from exc
        public_payload = {
            "feedback_id": feedback_id,
            "maneuver_id": maneuver_id,
            "lifecycle": lifecycle,
            "source": source,
            **({key: value for key, value in optional_text.items() if key in {"command_id", "snapshot_id"}}),
            **({"plan_revision": plan_revision} if plan_revision is not None else {}),
        }
        payload, redactions = sanitize_payload(public_payload)
        correlation, _ = sanitize_payload({"value": optional_text.get("correlation_id")})
        parent, _ = sanitize_payload({"value": optional_text.get("parent_id")})
        item = self._base(
            "maneuver_feedback",
            event_id=f"feedback:{feedback_id}",
            mission_id=mission_id,
            sequence=source_sequence,
            occurred_at="unknown",
            event_kind="maneuver-feedback",
            status=lifecycle,
            outcome=lifecycle,
            correlation_id=cast(str | None, correlation.get("value")),
            parent_id=cast(str | None, parent.get("value")),
            payload=payload,
            redactions=redactions,
            component="maneuver-control" if source == "maneuver-control" else "environment",
        )
        if source == "maneuver-control":
            item = replace(item, authority="maneuver-control-feedback")
        return _CanonicalRecord(item, "maneuver_feedback", True)

    def _replan_request(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _REPLAN_REQUEST_FIELDS, "PublicReplanRequest")
        from onr.contracts.hyper_agent import ReplanRequest

        request_id = _text(raw, "request_id")
        mission_id = _text(raw, "mission_id")
        reason = _text(raw, "reason")
        requester = _text(raw, "requester")
        revision = _integer(raw, "observed_plan_revision")
        source_sequence = _integer(raw, "source_sequence")
        source_revisions = raw.get("source_revisions")
        if not isinstance(source_revisions, Mapping):
            raise _RecordError("invalid_record")
        try:
            request = ReplanRequest.from_dict({
                "request_id": request_id,
                "mission_id": mission_id,
                "reason": reason,
                "requester": requester,
                "observed_plan_revision": revision,
                "source_revisions": source_revisions,
                "coalesced_request_ids": [],
                "coalesced_reasons": [],
            })
        except (TypeError, ValueError) as exc:
            raise _RecordError("invalid_record") from exc
        optional_text: dict[str, str] = {}
        for key in ("correlation_id", "parent_id", "status", "snapshot_id"):
            value = raw.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise _RecordError("invalid_record")
                optional_text[key] = value
        if optional_text.get("status", "requested") not in {
            "requested", "pending", "coalesced"
        }:
            raise _RecordError("invalid_record")
        payload, redactions = sanitize_payload({
            "request_id": request.request_id,
            "reason": request.reason,
            "requester": request.requester,
            "observed_plan_revision": request.observed_plan_revision,
            "source_revisions": dict(request.source_revisions),
            **({"snapshot_id": optional_text["snapshot_id"]} if "snapshot_id" in optional_text else {}),
        })
        correlation, _ = sanitize_payload({"value": optional_text.get("correlation_id")})
        parent, _ = sanitize_payload({"value": optional_text.get("parent_id")})
        status = optional_text.get("status", "requested")
        item = self._base(
            "replan_request",
            event_id=f"replan-request:{request_id}",
            mission_id=mission_id,
            sequence=source_sequence,
            occurred_at="unknown",
            event_kind="replan-request",
            status=status,
            correlation_id=cast(str | None, correlation.get("value")),
            parent_id=cast(str | None, parent.get("value")),
            payload=payload,
            redactions=redactions,
        )
        return _CanonicalRecord(item, "replan_request", True)

    def _snapshot(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        expected = (
            _SNAPSHOT_FIELDS
            if "source_hashes" in raw
            else _SNAPSHOT_FIELDS - {"source_hashes"}
        )
        _exact(raw, expected, "MissionSnapshot")
        from onr.contracts.context_coordination import MissionSnapshot

        try:
            MissionSnapshot.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise _RecordError("invalid_record") from exc
        mission_id = _text(raw, "mission_id")
        version = _integer(raw, "version", minimum=1)
        occurred = _text(raw, "created_at")
        missing_sources = raw.get("missing_sources")
        if not isinstance(missing_sources, (list, tuple)) or any(not isinstance(item, str) for item in missing_sources):
            raise _RecordError("invalid_record")
        fields = expected - {"schema_version", "mission_id", "version", "created_at", "missing_sources"}
        payload, redactions = _safe_payload(raw, fields)
        item = self._base(
            "mission_snapshot", event_id=f"mission-snapshot:{mission_id}:{version}",
            mission_id=mission_id, sequence=version, occurred_at=occurred,
            event_kind="mission-snapshot", payload=payload, redactions=redactions,
            missing=tuple(f"source:{item}" for item in missing_sources),
        )
        return _CanonicalRecord(item, "mission_snapshot")

    def _statechart(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        expected = _STATECHART_FIELDS if "timers" in raw else (_STATECHART_FIELDS - {"timers"}) | {"deadlines"}
        _exact(raw, expected, "Statechart")
        from onr.contracts.fsm import Statechart

        try:
            Statechart.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise _RecordError("invalid_record") from exc
        mission_id = _text(raw, "mission_id")
        revision = _integer(raw, "plan_revision")
        if raw.get("trusted") is not False:
            raise _RecordError("invalid_record")
        fields = expected - {"schema_version", "mission_id", "plan_revision"}
        payload, redactions = _safe_payload(raw, fields)
        item = self._base(
            "statechart", event_id=f"statechart:{mission_id}:{revision}", mission_id=mission_id,
            sequence=revision, occurred_at="unknown", event_kind="statechart",
            payload=payload, redactions=redactions,
        )
        return _CanonicalRecord(item, "statechart")

    def _fsm_status(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _FSM_STATUS_FIELDS, "FSMStatus")
        from onr.contracts.fsm import FSMStatus

        try:
            FSMStatus.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise _RecordError("invalid_record") from exc
        mission_id = _text(raw, "mission_id")
        revision = _integer(raw, "statechart_revision")
        status = _text(raw, "status")
        fields = _FSM_STATUS_FIELDS - {"schema_version", "mission_id", "statechart_revision", "status"}
        payload, redactions = _safe_payload(raw, fields)
        item = self._base(
            "fsm_status", event_id=f"fsm-status:{mission_id}:{revision}:{_digest(raw)}",
            mission_id=mission_id, sequence=revision, occurred_at="unknown", event_kind="fsm-status",
            status=status, payload=payload, redactions=redactions,
        )
        return _CanonicalRecord(item, "fsm_status")

    def _fsm_execution(self, raw: Mapping[str, object]) -> _CanonicalRecord:
        _exact(raw, _FSM_EXECUTION_FIELDS, "FSMExecutionRecord")
        from onr.contracts.fsm import FSMExecutionRecord

        try:
            FSMExecutionRecord.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise _RecordError("invalid_record") from exc
        mission_id = _text(raw, "mission_id")
        revision = _integer(raw, "record_revision", minimum=1)
        fields = _FSM_EXECUTION_FIELDS - {"schema_version", "mission_id", "record_revision"}
        payload, redactions = _safe_payload(raw, fields)
        item = self._base(
            "fsm_execution", event_id=f"fsm-execution:{mission_id}:{revision}",
            mission_id=mission_id, sequence=revision, occurred_at="unknown",
            event_kind="fsm-execution-record", payload=payload, redactions=redactions,
        )
        return _CanonicalRecord(item, "fsm_execution")


__all__ = ["ReplayDisposition", "TraceProjection", "TraceViewItem", "sanitize_payload"]
