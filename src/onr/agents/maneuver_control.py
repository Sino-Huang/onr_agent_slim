"""DeepAgents boundary for tool-driven Maneuver heartbeats and audit parsing."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import HumanMessage

from onr.agents.hyper_agent import _create_deep_agent
from onr.agents.maneuver_tools import (
    MANEUVER_OPERATIONAL_TOOLS,
    ManeuverToolContext,
)
from onr.agents.structured_output import (
    StructuralIssue,
    StructuredOutputFailure,
    invoke_with_structured_output_recovery,
)
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverControlDecision,
    ManeuverHeartbeatCompletion,
    ManeuverHeartbeatOutcome,
    ManeuverInvocation,
    NonPhysicalChoice,
    PhysicalAction,
)

_DECISION_FIELDS: Final = frozenset(
    {
        "schema_version",
        "decision_id",
        "mission_id",
        "plan_revision",
        "transition_event",
        "maneuver_id",
        "physical_intent",
        "choice",
        "payload",
    }
)
_PHYSICAL_INTENT_FIELDS: Final = frozenset({"action", "parameters"})
_PHYSICAL_ACTIONS: Final = tuple(sorted(action.value for action in PhysicalAction))
_NON_PHYSICAL_CHOICES: Final = tuple(
    sorted(choice.value for choice in NonPhysicalChoice)
)
_PHYSICAL_ACTIONS_EXPECTED: Final = "one of " + ", ".join(
    f'"{value}"' for value in _PHYSICAL_ACTIONS
)
_NON_PHYSICAL_CHOICES_EXPECTED: Final = (
    "one of "
    + ", ".join(f'"{value}"' for value in _NON_PHYSICAL_CHOICES)
    + ", or null"
)

MANEUVER_CONTROL_DECISION_SCHEMA: dict[str, Any] = {
    "title": "ManeuverControlDecision",
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "minimum": 1},
        "decision_id": {"type": "string", "minLength": 1},
        "mission_id": {"type": "string", "minLength": 1},
        "plan_revision": {"type": "integer", "minimum": 0},
        "transition_event": {"type": ["string", "null"]},
        "maneuver_id": {"type": ["string", "null"]},
        "physical_intent": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(_PHYSICAL_ACTIONS)},
                        "parameters": {
                            "type": "object",
                            "additionalProperties": {
                                "type": [
                                    "string",
                                    "number",
                                    "boolean",
                                    "null",
                                ]
                            },
                        },
                    },
                    "required": ["action", "parameters"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "choice": {
            "type": ["string", "null"],
            "enum": [*_NON_PHYSICAL_CHOICES, None],
        },
        "payload": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/json_value"},
        },
    },
    "required": sorted(_DECISION_FIELDS),
    "additionalProperties": False,
    "$defs": {
        "json_value": {
            "oneOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
                {
                    "type": "array",
                    "items": {"$ref": "#/$defs/json_value"},
                },
                {
                    "type": "object",
                    "additionalProperties": {"$ref": "#/$defs/json_value"},
                },
            ]
        }
    },
}

MANEUVER_HEARTBEAT_COMPLETION_SCHEMA: dict[str, Any] = {
    "title": "ManeuverHeartbeatCompletion",
    "type": "object",
    "properties": {
        "mission_id": {"type": "string", "minLength": 1},
        "request_id": {"type": "string", "minLength": 1},
        "outcome": {"type": "string", "enum": ["completed", "no_change"]},
        "summary": {"type": "string", "minLength": 1},
    },
    "required": ["mission_id", "request_id", "outcome", "summary"],
    "additionalProperties": False,
}


def create_maneuver_control_agent(
    *,
    model: Any,
    system_prompt: str | None = None,
    mission_id: str | None = None,
    memory_store: object | None = None,
    skill_catalog: object | None = None,
    skill_version: str | None = None,
    backend_root: Path | None = None,
) -> object:
    """Create the tool-driven DeepAgents Maneuver heartbeat."""

    return _create_deep_agent(
        model=model,
        system_prompt=system_prompt,
        response_format=MANEUVER_HEARTBEAT_COMPLETION_SCHEMA,
        mission_id=mission_id,
        role="maneuver-control",
        memory_store=memory_store,
        skill_catalog=skill_catalog,
        skill_version=skill_version,
        backend_root=backend_root,
        backend_kind="filesystem",
        tools=list(MANEUVER_OPERATIONAL_TOOLS),
        middleware=[TodoListMiddleware()],
        context_schema=ManeuverToolContext,
    )


class DeepAgentsHeartbeatProvider:
    """Invoke a Maneuver Deep Agent and validate completion against tool effects."""

    def __init__(self, agent: object, max_retries: int = 1) -> None:
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ValueError("Maneuver completion retry budget must be non-negative")
        self.agent = agent
        self.max_retries = max_retries

    def heartbeat(
        self,
        invocation: ManeuverInvocation,
        tool_context: ManeuverToolContext,
    ) -> ManeuverHeartbeatCompletion:
        if not isinstance(invocation, ManeuverInvocation):
            raise TypeError("Maneuver heartbeat provider requires ManeuverInvocation")
        if not isinstance(tool_context, ManeuverToolContext):
            raise TypeError("Maneuver heartbeat provider requires ManeuverToolContext")
        if tool_context.invocation != invocation:
            raise ValueError("Maneuver heartbeat tool context invocation does not match")
        invoke = cast(Any, self.agent).invoke
        callback = getattr(self.agent, "_onr_debug_callback", None)
        config = {"callbacks": [callback]} if callback is not None else None
        messages = [
            HumanMessage(
                content=json.dumps(
                    invocation.to_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        ]
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            kwargs: dict[str, object] = {"context": tool_context}
            if config is not None:
                kwargs["config"] = config
            response = invoke({"messages": messages}, **kwargs)
            try:
                completion = _parse_heartbeat_completion(response)
                self._validate_completion(completion, invocation, tool_context)
                return completion
            except (TypeError, ValueError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                messages = [
                    HumanMessage(
                        content=json.dumps(
                            {
                                "completion_correction": str(exc),
                                "request_id": invocation.request_id,
                                "successful_tool_calls": (
                                    tool_context.execution_record.successful_count
                                ),
                                "tool_executions": len(
                                    tool_context.execution_record.executions
                                ),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                ]
        assert last_error is not None
        raise last_error

    @staticmethod
    def _validate_completion(
        completion: ManeuverHeartbeatCompletion,
        invocation: ManeuverInvocation,
        tool_context: ManeuverToolContext,
    ) -> None:
        if (
            completion.mission_id != invocation.mission_id
            or completion.request_id != invocation.request_id
        ):
            raise ValueError("Maneuver completion identity does not match invocation")
        successful = tool_context.execution_record.successful_count
        executed = len(tool_context.execution_record.executions)
        if (
            completion.outcome is ManeuverHeartbeatOutcome.COMPLETED
            and successful < 1
        ):
            raise ValueError("completed Maneuver heartbeat requires a successful tool call")
        if completion.outcome is ManeuverHeartbeatOutcome.NO_CHANGE and executed != 0:
            raise ValueError("no_change Maneuver heartbeat requires no tool executions")


def _parse_heartbeat_completion(response: object) -> ManeuverHeartbeatCompletion:
    if isinstance(response, ManeuverHeartbeatCompletion):
        return response
    if not isinstance(response, Mapping):
        raise TypeError("Maneuver heartbeat returned invalid agent state")
    candidate = response.get("structured_response")
    model_dump = getattr(candidate, "model_dump", None)
    if callable(model_dump):
        candidate = model_dump()
    if candidate is None:
        candidate = _decision_from_final_message(response)
    if not isinstance(candidate, Mapping):
        raise TypeError("Maneuver heartbeat returned invalid structured output")
    return ManeuverHeartbeatCompletion.from_dict(cast(Mapping[str, object], candidate))


class DeepAgentsDecisionProvider:
    """Adapt a Deep Agent response to the application's validation gate."""

    def __init__(self, agent: object, max_retries: int = 1) -> None:
        self.agent = agent
        self.max_retries = max_retries

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
    ) -> ManeuverControlDecision:
        invoke = getattr(self.agent, "invoke", None)
        if not callable(invoke):
            raise TypeError("Deep Maneuver Control agent must expose invoke")
        original_context: dict[str, object] = {
            "snapshot": snapshot.to_dict(),
            "fsm_status": status.to_dict(),
            "overlay": overlay.to_dict() if overlay is not None else None,
        }
        callback = getattr(self.agent, "_onr_debug_callback", None)

        def invoke_with_callback(state: Mapping[str, object]) -> object:
            if callback is None:
                return invoke(state)
            return invoke(state, config={"callbacks": [callback]})

        return invoke_with_structured_output_recovery(
            invoke_with_callback,
            original_context,
            self.max_retries,
            _parse_decision_response,
        )


class DeepAgentsManeuverProvider(
    DeepAgentsHeartbeatProvider, DeepAgentsDecisionProvider
):
    """Runtime provider exposing the heartbeat path and retained audit parser."""


def _failure(*issues: StructuralIssue) -> StructuredOutputFailure:
    return StructuredOutputFailure(issues)


def _malformed() -> StructuredOutputFailure:
    return _failure(
        StructuralIssue(
            "malformed_structured_output", "$", "valid structured output"
        )
    )


def _parse_decision_response(candidate: object) -> ManeuverControlDecision:
    if isinstance(candidate, ManeuverControlDecision):
        return candidate
    if not isinstance(candidate, Mapping):
        raise _malformed()

    structured = candidate.get("structured_response")
    if isinstance(structured, ManeuverControlDecision):
        return structured
    if isinstance(structured, Mapping):
        decision_data = structured
    elif structured is not None:
        raise _failure(
            StructuralIssue("invalid_type", "$.structured_response", "object")
        )
    else:
        decision_data = _decision_from_final_message(candidate)

    issues = _decision_issues(decision_data)
    if issues:
        raise StructuredOutputFailure(issues)
    try:
        return ManeuverControlDecision.from_dict(
            cast(Mapping[str, object], decision_data)
        )
    except (TypeError, ValueError):
        raise _failure(
            StructuralIssue("invalid_value", "$", "valid ManeuverControlDecision")
        ) from None


def _decision_from_final_message(
    response: Mapping[object, object],
) -> Mapping[object, object]:
    messages = response.get("messages")
    if (
        not isinstance(messages, Sequence)
        or isinstance(messages, (str, bytes))
        or not messages
    ):
        raise _malformed()
    content = getattr(messages[-1], "content", None)
    if not isinstance(content, str):
        raise _malformed()
    try:
        decoded = json.loads(content, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise _malformed() from None
    if not isinstance(decoded, Mapping):
        raise _failure(
            StructuralIssue("invalid_type", "$.messages[-1].content", "JSON object")
        )
    return decoded


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _decision_issues(value: Mapping[object, object]) -> tuple[StructuralIssue, ...]:
    issues: list[StructuralIssue] = []
    keys = set(value)
    for field in sorted(_DECISION_FIELDS - keys):
        issues.append(
            StructuralIssue("missing_required_field", f"$.{field}", "required field")
        )
    if keys - _DECISION_FIELDS:
        issues.append(StructuralIssue("unexpected_field", "$", "exact field set"))

    _check_integer(value, "schema_version", issues)
    _check_string(value, "decision_id", issues)
    _check_string(value, "mission_id", issues)
    _check_integer(value, "plan_revision", issues)
    _check_nullable_string(value, "transition_event", issues)
    _check_nullable_string(value, "maneuver_id", issues)
    _check_choice(value, issues)
    _check_physical_intent(value, issues)
    _check_payload(value, issues)
    return tuple(issues)


def _check_integer(
    value: Mapping[object, object], field: str, issues: list[StructuralIssue]
) -> None:
    if field not in value:
        return
    item = value[field]
    if isinstance(item, bool) or not isinstance(item, int):
        issues.append(StructuralIssue("invalid_type", f"$.{field}", "integer"))


def _check_string(
    value: Mapping[object, object], field: str, issues: list[StructuralIssue]
) -> None:
    if field in value and not isinstance(value[field], str):
        issues.append(StructuralIssue("invalid_type", f"$.{field}", "string"))


def _check_nullable_string(
    value: Mapping[object, object], field: str, issues: list[StructuralIssue]
) -> None:
    if field in value and value[field] is not None and not isinstance(value[field], str):
        issues.append(
            StructuralIssue("invalid_type", f"$.{field}", "string or null")
        )


def _check_choice(
    value: Mapping[object, object], issues: list[StructuralIssue]
) -> None:
    if "choice" not in value or value["choice"] is None:
        return
    choice = value["choice"]
    if not isinstance(choice, str):
        issues.append(StructuralIssue("invalid_type", "$.choice", "string or null"))
    elif choice not in _NON_PHYSICAL_CHOICES:
        issues.append(
            StructuralIssue(
                "invalid_value", "$.choice", _NON_PHYSICAL_CHOICES_EXPECTED
            )
        )


def _check_physical_intent(
    value: Mapping[object, object], issues: list[StructuralIssue]
) -> None:
    if "physical_intent" not in value or value["physical_intent"] is None:
        return
    physical = value["physical_intent"]
    if not isinstance(physical, Mapping):
        issues.append(
            StructuralIssue("invalid_type", "$.physical_intent", "object or null")
        )
        return
    keys = set(physical)
    for field in sorted(_PHYSICAL_INTENT_FIELDS - keys):
        issues.append(
            StructuralIssue(
                "missing_required_field",
                f"$.physical_intent.{field}",
                "required field",
            )
        )
    if keys - _PHYSICAL_INTENT_FIELDS:
        issues.append(
            StructuralIssue(
                "unexpected_field", "$.physical_intent", "exact field set"
            )
        )
    action = physical.get("action")
    if "action" in physical:
        if not isinstance(action, str):
            issues.append(
                StructuralIssue("invalid_type", "$.physical_intent.action", "string")
            )
        elif action not in _PHYSICAL_ACTIONS:
            issues.append(
                StructuralIssue(
                    "invalid_value",
                    "$.physical_intent.action",
                    _PHYSICAL_ACTIONS_EXPECTED,
                )
            )
    parameters = physical.get("parameters")
    if "parameters" not in physical:
        return
    if not isinstance(parameters, Mapping):
        issues.append(
            StructuralIssue(
                "invalid_type", "$.physical_intent.parameters", "object"
            )
        )
        return
    if any(not isinstance(key, str) for key in parameters):
        issues.append(
            StructuralIssue(
                "invalid_type", "$.physical_intent.parameters.*", "string field name"
            )
        )
    if any(not _is_json_scalar(item) for item in parameters.values()):
        issues.append(
            StructuralIssue(
                "invalid_type", "$.physical_intent.parameters.*", "JSON scalar"
            )
        )


def _check_payload(
    value: Mapping[object, object], issues: list[StructuralIssue]
) -> None:
    if "payload" not in value:
        return
    payload = value["payload"]
    if not isinstance(payload, Mapping):
        issues.append(StructuralIssue("invalid_type", "$.payload", "object"))
        return
    if not _is_json_object(payload):
        issues.append(
            StructuralIssue("invalid_type", "$.payload.*", "JSON-compatible value")
        )


def _is_json_scalar(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _is_json_object(value: Mapping[object, object]) -> bool:
    return all(
        isinstance(key, str) and _is_json_value(item) for key, item in value.items()
    )


def _is_json_value(value: object) -> bool:
    if _is_json_scalar(value):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return _is_json_object(value)
    return False
__all__ = [
    "MANEUVER_CONTROL_DECISION_SCHEMA",
    "MANEUVER_HEARTBEAT_COMPLETION_SCHEMA",
    "DeepAgentsDecisionProvider",
    "DeepAgentsHeartbeatProvider",
    "DeepAgentsManeuverProvider",
    "create_maneuver_control_agent",
]
