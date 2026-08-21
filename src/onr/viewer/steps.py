"""Pure projection of local run artifacts into viewer steps and overview data."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, cast

STEPS_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1
TEXT_FIELD_LIMIT = 200_000
MISSION_TEXT_LIMIT = 20_000
MISSION_CONTENT_WARNING = (
    "Mission content is unavailable from operational logs, planning-intent debug "
    "artifacts, and mission snapshots."
)
PHASES = (
    "planning-intent",
    "planning-context",
    "planner-assets",
    "planner-execution",
    "statechart-generation",
    "maneuver-handoff",
    "heartbeat",
)

StepKind = Literal["llm", "tool", "decision", "feedback"]
StepStatus = Literal["ok", "error", "unknown"]
RunStatus = Literal["complete", "running", "unknown"]

_SUCCESS_OUTCOMES = {
    "accepted",
    "complete",
    "completed",
    "ok",
    "ready",
    "success",
    "succeeded",
}
_ERROR_OUTCOMES = {"error", "failed", "failure", "rejected"}
_IDENTIFIER_KEYS = {
    "command_id",
    "correlation_id",
    "decision_id",
    "event_id",
    "invocation_id",
    "parent_id",
    "record_id",
    "request_id",
    "response_id",
}
_PRIVATE_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "invocation_params",
    "memory",
    "messages",
    "mission_memory",
    "password",
    "prompt",
    "secret",
    "token",
}
_MISSION_FIELD_ALIASES = {
    "title": ("title", "mission_title"),
    "objective": ("objective", "mission_objective"),
    "description": ("description", "mission_description"),
    "constraints": ("constraints", "mission_constraints"),
    "sector": ("sector", "mission_sector"),
    "issued_at": ("issued_at", "mission_issued_at"),
    "raw": ("raw", "raw_mission", "mission_text", "operator_intent"),
    "source_authority": ("source_authority",),
    "mission_pattern": ("mission_pattern",),
    "capture_rule": ("capture_rule",),
    "value_rule": ("value_rule",),
    "source_roles": ("source_roles",),
}
_MISSION_SCOPE_KEYS = (
    "mission",
    "mission_input",
    "planning_intent",
    "intent",
    "payload",
    "details",
)
_ATTEMPT = re.compile(r"(?:attempts?|workspace)[-_/ ]*0*(\d+)", re.IGNORECASE)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _frozen(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _frozen(item)
                for key, item in value.items()
                if isinstance(key, str)
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    return str(value)


def _public_value(value: object) -> object:
    """Copy arbitrary JSON while omitting private prompt/config namespaces."""

    if isinstance(value, Mapping):
        return {
            str(key): _public_value(item)
            for key, item in value.items()
            if isinstance(key, str) and key.lower().replace("-", "_") not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    copied = _public_value(value)
    return cast(dict[str, object], copied) if isinstance(copied, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object, default: int = 0) -> int:
    return value if type(value) is int and value >= 0 else default


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _iso(value: object) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def _generated_at(value: str | None) -> str:
    parsed = _timestamp(value)
    if parsed is not None:
        return parsed.isoformat()
    return datetime.now(UTC).isoformat()


def _duration_ms(started_at: str | None, finished_at: str | None) -> int | None:
    started = _timestamp(started_at)
    finished = _timestamp(finished_at)
    if started is None or finished is None or finished < started:
        return None
    return round((finished - started).total_seconds() * 1000)


def _limited_text(value: object) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    if len(value) <= TEXT_FIELD_LIMIT:
        return value, False
    return value[:TEXT_FIELD_LIMIT], True


def _bounded_mission_value(
    value: object, *, depth: int = 0
) -> tuple[object | None, bool]:
    if isinstance(value, str):
        if not value.strip():
            return None, False
        if len(value) <= MISSION_TEXT_LIMIT:
            return value, False
        return value[:MISSION_TEXT_LIMIT], True
    if value is None or isinstance(value, (bool, int)):
        return value, False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, True)
    if depth >= 4:
        return None, True
    if isinstance(value, (list, tuple)):
        list_result: list[object] = []
        truncated = len(value) > 50
        for item in value[:50]:
            selected, clipped = _bounded_mission_value(item, depth=depth + 1)
            truncated |= clipped
            if selected is not None:
                list_result.append(selected)
        return (list_result if list_result else None), truncated
    if isinstance(value, Mapping):
        mapping_result: dict[str, object] = {}
        items = [(key, item) for key, item in value.items() if isinstance(key, str)]
        truncated = len(items) > 50
        for key, item in items[:50]:
            selected, clipped = _bounded_mission_value(item, depth=depth + 1)
            truncated |= clipped
            if selected is not None:
                mapping_result[key] = selected
        return (mapping_result if mapping_result else None), truncated
    return None, True


def _mission_fields(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    scopes: list[Mapping[str, object]] = [value]
    for scope_key in _MISSION_SCOPE_KEYS:
        nested = value.get(scope_key)
        if isinstance(nested, Mapping):
            scopes.append(nested)
    result: dict[str, object] = {}
    truncated = False
    for output_field, aliases in _MISSION_FIELD_ALIASES.items():
        for scope in scopes:
            raw = next((scope[alias] for alias in aliases if alias in scope), None)
            if raw is None:
                continue
            selected, clipped = _bounded_mission_value(raw)
            truncated |= clipped
            if selected is not None:
                result[output_field] = selected
                break
    if not result:
        return None
    if truncated:
        result["truncated"] = True
    return result


def _reasoning(record: Mapping[str, object]) -> tuple[str | None, bool]:
    parts: list[str] = []
    truncated = False
    for key in ("reasoning", "reasoning_content"):
        text, clipped = _limited_text(record.get(key))
        truncated |= clipped
        if text and text not in parts:
            parts.append(text)
    details = record.get("reasoning_details")
    if details is not None:
        try:
            rendered = json.dumps(
                _public_value(details),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError, RecursionError):
            rendered = str(details)
        rendered, clipped = _limited_text(rendered)
        truncated |= clipped
        if rendered and rendered not in parts:
            parts.append(rendered)
    if not parts:
        return None, truncated
    combined, clipped = _limited_text("\n\n".join(parts))
    return combined, truncated or clipped


def _status(outcome: object, *, error: object = None, finished: bool = False) -> StepStatus:
    if error not in (None, "", False):
        return "error"
    selected = outcome.lower() if isinstance(outcome, str) else ""
    if selected in _ERROR_OUTCOMES:
        return "error"
    if selected in _SUCCESS_OUTCOMES or finished:
        return "ok"
    return "unknown"


def _identifiers(value: object) -> set[str]:
    found: set[str] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if key in _IDENTIFIER_KEYS and isinstance(item, str) and item:
                    found.add(item)
                elif isinstance(item, (Mapping, list, tuple)):
                    pending.append(item)
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return found


def _phase_for_name(name: str) -> str:
    selected = name.lower().replace("_", "-")
    if any(token in selected for token in ("intent", "planner-choice")):
        return "planning-intent"
    if any(token in selected for token in ("context", "snapshot", "evidence")):
        return "planning-context"
    if any(
        token in selected
        for token in (
            "artifact",
            "problem",
            "model",
            "data",
            "minizinc-file",
        )
    ):
        return "planner-assets"
    if any(token in selected for token in ("planner", "solve", "minizinc")):
        return "planner-execution"
    if any(token in selected for token in ("statechart", "state-chart", "fsm")):
        return "statechart-generation"
    if any(token in selected for token in ("handoff", "maneuver", "execute")):
        return "maneuver-handoff"
    return "heartbeat"


def _phase_for_event(event_kind: str, fallback: str = "heartbeat") -> str:
    selected = event_kind.lower().replace("_", "-")
    if selected in PHASES:
        return selected
    aliases = {
        "planner-choice": "planning-intent",
        "planner-assets": "planner-assets",
        "planning": "planner-execution",
        "statechart": "statechart-generation",
        "fsm": "statechart-generation",
        "control": "maneuver-handoff",
        "transport": "maneuver-handoff",
        "environment": "heartbeat",
    }
    return aliases.get(selected, fallback)


def _attempt_number(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str):
        match = _ATTEMPT.search(value)
        if match is not None:
            return int(match.group(1))
        if value.isdigit() and int(value) > 0:
            return int(value)
    if isinstance(value, Mapping):
        for key in ("attempt", "attempt_number", "planner_attempt", "statechart_attempt"):
            attempt = _attempt_number(value.get(key))
            if attempt is not None:
                return attempt
        for key in ("details", "payload"):
            attempt = _attempt_number(value.get(key))
            if attempt is not None:
                return attempt
    return None


def _title(phase: str, name: str, decision: Mapping[str, object] | None = None) -> str:
    attempt = _attempt_number(decision or {}) or _attempt_number(name)
    base = phase.replace("-", " ").capitalize()
    return f"{base} (attempt {attempt})" if attempt is not None else base


def _component_for_event(record: Mapping[str, object]) -> str:
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        for key in ("source", "component", "target_service"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    kind = _text(record.get("event_kind")) or "transport"
    if kind.startswith("maneuver-"):
        return "maneuver-control"
    if kind.startswith("fsm-") or kind == "statechart":
        return "fsm-runner"
    if kind.startswith("environment-"):
        return "environment"
    return "transport"


def _normal_tool_call(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    function = value.get("function")
    source = function if isinstance(function, Mapping) else value
    name = source.get("name")
    if not isinstance(name, str) or not name.strip():
        name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "tool"
    raw_args = source.get("arguments", source.get("args", value.get("args", {})))
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError, RecursionError):
            parsed = {"value": raw_args}
        raw_args = parsed
    args = _public_value(raw_args)
    if not isinstance(args, Mapping):
        args = {"value": args}
    duration = value.get("duration_ms")
    return {
        "name": name,
        "args": dict(args),
        "result": _public_value(value.get("result")),
        "error": _public_value(value.get("error")),
        "duration_ms": duration if type(duration) is int and duration >= 0 else None,
    }


def _tool_calls(record: Mapping[str, object]) -> list[dict[str, object]]:
    raw = record.get("tool_calls")
    if not isinstance(raw, (list, tuple)):
        return []
    return [call for item in raw if (call := _normal_tool_call(item)) is not None]


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    args: Mapping[str, object] = field(default_factory=dict)
    result: object = None
    error: object = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", _frozen(self.args))
        object.__setattr__(self, "result", _frozen(self.result))
        object.__setattr__(self, "error", _frozen(self.error))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "args": _plain(self.args),
            "result": _plain(self.result),
            "error": _plain(self.error),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class Feedback:
    kind: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _frozen(self.payload))

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "payload": _plain(self.payload)}


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    kind: str
    ref: str
    label: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ArtifactReference | None:
        kind = value.get("kind")
        ref = value.get("ref")
        label = value.get("label")
        if not all(isinstance(item, str) and item for item in (kind, ref, label)):
            return None
        return cls(cast(str, kind), cast(str, ref), cast(str, label))

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "label": self.label}


@dataclass(frozen=True, slots=True)
class Step:
    step_id: str
    seq: int
    component: str
    role: str
    phase: str
    kind: StepKind
    name: str
    title: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    status: StepStatus = "unknown"
    outcome: str | None = None
    reasoning: str | None = None
    content: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    decision: Mapping[str, object] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    feedback: tuple[Feedback, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    children: tuple[Step, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.decision is not None:
            object.__setattr__(self, "decision", _frozen(self.decision))
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "feedback", tuple(self.feedback))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "children", tuple(self.children))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "step_id": self.step_id,
            "seq": self.seq,
            "component": self.component,
            "role": self.role,
            "phase": self.phase,
            "kind": self.kind,
            "name": self.name,
            "title": self.title,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "outcome": self.outcome,
            "reasoning": self.reasoning,
            "content": self.content,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "decision": _plain(self.decision),
            "tool_calls": [item.to_dict() for item in self.tool_calls],
            "feedback": [item.to_dict() for item in self.feedback],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "children": [item.to_dict() for item in self.children],
        }
        if self.truncated:
            result["truncated"] = True
        return result


@dataclass(frozen=True, slots=True)
class StepsView:
    mission_id: str
    generated_at: str
    warnings: tuple[str, ...]
    steps: tuple[Step, ...]
    schema_version: int = STEPS_SCHEMA_VERSION
    phases: tuple[str, ...] = PHASES

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "generated_at": self.generated_at,
            "warnings": list(self.warnings),
            "phases": list(self.phases),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class RunOverview:
    mission_id: str
    generated_at: str
    warnings: tuple[str, ...]
    mission: Mapping[str, object] | None
    status: RunStatus
    aggregates: Mapping[str, object]
    components: tuple[str, ...]
    summaries: tuple[Mapping[str, object], ...]
    fsm: Mapping[str, object] | None
    environment: Mapping[str, object] | None
    artifacts_index: tuple[ArtifactReference, ...]
    final_result: object = None
    schema_version: int = RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.mission is not None:
            object.__setattr__(self, "mission", _frozen(self.mission))
        object.__setattr__(self, "aggregates", _frozen(self.aggregates))
        object.__setattr__(self, "summaries", tuple(_frozen(item) for item in self.summaries))
        if self.fsm is not None:
            object.__setattr__(self, "fsm", _frozen(self.fsm))
        if self.environment is not None:
            object.__setattr__(self, "environment", _frozen(self.environment))
        object.__setattr__(self, "artifacts_index", tuple(self.artifacts_index))
        object.__setattr__(self, "final_result", _frozen(self.final_result))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "generated_at": self.generated_at,
            "warnings": list(self.warnings),
            "mission": _plain(self.mission),
            "status": self.status,
            "aggregates": _plain(self.aggregates),
            "components": list(self.components),
            "summaries": [_plain(item) for item in self.summaries],
            "fsm": _plain(self.fsm),
            "environment": _plain(self.environment),
            "artifacts_index": [item.to_dict() for item in self.artifacts_index],
            "final_result": _plain(self.final_result),
        }


def _draft_from_invocation(record: Mapping[str, object]) -> dict[str, object] | None:
    role = _text(record.get("role")) or _text(record.get("agent_role"))
    kind = record.get("kind")
    if role is None or kind not in {"llm", "tool"}:
        return None
    seq = _integer(record.get("sequence"))
    name = _text(record.get("name")) or cast(str, kind)
    started_at = _iso(record.get("started_at"))
    finished_at = _iso(record.get("finished_at"))
    error = record.get("error")
    draft: dict[str, object] = {
        "step_id": f"{role}:{seq}",
        "seq": seq,
        "component": role,
        "role": role,
        "phase": _phase_for_name(name),
        "kind": kind,
        "name": name,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": _duration_ms(started_at, finished_at),
        "status": _status(None, error=error, finished=finished_at is not None),
        "outcome": "failed" if error not in (None, "", False) else None,
        "reasoning": None,
        "content": None,
        "model": None,
        "finish_reason": None,
        "decision": None,
        "tool_calls": [],
        "feedback": [],
        "artifacts": [],
        "children": [],
        "truncated": False,
        "_ids": _identifiers(record),
        "_invocation_id": _text(record.get("invocation_id")),
        "_parent_id": _text(record.get("parent_id")),
        "_input": record.get("input"),
        "_output": record.get("output"),
        "_error": error,
    }
    if kind == "tool":
        draft["tool_calls"] = [
            {
                "name": name,
                "args": _mapping(record.get("input")),
                "result": _public_value(record.get("output")),
                "error": _public_value(error),
                "duration_ms": draft["duration_ms"],
            }
        ]
    return draft


def _apply_llm(draft: dict[str, object], record: Mapping[str, object]) -> None:
    reasoning, reasoning_truncated = _reasoning(record)
    content, content_truncated = _limited_text(record.get("content"))
    draft["reasoning"] = reasoning
    draft["content"] = content
    draft["model"] = _text(record.get("model"))
    draft["finish_reason"] = _text(record.get("finish_reason"))
    draft["tool_calls"] = _tool_calls(record)
    draft["truncated"] = reasoning_truncated or content_truncated
    status_code = record.get("status_code")
    if type(status_code) is int and status_code >= 400:
        draft["status"] = "error"
        draft["outcome"] = "failed"
    elif type(status_code) is int and 200 <= status_code < 300:
        draft["status"] = "ok"
    draft["_ids"] = cast(set[str], draft["_ids"]) | _identifiers(
        {key: record.get(key) for key in _IDENTIFIER_KEYS}
    )


def _draft_from_llm(record: Mapping[str, object]) -> dict[str, object] | None:
    role = _text(record.get("role")) or _text(record.get("agent_role"))
    if role is None:
        return None
    seq = _integer(record.get("sequence"))
    name = _text(record.get("name")) or _text(record.get("model")) or "llm"
    started_at = _iso(record.get("started_at"))
    finished_at = _iso(record.get("finished_at"))
    draft: dict[str, object] = {
        "step_id": f"{role}:{seq}",
        "seq": seq,
        "component": role,
        "role": role,
        "phase": _phase_for_name(name),
        "kind": "llm",
        "name": name,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": _duration_ms(started_at, finished_at),
        "status": _status(
            "completed" if record.get("status_code") == 200 else None,
            finished=finished_at is not None,
        ),
        "outcome": None,
        "reasoning": None,
        "content": None,
        "model": None,
        "finish_reason": None,
        "decision": None,
        "tool_calls": [],
        "feedback": [],
        "artifacts": [],
        "children": [],
        "truncated": False,
        "_ids": _identifiers(record),
        "_invocation_id": None,
        "_parent_id": None,
    }
    _apply_llm(draft, record)
    return draft


def _pair_llm_records(
    drafts: list[dict[str, object]],
    records: list[Mapping[str, object]],
    warnings: list[str],
) -> list[Mapping[str, object]]:
    unmatched = list(records)
    if not drafts or not records:
        return unmatched
    sequences = [_integer(record.get("sequence")) for record in records]
    dense = sequences == list(range(1, len(sequences) + 1))
    if dense:
        for draft, record in zip(drafts, records, strict=False):
            _apply_llm(draft, record)
            unmatched.remove(record)
        return unmatched

    by_sequence = {_integer(record.get("sequence")): record for record in records}
    for draft in drafts:
        record = by_sequence.get(cast(int, draft["seq"]))
        if record is not None and record in unmatched:
            _apply_llm(draft, record)
            unmatched.remove(record)

    for record in list(unmatched):
        llm_time = _timestamp(record.get("started_at") or record.get("created_at"))
        candidates = [draft for draft in drafts if draft.get("reasoning") is None]
        if llm_time is None or not candidates:
            continue
        selected = min(
            candidates,
            key=lambda draft: abs(
                (
                    (_timestamp(draft.get("started_at")) or datetime.min.replace(tzinfo=UTC))
                    - cast(datetime, llm_time)
                ).total_seconds()
            ),
        )
        _apply_llm(selected, record)
        unmatched.remove(record)
    if unmatched:
        warnings.append(f"{len(unmatched)} LLM record(s) could not be paired by sequence or time.")
    return unmatched


def _time_distance(draft: Mapping[str, object], when: datetime) -> float:
    started = _timestamp(draft.get("started_at"))
    finished = _timestamp(draft.get("finished_at"))
    if started is not None and finished is not None and started <= when <= finished:
        return 0.0
    points = [point for point in (started, finished) if point is not None]
    return min((abs((point - when).total_seconds()) for point in points), default=math.inf)


def _unique_step_id(base: str, kind: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    candidate = f"{base}:{kind}"
    suffix = 2
    while candidate in used:
        candidate = f"{base}:{kind}:{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _decision_draft(record: Mapping[str, object], used: set[str]) -> dict[str, object]:
    source = _text(record.get("source")) or "runtime"
    seq = _integer(record.get("sequence"))
    event_kind = _text(record.get("event_kind")) or "decision"
    outcome = _text(record.get("outcome"))
    details = _mapping(record.get("details"))
    started_at = _iso(record.get("event_time"))
    phase = _phase_for_event(event_kind, _phase_for_name(event_kind))
    decision = {"event_kind": event_kind, "outcome": outcome, "details": details}
    return {
        "step_id": _unique_step_id(f"{source}:{seq}", "decision", used),
        "seq": seq,
        "component": source,
        "role": source,
        "phase": phase,
        "kind": "decision",
        "name": event_kind,
        "started_at": started_at,
        "finished_at": started_at,
        "duration_ms": 0 if started_at is not None else None,
        "status": _status(outcome),
        "outcome": outcome,
        "reasoning": None,
        "content": None,
        "model": None,
        "finish_reason": None,
        "decision": decision,
        "tool_calls": [],
        "feedback": [],
        "artifacts": [],
        "children": [],
        "truncated": False,
        "_ids": _identifiers(record),
        "_invocation_id": None,
        "_parent_id": None,
    }


def _attach_decisions(
    drafts: list[dict[str, object]],
    logs: Iterable[Mapping[str, object]],
    used: set[str],
) -> None:
    for record in sorted(
        logs,
        key=lambda item: (
            _timestamp(item.get("event_time")) or datetime.min.replace(tzinfo=UTC),
            _integer(item.get("sequence")),
        ),
    ):
        source = _text(record.get("source")) or "runtime"
        event_kind = _text(record.get("event_kind")) or "decision"
        details = _mapping(record.get("details"))
        outcome = _text(record.get("outcome"))
        decision = {"event_kind": event_kind, "outcome": outcome, "details": details}
        record_ids = _identifiers(record)
        candidates = [
            draft
            for draft in drafts
            if draft.get("role") == source and draft.get("decision") is None
        ]
        selected = next(
            (
                draft
                for draft in candidates
                if record_ids & cast(set[str], draft.get("_ids", set()))
            ),
            None,
        )
        when = _timestamp(record.get("event_time"))
        if selected is None and when is not None and candidates:
            selected_time = cast(datetime, when)
            nearest = min(
                candidates, key=lambda draft: _time_distance(draft, selected_time)
            )
            if _time_distance(nearest, selected_time) <= 120:
                selected = nearest
        if selected is None and candidates and when is None:
            sequence = _integer(record.get("sequence"))
            selected = min(candidates, key=lambda draft: abs(cast(int, draft["seq"]) - sequence))
        if selected is None:
            drafts.append(_decision_draft(record, used))
            continue
        selected["decision"] = decision
        selected["outcome"] = outcome
        selected["status"] = _status(
            outcome,
            error=selected.get("_error"),
            finished=selected.get("finished_at") is not None,
        )
        selected["phase"] = _phase_for_event(event_kind, cast(str, selected["phase"]))
        cast(set[str], selected["_ids"]).update(record_ids)


def _feedback_draft(record: Mapping[str, object], used: set[str]) -> dict[str, object]:
    event_kind = _text(record.get("event_kind")) or "feedback"
    component = _component_for_event(record)
    seq = _integer(record.get("sequence"))
    payload = _mapping(record.get("payload"))
    when = _iso(
        record.get("event_time")
        or record.get("created_at")
        or record.get("timestamp")
    )
    outcome = _text(payload.get("lifecycle")) or _text(payload.get("status"))
    return {
        "step_id": _unique_step_id(f"{component}:{seq}", "feedback", used),
        "seq": seq,
        "component": component,
        "role": component,
        "phase": _phase_for_event(event_kind, _phase_for_name(event_kind)),
        "kind": "feedback",
        "name": event_kind,
        "started_at": when,
        "finished_at": when,
        "duration_ms": 0 if when is not None else None,
        "status": _status(outcome),
        "outcome": outcome,
        "reasoning": None,
        "content": None,
        "model": None,
        "finish_reason": None,
        "decision": None,
        "tool_calls": [],
        "feedback": [{"kind": event_kind, "payload": payload}],
        "artifacts": [],
        "children": [],
        "truncated": False,
        "_ids": _identifiers(record),
        "_invocation_id": None,
        "_parent_id": None,
    }


def _attach_feedback(
    drafts: list[dict[str, object]],
    events: Iterable[Mapping[str, object]],
    used: set[str],
) -> None:
    for record in events:
        event_kind = _text(record.get("event_kind")) or "feedback"
        payload = _mapping(record.get("payload"))
        record_ids = _identifiers(record)
        selected = next(
            (
                draft
                for draft in drafts
                if record_ids & cast(set[str], draft.get("_ids", set()))
            ),
            None,
        )
        when = _timestamp(
            record.get("event_time")
            or record.get("created_at")
            or record.get("timestamp")
        )
        if selected is None and when is not None and drafts:
            selected_time = cast(datetime, when)
            nearest = min(
                drafts, key=lambda draft: _time_distance(draft, selected_time)
            )
            if _time_distance(nearest, selected_time) <= 30:
                selected = nearest
        if selected is None:
            drafts.append(_feedback_draft(record, used))
            continue
        cast(list[dict[str, object]], selected["feedback"]).append(
            {"kind": event_kind, "payload": payload}
        )
        cast(set[str], selected["_ids"]).update(record_ids)


def _attach_artifacts(
    drafts: list[dict[str, object]], artifacts: tuple[ArtifactReference, ...]
) -> None:
    for artifact in artifacts:
        if artifact.kind in {"model.mzn", "data.dzn"}:
            phases = ("planner-execution", "planner-assets")
        elif "statechart" in artifact.kind:
            phases = ("statechart-generation",)
        else:
            phases = ("planner-assets", "planner-execution")
        candidates = [draft for draft in drafts if draft.get("phase") in phases]
        if not candidates:
            continue
        attempt = _attempt_number(artifact.ref)
        selected = next(
            (
                draft
                for draft in candidates
                if attempt is not None
                and attempt
                in {
                    _attempt_number(draft.get("name")),
                    _attempt_number(draft.get("decision")),
                }
            ),
            candidates[-1],
        )
        cast(list[ArtifactReference], selected["artifacts"]).append(artifact)


def _enrich_tool_results(drafts: list[dict[str, object]]) -> None:
    by_parent: dict[str, list[dict[str, object]]] = defaultdict(list)
    for draft in drafts:
        parent_id = draft.get("_parent_id")
        if isinstance(parent_id, str):
            by_parent[parent_id].append(draft)
    for draft in drafts:
        invocation_id = draft.get("_invocation_id")
        if not isinstance(invocation_id, str):
            continue
        children = by_parent.get(invocation_id, [])
        calls = cast(list[dict[str, object]], draft["tool_calls"])
        available = list(children)
        for call in calls:
            child = next(
                (item for item in available if item.get("name") == call.get("name")),
                None,
            )
            if child is None:
                continue
            available.remove(child)
            call["result"] = _public_value(child.get("_output"))
            call["error"] = _public_value(child.get("_error"))
            call["duration_ms"] = child.get("duration_ms")


def _step_from_draft(draft: Mapping[str, object], children: tuple[Step, ...]) -> Step:
    decision = draft.get("decision")
    phase = cast(str, draft["phase"])
    name = cast(str, draft["name"])
    calls = tuple(
        ToolCall(
            name=cast(str, item["name"]),
            args=cast(Mapping[str, object], item.get("args", {})),
            result=item.get("result"),
            error=item.get("error"),
            duration_ms=cast(int | None, item.get("duration_ms")),
        )
        for item in cast(list[dict[str, object]], draft["tool_calls"])
    )
    feedback = tuple(
        Feedback(
            cast(str, item["kind"]),
            cast(Mapping[str, object], item.get("payload", {})),
        )
        for item in cast(list[dict[str, object]], draft["feedback"])
    )
    return Step(
        step_id=cast(str, draft["step_id"]),
        seq=cast(int, draft["seq"]),
        component=cast(str, draft["component"]),
        role=cast(str, draft["role"]),
        phase=phase,
        kind=cast(StepKind, draft["kind"]),
        name=name,
        title=_title(
            phase,
            name,
            cast(Mapping[str, object], decision) if isinstance(decision, Mapping) else None,
        ),
        started_at=cast(str | None, draft.get("started_at")),
        finished_at=cast(str | None, draft.get("finished_at")),
        duration_ms=cast(int | None, draft.get("duration_ms")),
        status=cast(StepStatus, draft["status"]),
        outcome=cast(str | None, draft.get("outcome")),
        reasoning=cast(str | None, draft.get("reasoning")),
        content=cast(str | None, draft.get("content")),
        model=cast(str | None, draft.get("model")),
        finish_reason=cast(str | None, draft.get("finish_reason")),
        decision=cast(Mapping[str, object] | None, decision),
        tool_calls=calls,
        feedback=feedback,
        artifacts=tuple(cast(list[ArtifactReference], draft["artifacts"])),
        children=children,
        truncated=bool(draft.get("truncated")),
    )


def _sort_key(draft: Mapping[str, object]) -> tuple[datetime, int, str]:
    return (
        _timestamp(draft.get("started_at"))
        or _timestamp(draft.get("finished_at"))
        or datetime.max.replace(tzinfo=UTC),
        cast(int, draft["seq"]),
        cast(str, draft["step_id"]),
    )


def _tree(drafts: list[dict[str, object]]) -> tuple[Step, ...]:
    by_invocation = {
        cast(str, draft["_invocation_id"]): draft
        for draft in drafts
        if isinstance(draft.get("_invocation_id"), str)
    }
    children: dict[int, list[dict[str, object]]] = defaultdict(list)
    child_ids: set[int] = set()
    for draft in drafts:
        parent = by_invocation.get(cast(str, draft.get("_parent_id")))
        if parent is not None and parent is not draft:
            children[id(parent)].append(draft)
            child_ids.add(id(draft))

    active: set[int] = set()

    def build(draft: dict[str, object]) -> Step:
        identity = id(draft)
        if identity in active:
            return _step_from_draft(draft, ())
        active.add(identity)
        nested = tuple(build(item) for item in sorted(children[identity], key=_sort_key))
        active.remove(identity)
        return _step_from_draft(draft, nested)

    roots = [draft for draft in drafts if id(draft) not in child_ids]
    return tuple(build(draft) for draft in sorted(roots, key=_sort_key))


class StepProjection:
    """Join already-loaded artifacts without performing filesystem I/O."""

    def project(
        self,
        mission_id: str,
        *,
        llm_records: Iterable[Mapping[str, object]] = (),
        agent_invocations: Iterable[Mapping[str, object]] = (),
        operational_logs: Iterable[Mapping[str, object]] = (),
        transport_events: Iterable[Mapping[str, object]] = (),
        planner_artifacts: Iterable[Mapping[str, object]] = (),
        generated_at: str | None = None,
    ) -> StepsView:
        warnings: list[str] = []
        invocations = [item for item in agent_invocations if isinstance(item, Mapping)]
        conversations = [item for item in llm_records if isinstance(item, Mapping)]
        logs = [item for item in operational_logs if isinstance(item, Mapping)]
        events = [item for item in transport_events if isinstance(item, Mapping)]
        artifacts = tuple(
            artifact
            for item in planner_artifacts
            if isinstance(item, Mapping)
            and (artifact := ArtifactReference.from_mapping(item)) is not None
        )
        drafts = [
            draft
            for record in invocations
            if (draft := _draft_from_invocation(record)) is not None
        ]
        used = {cast(str, draft["step_id"]) for draft in drafts}

        invocations_by_role: dict[str, list[dict[str, object]]] = defaultdict(list)
        records_by_role: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for draft in drafts:
            if draft["kind"] == "llm":
                invocations_by_role[cast(str, draft["role"])].append(draft)
        for record in conversations:
            role = _text(record.get("role")) or _text(record.get("agent_role"))
            if role is not None:
                records_by_role[role].append(record)
        for role in sorted(set(invocations_by_role) | set(records_by_role)):
            role_drafts = sorted(invocations_by_role[role], key=lambda item: cast(int, item["seq"]))
            role_records = sorted(
                records_by_role[role], key=lambda item: _integer(item.get("sequence"))
            )
            unmatched = _pair_llm_records(role_drafts, role_records, warnings)
            for record in unmatched:
                draft = _draft_from_llm(record)
                if draft is None:
                    continue
                draft["step_id"] = _unique_step_id(
                    cast(str, draft["step_id"]), "llm", used
                )
                drafts.append(draft)

        if not invocations and not conversations:
            warnings.append(
                "Debug artifacts are unavailable; projected operational and transport evidence only."
            )
        elif len([draft for draft in drafts if draft["kind"] == "llm"]) != len(
            conversations
        ):
            warnings.append("Some LLM invocations have no paired reasoning record.")
        if not artifacts:
            warnings.append("Planner artifacts are unavailable.")

        _attach_decisions(drafts, logs, used)
        _attach_feedback(drafts, events, used)
        _attach_artifacts(drafts, artifacts)
        _enrich_tool_results(drafts)
        steps = _tree(drafts)
        if not steps:
            warnings.append(f"No step artifacts were found for mission '{mission_id}'.")
        return StepsView(
            mission_id=mission_id,
            generated_at=_generated_at(generated_at),
            warnings=tuple(dict.fromkeys(warnings)),
            steps=steps,
        )

    def mission_content(
        self,
        *,
        operational_logs: Iterable[Mapping[str, object]] = (),
        agent_invocations: Iterable[Mapping[str, object]] = (),
        transport_events: Iterable[Mapping[str, object]] = (),
    ) -> tuple[Mapping[str, object] | None, tuple[str, ...]]:
        """Select a small mission view from the strongest available evidence."""

        planning_logs = sorted(
            (
                item
                for item in operational_logs
                if isinstance(item, Mapping)
                and item.get("event_kind") in {"planning-intent", "planning-context"}
            ),
            key=lambda item: _integer(item.get("sequence")),
        )
        for record in planning_logs:
            mission = _mission_fields(record.get("details"))
            if mission is not None:
                return mission, ()

        planning_invocations = sorted(
            (
                item
                for item in agent_invocations
                if isinstance(item, Mapping)
                and item.get("kind") == "tool"
                and item.get("name") == "record_planning_intent"
                and (
                    item.get("role", item.get("agent_role")) in {None, "hyper-agent"}
                )
            ),
            key=lambda item: _integer(item.get("sequence")),
        )
        for record in planning_invocations:
            mission = _mission_fields(record.get("input"))
            if mission is not None:
                return mission, ()

        snapshots = sorted(
            (
                item
                for item in transport_events
                if isinstance(item, Mapping)
                and item.get("event_kind") == "mission-snapshot"
            ),
            key=lambda item: _integer(item.get("sequence")),
        )
        for record in snapshots:
            mission = _mission_fields(record.get("payload"))
            if mission is not None:
                return mission, ()
        return None, (MISSION_CONTENT_WARNING,)

    def overview(
        self,
        steps: StepsView,
        *,
        mission: Mapping[str, object] | None = None,
        status: RunStatus = "unknown",
        summaries: Iterable[Mapping[str, object]] = (),
        fsm: Mapping[str, object] | None = None,
        environment: Mapping[str, object] | None = None,
        planner_artifacts: Iterable[Mapping[str, object]] = (),
        final_result: object = None,
        warnings: Iterable[str] = (),
        generated_at: str | None = None,
    ) -> RunOverview:
        artifact_index = tuple(
            artifact
            for item in planner_artifacts
            if isinstance(item, Mapping)
            and (artifact := ArtifactReference.from_mapping(item)) is not None
        )
        flattened: list[Step] = []

        def collect(items: Iterable[Step]) -> None:
            for item in items:
                flattened.append(item)
                collect(item.children)

        collect(steps.steps)
        starts = [value for item in flattened if (value := _timestamp(item.started_at))]
        finishes = [
            value for item in flattened if (value := _timestamp(item.finished_at))
        ]
        duration = 0
        if starts and finishes and max(finishes) >= min(starts):
            duration = round((max(finishes) - min(starts)).total_seconds() * 1000)
        planner_attempts = {
            attempt
            for artifact in artifact_index
            if artifact.ref.startswith("workspace/")
            and (attempt := _attempt_number(artifact.ref)) is not None
        }
        statechart_attempts = {
            attempt
            for artifact in artifact_index
            if "statechart-attempts/" in artifact.ref
            and (attempt := _attempt_number(artifact.ref)) is not None
        }
        aggregates = {
            "step_count": len(flattened),
            "llm_call_count": sum(item.kind == "llm" for item in flattened),
            "tool_call_count": sum(item.kind == "tool" for item in flattened),
            "error_count": sum(item.status == "error" for item in flattened),
            "duration_ms": duration,
            "planner_attempts": len(planner_attempts),
            "statechart_attempts": len(statechart_attempts),
        }
        selected_status: RunStatus = (
            status if status in {"complete", "running", "unknown"} else "unknown"
        )
        return RunOverview(
            mission_id=steps.mission_id,
            generated_at=_generated_at(generated_at or steps.generated_at),
            warnings=tuple(dict.fromkeys([*steps.warnings, *warnings])),
            mission=mission if mission else None,
            status=selected_status,
            aggregates=aggregates,
            components=tuple(sorted({item.component for item in flattened})),
            summaries=tuple(
                _mapping(item) for item in summaries if isinstance(item, Mapping)
            ),
            fsm=_mapping(fsm) if fsm is not None else None,
            environment=_mapping(environment) if environment is not None else None,
            artifacts_index=artifact_index,
            final_result=_public_value(final_result),
        )


StepsProjection = StepProjection


__all__ = [
    "MISSION_CONTENT_WARNING",
    "MISSION_TEXT_LIMIT",
    "PHASES",
    "RUN_SCHEMA_VERSION",
    "STEPS_SCHEMA_VERSION",
    "TEXT_FIELD_LIMIT",
    "ArtifactReference",
    "Feedback",
    "RunOverview",
    "Step",
    "StepProjection",
    "StepsProjection",
    "StepsView",
    "ToolCall",
]
