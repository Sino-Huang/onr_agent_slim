"""DeepAgents boundary for tool-driven Maneuver heartbeats and audit parsing."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

from langchain.agents.middleware import wrap_model_call
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage

from onr.agents.hyper_agent import _create_deep_agent
from onr.agents.maneuver_tools import (
    MANEUVER_OPERATIONAL_TOOLS,
    ManeuverToolContext,
    _ingest_pending_perceptions,
    _run,
    communicate,
    navigate,
    pursue,
    set_transition_target,
    transition_fsm,
)
from onr.agents.structured_output import (
    StructuralIssue,
    StructuredOutputFailure,
    StructuredOutputRetriesExhausted,
    invoke_with_structured_output_recovery,
    summarize_mission4_coverage,
)
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverControlDecision,
    ManeuverHeartbeatCompletion,
    ManeuverInvocation,
    NonPhysicalChoice,
    PhysicalAction,
)
from onr.contracts.transition_intent import ManeuverFSMContext

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
_MANEUVER_MODEL_TOOL_NAMES: Final = frozenset(
    {tool.name for tool in MANEUVER_OPERATIONAL_TOOLS}
    | {"ManeuverHeartbeatResponse"}
)


class ManeuverHeartbeatOrderingError(RuntimeError):
    """The heartbeat ended without satisfying its FSM ordering obligations."""

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

_MANEUVER_HEARTBEAT_RESPONSE_SCHEMA: dict[str, Any] = {
    "title": "ManeuverHeartbeatResponse",
    "description": (
        "Complete this heartbeat only after receiving results for every chosen "
        "operational effect. First call the required physical, FSM, ingestion, or "
        "communication tool and inspect its result; then call this completion "
        "tool. A no-effect heartbeat may complete directly after assessment. "
        "This completion records only a summary and cannot submit an action."
    ),
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Concise public summary grounded in actual tool results. Describe "
                "a submitted action only when its operational tool returned a "
                "submission result in this heartbeat."
            ),
        },
    },
    "required": ["summary"],
    "additionalProperties": False,
}


def _request_tool_name(value: object) -> str | None:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(value, Mapping):
        function = value.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return cast(str, function["name"])
    return None


@wrap_model_call
def _gate_maneuver_scaffolding(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Keep DeepAgents memory internals while hiding unused workflow tools."""

    tools = [
        item
        for item in request.tools
        if _request_tool_name(item) in _MANEUVER_MODEL_TOOL_NAMES
    ]
    return handler(request.override(tools=tools))


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
        response_format=_MANEUVER_HEARTBEAT_RESPONSE_SCHEMA,
        mission_id=mission_id,
        role="maneuver-control",
        memory_store=memory_store,
        skill_catalog=skill_catalog,
        skill_version=skill_version,
        backend_root=backend_root,
        backend_kind="filesystem",
        tools=list(MANEUVER_OPERATIONAL_TOOLS),
        middleware=[_gate_maneuver_scaffolding],
        inline_skills=frozenset(
            {
                "decision-cycle",
                "physical-maneuver-selection",
                "hyper-coordination",
            }
        ),
        filesystem_tools=["read_file"],
        context_schema=ManeuverToolContext,
    )


def _derived_transition_facts(
    invocation: ManeuverInvocation,
) -> dict[str, object] | None:
    """Exact arithmetic and ledger membership, not a condition assessment."""
    intent = invocation.fsm_context.transition_intent
    if intent is None:
        return None
    readiness = intent.condition.get("readiness")
    if not isinstance(readiness, Mapping):
        return None

    def seconds(value: object) -> float | None:
        if isinstance(value, Mapping):
            value = value.get("seconds")
        return float(value) if type(value) in (int, float) else None

    facts: dict[str, object] = {}
    now = seconds(invocation.environment_data.get("mission_time_seconds"))
    if now is not None:
        bound = seconds(readiness.get("not_before"))
        if bound is not None:
            facts["not_before_seconds"] = bound
            facts["seconds_until_not_before"] = bound - now
        window = invocation.fsm_context.current_state_context.get("observation_window")
        if isinstance(window, Mapping):
            start = seconds(window.get("start"))
            duration = seconds(window.get("duration"))
            if start is not None and duration is not None:
                facts["window_end_seconds"] = start + duration
                facts["seconds_until_window_end"] = start + duration - now

    sensed = readiness.get("sensed_evidence")
    info = invocation.environment_data.get("world_model_info")
    if (
        isinstance(sensed, Mapping)
        and sensed.get("report_check_ledger") == "world_model_info.event_report_checks"
        and isinstance(info, Mapping)
    ):
        required = sensed.get("report_ids")
        ledger = info.get("event_report_checks")
        if (
            isinstance(required, (list, tuple))
            and all(isinstance(item, str) for item in required)
            and isinstance(ledger, (list, tuple))
        ):
            required_ids = list(dict.fromkeys(required))
            outcomes = {
                row["report_id"]: row.get("outcome")
                for row in ledger
                if isinstance(row, Mapping) and row.get("report_id") in required_ids
            }
            matched = [item for item in required_ids if item in outcomes]
            unconfirmed = [item for item in required_ids if item not in outcomes]
            facts["report_check_comparison"] = {
                "required_count": len(required_ids),
                "matched_count": len(matched),
                "matched_report_ids": matched,
                "matched_outcomes": outcomes,
                "unconfirmed_count": len(unconfirmed),
                "unconfirmed_report_ids": unconfirmed,
            }
    if not facts:
        return None
    return {
        "intent_id": intent.intent_id,
        "source_state": invocation.fsm_context.current_state,
        "target_state": intent.target_state,
        "state_entry_revision": invocation.fsm_context.state_entry_revision,
        "mission_time_seconds": now,
        **facts,
    }


def _derived_pursuit_facts(
    invocation: ManeuverInvocation,
) -> dict[str, object] | None:
    current = invocation.fsm_context.current_state_context
    target = current.get("target_entity_id")
    if current.get("surveillance_mode") != "pursue_ship" or target is None:
        return None

    def seconds(value: object) -> float | None:
        if isinstance(value, Mapping):
            value = value.get("seconds")
        return float(value) if type(value) in (int, float) else None

    def plain(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        return value

    environment = invocation.environment_data
    lifecycle = environment.get("maneuver_lifecycle")
    world = environment.get("world_model_info")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    world = world if isinstance(world, Mapping) else {}
    parameters = lifecycle.get("parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    matching_pursuit = (
        lifecycle.get("action") == "pursue"
        and lifecycle.get("lifecycle") == "active"
        and str(parameters.get("entity_id")) == str(target)
    )
    visible_ids = world.get("visible_ship_ids")
    visible_ids = visible_ids if isinstance(visible_ids, (list, tuple)) else ()
    facts: dict[str, object] = {
        "target_entity_id": target,
        "matching_active_pursuit": matching_pursuit,
        "active_acquisition_navigation": (
            lifecycle.get("action") == "navigate"
            and lifecycle.get("lifecycle") == "active"
        ),
        "target_visible": any(str(item) == str(target) for item in visible_ids),
        "pursuit_phase": lifecycle.get("phase"),
    }
    now = seconds(environment.get("mission_time_seconds"))
    attempt = seconds(lifecycle.get("start_time")) if matching_pursuit else None
    if now is not None:
        facts["mission_time_seconds"] = now
    if attempt is not None:
        facts["attempt_start_seconds"] = attempt
        bounds: list[float] = []
        window = current.get("observation_window")
        window = window if isinstance(window, Mapping) else {}
        observation_start = seconds(window.get("start"))
        if observation_start is not None and observation_start > attempt:
            facts["first_required_observation_seconds"] = observation_start
            bounds.append(observation_start)
        interval = seconds(world.get("gps_interval_seconds"))
        next_gps = seconds(world.get("next_gps_update_time_s"))
        if interval and next_gps is not None:
            cycles = max(0, math.ceil((next_gps - attempt) / interval) - 1)
            first_gps = next_gps - cycles * interval
            if first_gps > attempt:
                facts["first_gps_after_attempt_seconds"] = first_gps
                bounds.append(first_gps)
        if not bounds:
            duration = seconds(window.get("duration"))
            if observation_start is not None and duration is not None:
                bounds.append(observation_start + duration)
        if bounds:
            bound = min(bounds)
            facts["acquisition_bound_seconds"] = bound
            if now is not None:
                facts["seconds_since_acquisition_bound"] = now - bound

    fixes = world.get("public_position_fixes")
    fixes = fixes if isinstance(fixes, (list, tuple)) else ()
    target_fixes = [
        fix
        for fix in fixes
        if isinstance(fix, Mapping)
        and str(fix.get("entity_id")) == str(target)
        and seconds(fix.get("sampled_at_s")) is not None
    ]
    if target_fixes:
        newest = max(target_fixes, key=lambda fix: seconds(fix["sampled_at_s"]))
        sampled_at = seconds(newest["sampled_at_s"])
        facts["newest_target_fix"] = plain(newest)
        facts["newest_fix_after_attempt"] = (
            sampled_at > attempt
            if sampled_at is not None and attempt is not None
            else None
        )
    return facts


def _routine_future_tracking_summary(
    invocation: ManeuverInvocation,
    *,
    event_batch_resolved: bool,
) -> str | None:
    if invocation.fsm_context.transition_intent is None or invocation.hyper_outcomes:
        return None
    if (
        any(
            item.observation_kind == "event"
            for item in invocation.pending_perceptions
        )
        and not event_batch_resolved
    ):
        return None
    transition = _derived_transition_facts(invocation)
    pursuit = _derived_pursuit_facts(invocation)
    if transition is None or pursuit is None:
        return None
    future = [
        float(transition[key])
        for key in ("seconds_until_not_before", "seconds_until_window_end")
        if type(transition.get(key)) in (int, float) and transition[key] > 1e-9
    ]
    if not future:
        return None
    if not (
        pursuit.get("matching_active_pursuit") is True
        and (
            pursuit.get("pursuit_phase") == "pursuit"
            or pursuit.get("target_visible") is True
            or (
                pursuit.get("pursuit_phase") == "search"
                and type(pursuit.get("seconds_since_acquisition_bound"))
                in (int, float)
                and pursuit["seconds_since_acquisition_bound"] < 0
            )
        )
    ):
        return None
    target = pursuit["target_entity_id"]
    remaining = max(future)
    ingestion = (
        " The pending Event batch was recorded before assessment."
        if event_batch_resolved
        and any(
            item.observation_kind == "event"
            for item in invocation.pending_perceptions
        )
        else ""
    )
    if pursuit.get("pursuit_phase") == "pursuit":
        pursuit_status = "remains active in tracking phase"
    elif pursuit.get("target_visible") is True:
        pursuit_status = "remains active with the target currently visible"
    else:
        pursuit_status = "remains active within its acquisition search bound"
    return (
        f"Retained the current Transition Intent with {remaining:g} seconds of "
        f"exact time readiness remaining. Target {target}'s matching pursuit "
        f"{pursuit_status}; no physical command was submitted."
        f"{ingestion}"
    )


def _direct_tool_runtime(
    context: ManeuverToolContext, tool_call_id: str
) -> ToolRuntime[ManeuverToolContext]:
    return ToolRuntime(
        state={"messages": []},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=tool_call_id,
        store=None,
    )


def _future_transition_gate(invocation: ManeuverInvocation) -> bool:
    facts = _derived_transition_facts(invocation)
    if facts is None:
        return False
    return any(
        type(facts.get(key)) in (int, float) and facts[key] > 1e-9
        for key in ("seconds_until_not_before", "seconds_until_window_end")
    )


def _model_visible_invocation(invocation: ManeuverInvocation) -> dict[str, object]:
    """Bound historical context while retaining current and actionable evidence."""

    payload = invocation.to_dict()
    perceptions = cast(list[dict[str, object]], payload["pending_perceptions"])
    entity_rows = [
        (index, item)
        for index, item in enumerate(perceptions)
        if item.get("observation_kind") == "entity"
    ]
    if entity_rows:
        latest: dict[object, tuple[int, dict[str, object]]] = {}
        for index, item in entity_rows:
            entity_id = item["entity_id"]
            previous = latest.get(entity_id)
            if previous is None or (
                float(cast(Any, item["observed_time"])), index
            ) > (
                float(cast(Any, previous[1]["observed_time"])),
                previous[0],
            ):
                latest[entity_id] = (index, item)
        payload["pending_perceptions"] = [
            item
            for item in perceptions
            if item.get("observation_kind") != "entity"
        ]
        payload["pending_entity_perceptions"] = {
            "observation_count": len(entity_rows),
            "latest_by_entity": [
                item
                for _, item in sorted(
                    latest.values(),
                    key=lambda pair: json.dumps(
                        pair[1]["entity_id"], sort_keys=True
                    ),
                )
            ],
        }
    _project_model_world_history(payload)
    return payload


def _project_model_world_history(payload: dict[str, object]) -> None:
    pending = payload.get("pending_perceptions")
    if isinstance(pending, list):
        payload["pending_event_perception_count"] = sum(
            isinstance(item, Mapping) and item.get("observation_kind") == "event"
            for item in pending
        )
    environment = payload.get("environment_data")
    fsm_context = payload.get("fsm_context")
    if not isinstance(environment, dict) or not isinstance(fsm_context, Mapping):
        return
    info = environment.get("world_model_info")
    if not isinstance(info, dict):
        return

    summarize_mission4_coverage(payload)

    report_ids: set[str] = set()
    entity_ids: set[str] = set()

    def collect(value: object, *, include_entities: bool = True) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"report_id", "related_report_id"} and isinstance(item, str):
                    report_ids.add(item)
                elif key == "report_ids" and isinstance(item, (list, tuple)):
                    report_ids.update(
                        candidate for candidate in item if isinstance(candidate, str)
                    )
                elif (
                    include_entities
                    and key in {"entity_id", "target_entity_id"}
                    and isinstance(item, (str, int))
                    and not isinstance(item, bool)
                ):
                    entity_ids.add(str(item))
                collect(item, include_entities=include_entities)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, include_entities=include_entities)

    collect(fsm_context)
    collect(environment.get("maneuver_lifecycle"))
    collect(payload.get("pending_perceptions"), include_entities=False)
    if not report_ids and not entity_ids:
        return

    counts: dict[str, dict[str, int]] = {}

    def record(name: str, before: int, after: int) -> None:
        counts[name] = {"available": before, "shown": after}

    checks = info.get("event_report_checks")
    if isinstance(checks, list):
        selected = [
            row
            for row in checks
            if isinstance(row, Mapping)
            and (
                row.get("report_id") in report_ids
                or str(row.get("entity_id")) in entity_ids
            )
        ]
        info["event_report_checks"] = selected
        record("event_report_checks", len(checks), len(selected))

    reports = info.get("ship_event_reports")
    if isinstance(reports, dict):
        selected_reports: dict[str, object] = {}
        available = 0
        shown = 0
        for entity_id, rows in reports.items():
            if not isinstance(rows, list):
                continue
            available += len(rows)
            selected_rows = [
                row
                for row in rows
                if isinstance(row, Mapping) and row.get("report_id") in report_ids
            ]
            if not report_ids and str(entity_id) in entity_ids:
                selected_rows = rows
            if selected_rows:
                selected_reports[str(entity_id)] = selected_rows
                shown += len(selected_rows)
        info["ship_event_reports"] = selected_reports
        record("ship_event_reports", available, shown)

    fixes = info.get("public_position_fixes")
    if isinstance(fixes, list):
        selected_fixes = [
            row
            for row in fixes
            if isinstance(row, Mapping)
            and str(row.get("entity_id")) in entity_ids
        ]
        info["public_position_fixes"] = selected_fixes
        record("public_position_fixes", len(fixes), len(selected_fixes))

    issues = info.get("detected_issues")
    if isinstance(issues, dict):
        selected_issues = {
            str(entity_id): rows
            for entity_id, rows in issues.items()
            if str(entity_id) in entity_ids
        }
        info["detected_issues"] = selected_issues
        record("detected_issue_entities", len(issues), len(selected_issues))

    if counts:
        info["history_projection"] = {
            "scope": "current_fsm_and_pending_events",
            **counts,
        }


class DeepAgentsHeartbeatProvider:
    """Invoke a Maneuver Deep Agent and identify its heartbeat completion."""

    def __init__(self, agent: object, max_retries: int = 1) -> None:
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ValueError("Maneuver completion retry budget must be non-negative")
        self.agent = agent
        self.max_retries = max_retries
        self._notified_missed_acquisitions: set[tuple[object, ...]] = set()
        self._submitted_recovery_fixes: set[tuple[object, ...]] = set()

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
        pre_ingestion: dict[str, object] | None = None
        if (
            any(
                item.observation_kind == "event"
                for item in invocation.pending_perceptions
            )
            and getattr(tool_context.belief_service, "belief_kind", None)
            == "reporting_reliability"
        ):
            pre_ingestion = cast(
                dict[str, object],
                json.loads(
                    _ingest_pending_perceptions(
                        tool_context,
                        "Runtime ingested the complete pending Event batch before "
                        "Maneuver assessment.",
                    )
                ),
            )
        entry_summary = self._routine_ready_entry(invocation, tool_context)
        if entry_summary is not None:
            return ManeuverHeartbeatCompletion(
                mission_id=invocation.mission_id,
                request_id=invocation.request_id,
                summary=entry_summary,
            )
        handoff_summary = self._routine_pursuit_handoff(invocation, tool_context)
        if handoff_summary is not None:
            return ManeuverHeartbeatCompletion(
                mission_id=invocation.mission_id,
                request_id=invocation.request_id,
                summary=handoff_summary,
            )
        fixed_view_summary = self._routine_future_fixed_view(
            invocation,
            event_batch_resolved=pre_ingestion is not None,
        )
        if fixed_view_summary is not None:
            return ManeuverHeartbeatCompletion(
                mission_id=invocation.mission_id,
                request_id=invocation.request_id,
                summary=fixed_view_summary,
            )
        routine_summary = _routine_future_tracking_summary(
            invocation,
            event_batch_resolved=pre_ingestion is not None,
        )
        if routine_summary is not None:
            return ManeuverHeartbeatCompletion(
                mission_id=invocation.mission_id,
                request_id=invocation.request_id,
                summary=routine_summary,
            )
        recovery_summary = self._routine_pursuit_recovery(
            invocation,
            tool_context,
            event_batch_resolved=pre_ingestion is not None,
        )
        if recovery_summary is not None:
            return ManeuverHeartbeatCompletion(
                mission_id=invocation.mission_id,
                request_id=invocation.request_id,
                summary=recovery_summary,
            )
        payload = _model_visible_invocation(invocation)
        if pre_ingestion is not None:
            payload["pending_perceptions"] = [
                item
                for item in cast(list[dict[str, object]], payload["pending_perceptions"])
                if item.get("observation_kind") != "event"
            ]
            payload["pending_event_perception_count"] = 0
            payload["pre_ingested_event_perceptions"] = pre_ingestion
        stable_prefix = (
            "schema_version",
            "mission_id",
            "plan_revision",
            "statechart_reference",
            "available_recipients",
            "planning_snapshot",
            "fsm_context",
            "environment_data",
        )
        ordered_payload = {
            **{key: payload[key] for key in stable_prefix if key in payload},
            **payload,
        }
        messages = [
            HumanMessage(
                content=json.dumps(
                    ordered_payload,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        ]
        facts = _derived_transition_facts(invocation)
        if facts is not None:
            messages.append(
                HumanMessage(
                    content=json.dumps(
                        {"derived_transition_facts": facts},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                )
            )
        pursuit_facts = _derived_pursuit_facts(invocation)
        if pursuit_facts is not None:
            messages.append(
                HumanMessage(
                    content=json.dumps(
                        {"derived_pursuit_facts": pursuit_facts},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                )
            )
        kwargs: dict[str, object] = {"context": tool_context}
        if config is not None:
            kwargs["config"] = config
        response = invoke({"messages": messages}, **kwargs)
        for attempt in range(self.max_retries + 1):
            try:
                summary = _parse_heartbeat_summary(response)
                break
            except StructuredOutputFailure as error:
                if attempt >= self.max_retries:
                    primary = sorted(set(error.issues))[0]
                    raise StructuredOutputRetriesExhausted(primary.code) from None
                correction_state = _heartbeat_summary_correction_state(
                    response,
                    original_messages=messages,
                    error=error,
                )
                response = invoke(correction_state, **kwargs)
        else:
            raise AssertionError("unreachable")
        try:
            _require_final_transition_intent(tool_context)
        except ManeuverHeartbeatOrderingError as exc:
            if self.max_retries < 1:
                raise
            correction_state = _heartbeat_correction_state(
                response,
                original_messages=messages,
                tool_context=tool_context,
                error=exc,
            )
            response = invoke(correction_state, **kwargs)
            try:
                summary = _parse_heartbeat_summary(response)
                _require_final_transition_intent(tool_context)
            except (TypeError, ValueError, ManeuverHeartbeatOrderingError) as retry_exc:
                raise ManeuverHeartbeatOrderingError(
                    "Maneuver heartbeat correction did not select the required "
                    "Transition Intent"
                ) from retry_exc
        return ManeuverHeartbeatCompletion(
            mission_id=invocation.mission_id,
            request_id=invocation.request_id,
            summary=summary,
        )

    def _routine_ready_entry(
        self,
        invocation: ManeuverInvocation,
        tool_context: ManeuverToolContext,
    ) -> str | None:
        focused = invocation.fsm_context
        if focused.transition_intent is not None or len(focused.transition_candidates) != 1:
            return None
        candidate = focused.transition_candidates[0]
        readiness = candidate.condition.get("readiness")
        readiness = readiness if isinstance(readiness, Mapping) else {}
        threshold = readiness.get("mission_time_at_or_after")
        if isinstance(threshold, Mapping):
            threshold = threshold.get("seconds")
        now = invocation.environment_data.get("mission_time_seconds")
        if (
            type(threshold) not in (int, float)
            or type(now) not in (int, float)
            or now + 1e-9 < threshold
        ):
            return None
        runtime = _direct_tool_runtime(tool_context, "routine-ready-entry")
        selection = json.loads(
            cast(Any, set_transition_target).func(
                target_state=candidate.target_state,
                rationale="The sole exact entry transition is time-ready.",
                runtime=runtime,
            )
        )
        if selection.get("status") not in {"selected", "retained"}:
            return None
        transition = json.loads(
            cast(Any, transition_fsm).func(
                current_state=focused.current_state,
                next_state=candidate.target_state,
                assessment="satisfied",
                evidence=(
                    f"Mission time {float(now):g} meets the exact entry threshold "
                    f"{float(threshold):g}."
                ),
                uncertainty="None; this entry condition is an exact Mission-time gate.",
                runtime=runtime,
            )
        )
        if transition.get("status") != "transitioned":
            return None

        live = _current_focused_fsm_context(tool_context)
        if live.transition_candidates:
            if len(live.transition_candidates) != 1:
                raise RuntimeError("Mission 1 generated state has ambiguous next targets")
            next_target = live.transition_candidates[0].target_state
            next_selection = json.loads(
                cast(Any, set_transition_target).func(
                    target_state=next_target,
                    rationale="Retain the sole planner-ordered next assignment.",
                    runtime=runtime,
                )
            )
            if next_selection.get("status") not in {"selected", "retained"}:
                raise RuntimeError("Mission 1 next Transition Intent was rejected")

        state = live.current_state_context
        mode = state.get("surveillance_mode")
        outcome = state.get("desired_outcome")
        outcome = outcome if isinstance(outcome, Mapping) else {}
        vehicle = invocation.environment_data.get("controlled_vehicle")
        vehicle = vehicle if isinstance(vehicle, Mapping) else {}
        vehicle_position = vehicle.get("position")
        vehicle_position = (
            vehicle_position if isinstance(vehicle_position, Mapping) else {}
        )
        altitude = vehicle_position.get("z")
        candidate_id = state.get("candidate_id", candidate.target_state)
        action_id = f"assignment-entry:{candidate_id}"
        action: str | None = None
        if mode == "fixed_view":
            location = outcome.get("location")
            deadline = outcome.get("arrival_deadline")
            deadline = deadline.get("seconds") if isinstance(deadline, Mapping) else None
            planner_item = state.get("planner_item")
            parameters = (
                planner_item.get("parameters")
                if isinstance(planner_item, Mapping)
                else {}
            )
            direction = (
                parameters.get("arrival_direction")
                if isinstance(parameters, Mapping)
                else None
            )
            if not isinstance(location, Mapping):
                raise RuntimeError("Mission 1 fixed-view entry has no location")
            cast(Any, navigate).func(
                maneuver_id=action_id,
                x=location["x"],
                y=location["y"],
                z=altitude,
                deadline_time=deadline,
                arrival_direction=direction,
                reflection="Entered the planner-selected fixed-view assignment.",
                runtime=runtime,
            )
            action = "fixed-view navigation"
        elif mode == "pursue_ship":
            target = state.get("target_entity_id")
            world = invocation.environment_data.get("world_model_info")
            world = world if isinstance(world, Mapping) else {}
            visible_ids = world.get("visible_ship_ids")
            visible_ids = visible_ids if isinstance(visible_ids, (list, tuple)) else ()
            visible = any(str(item) == str(target) for item in visible_ids)
            rendezvous = outcome.get("acquisition_rendezvous")
            rendezvous = rendezvous if isinstance(rendezvous, Mapping) else {}
            location = rendezvous.get("location")
            deadline = rendezvous.get("arrival_deadline")
            deadline = deadline.get("seconds") if isinstance(deadline, Mapping) else None
            arrived = False
            if isinstance(location, Mapping):
                coordinates = (
                    vehicle_position.get("x"),
                    vehicle_position.get("y"),
                    location.get("x"),
                    location.get("y"),
                )
                if all(type(item) in (int, float) for item in coordinates):
                    arrived = math.dist(coordinates[:2], coordinates[2:]) <= 1e-6
            if visible or arrived:
                cast(Any, pursue).func(
                    maneuver_id=action_id,
                    entity_id=target,
                    reflection="Entered the planner-selected pursuit assignment.",
                    runtime=runtime,
                )
                action = "pursuit"
            else:
                if not isinstance(location, Mapping):
                    raise RuntimeError("Mission 1 pursuit entry has no rendezvous")
                cast(Any, navigate).func(
                    maneuver_id=action_id,
                    x=location["x"],
                    y=location["y"],
                    z=altitude,
                    deadline_time=deadline,
                    reflection="Entered pursuit acquisition via its rendezvous.",
                    runtime=runtime,
                )
                action = "pursuit-acquisition navigation"
        elif live.transition_candidates:
            raise RuntimeError("Mission 1 assignment has no supported surveillance mode")

        return (
            f"Applied the sole time-ready entry transition to {candidate.target_state}"
            + (f" and submitted {action}." if action is not None else ".")
        )

    def _routine_pursuit_handoff(
        self,
        invocation: ManeuverInvocation,
        tool_context: ManeuverToolContext,
    ) -> str | None:
        if not _future_transition_gate(invocation):
            return None
        state = invocation.fsm_context.current_state_context
        target = state.get("target_entity_id")
        if state.get("surveillance_mode") != "pursue_ship" or target is None:
            return None
        lifecycle = invocation.environment_data.get("maneuver_lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
        if lifecycle.get("action") != "navigate":
            return None
        world = invocation.environment_data.get("world_model_info")
        world = world if isinstance(world, Mapping) else {}
        visible_ids = world.get("visible_ship_ids")
        visible_ids = visible_ids if isinstance(visible_ids, (list, tuple)) else ()
        visible = any(str(item) == str(target) for item in visible_ids)
        arrived = lifecycle.get("lifecycle") == "completed"
        if not visible and not arrived:
            return None
        candidate_id = state.get("candidate_id", invocation.fsm_context.current_state)
        result = json.loads(
            cast(Any, pursue).func(
                maneuver_id=f"assignment-pursuit:{candidate_id}",
                entity_id=target,
                reflection=(
                    f"Target {target} is currently visible."
                    if visible
                    else f"Acquisition navigation for target {target} completed."
                ),
                runtime=_direct_tool_runtime(tool_context, "routine-pursuit-handoff"),
            )
        )
        if result.get("status") not in {"queued", "already_queued"}:
            return None
        basis = "current sighting" if visible else "navigation arrival"
        return f"Submitted pursuit of target {target} after {basis}."

    @staticmethod
    def _routine_future_fixed_view(
        invocation: ManeuverInvocation,
        *,
        event_batch_resolved: bool,
    ) -> str | None:
        if invocation.hyper_outcomes or not _future_transition_gate(invocation):
            return None
        if (
            any(
                item.observation_kind == "event"
                for item in invocation.pending_perceptions
            )
            and not event_batch_resolved
        ):
            return None
        state = invocation.fsm_context.current_state_context
        if state.get("surveillance_mode") != "fixed_view":
            return None
        outcome = state.get("desired_outcome")
        outcome = outcome if isinstance(outcome, Mapping) else {}
        location = outcome.get("location")
        lifecycle = invocation.environment_data.get("maneuver_lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
        parameters = lifecycle.get("parameters")
        parameters = parameters if isinstance(parameters, Mapping) else {}
        if (
            not isinstance(location, Mapping)
            or lifecycle.get("action") != "navigate"
            or lifecycle.get("lifecycle") not in {"active", "completed"}
            or parameters.get("x") != location.get("x")
            or parameters.get("y") != location.get("y")
        ):
            return None
        transition = _derived_transition_facts(invocation)
        assert transition is not None
        remaining = max(
            float(transition[key])
            for key in ("seconds_until_not_before", "seconds_until_window_end")
            if type(transition.get(key)) in (int, float) and transition[key] > 1e-9
        )
        status = lifecycle["lifecycle"]
        return (
            f"Retained the planner-selected fixed viewpoint with navigation {status}; "
            f"the exact transition gate remains {remaining:g} seconds in the future."
        )

    def _routine_pursuit_recovery(
        self,
        invocation: ManeuverInvocation,
        tool_context: ManeuverToolContext,
        *,
        event_batch_resolved: bool,
    ) -> str | None:
        if not _future_transition_gate(invocation):
            return None
        if (
            any(
                item.observation_kind == "event"
                for item in invocation.pending_perceptions
            )
            and not event_batch_resolved
        ):
            return None
        facts = _derived_pursuit_facts(invocation)
        if (
            facts is None
            or facts.get("matching_active_pursuit") is not True
            or facts.get("pursuit_phase") != "search"
            or facts.get("target_visible") is not False
            or type(facts.get("seconds_since_acquisition_bound"))
            not in (int, float)
            or facts["seconds_since_acquisition_bound"] < 0
        ):
            return None
        target = facts["target_entity_id"]
        attempt = facts.get("attempt_start_seconds")
        key = (
            invocation.mission_id,
            invocation.plan_revision,
            invocation.fsm_context.state_entry_revision,
            target,
            attempt,
        )
        newest_fix = facts.get("newest_target_fix")
        sampled_at = (
            newest_fix.get("sampled_at_s")
            if isinstance(newest_fix, Mapping)
            else None
        )
        if facts.get("newest_fix_after_attempt") is True:
            position = newest_fix.get("position") if isinstance(newest_fix, Mapping) else None
            vehicle = invocation.environment_data.get("controlled_vehicle")
            vehicle_position = (
                vehicle.get("position") if isinstance(vehicle, Mapping) else None
            )
            if (
                not isinstance(position, Mapping)
                or type(position.get("x")) not in (int, float)
                or type(position.get("y")) not in (int, float)
                or not isinstance(vehicle_position, Mapping)
                or type(vehicle_position.get("z")) not in (int, float)
            ):
                return None
            recovery_key = (*key, sampled_at)
            if recovery_key in self._submitted_recovery_fixes:
                return (
                    f"Retained recovery for target {target} using the already "
                    f"submitted GPS fix sampled at {sampled_at}."
                )
            reflection = (
                f"Target {target} remained unseen after its acquisition bound; "
                f"recovering to the newer GPS fix sampled at {sampled_at}."
            )
            runtime = _direct_tool_runtime(tool_context, "routine-pursuit-recovery")
            navigate_result = json.loads(
                cast(Any, navigate).func(
                    maneuver_id=f"pursuit-recovery:{target}:gps-{sampled_at}",
                    x=position["x"],
                    y=position["y"],
                    z=vehicle_position["z"],
                    reflection=reflection,
                    runtime=runtime,
                )
            )
            if navigate_result.get("status") not in {"queued", "already_queued"}:
                return None
            cast(Any, communicate).func(
                recipient="hyper-agent",
                kind="report",
                message=(
                    f"Target {target} remained unseen after the acquisition bound. "
                    f"Submitted recovery navigation to the public GPS fix sampled "
                    f"at {sampled_at}; the active assignment and target are unchanged."
                ),
                reflection=reflection,
                runtime=_direct_tool_runtime(
                    tool_context, "routine-pursuit-recovery-report"
                ),
            )
            self._submitted_recovery_fixes.add(recovery_key)
            return (
                f"Submitted recovery navigation for target {target} to the GPS "
                f"fix sampled at {sampled_at} and reported it to Hyper."
            )

        if facts.get("newest_fix_after_attempt") is not False:
            return None
        if (
            facts.get("first_gps_after_attempt_seconds")
            == facts.get("acquisition_bound_seconds")
            and abs(float(facts["seconds_since_acquisition_bound"])) <= 1e-9
        ):
            return (
                f"Retained target {target}'s active local search while the GPS fix "
                "due at this exact acquisition boundary is published."
            )
        if invocation.hyper_outcomes:
            self._notified_missed_acquisitions.add(key)
        if key in self._notified_missed_acquisitions:
            return (
                f"Retained target {target}'s active local search after its missed "
                "acquisition was already evaluated by Hyper; no newer GPS fix exists."
            )
        bound = facts.get("acquisition_bound_seconds")
        reflection = (
            f"Target {target} remained unseen when its acquisition bound {bound} "
            "passed, with no newer GPS fix available."
        )
        cast(Any, communicate).func(
            recipient="hyper-agent",
            kind="replan",
            message=(
                f"Missed acquisition for target {target}: the active pursuit remained "
                f"in search after bound {bound}, and no public GPS fix sampled after "
                f"attempt start {attempt} exists. No replacement physical command "
                "was issued; requesting Hyper evaluation."
            ),
            reflection=reflection,
            runtime=_direct_tool_runtime(tool_context, "routine-missed-acquisition"),
        )
        self._notified_missed_acquisitions.add(key)
        return (
            f"Reported target {target}'s missed acquisition to Hyper and retained "
            "the active local search because no newer GPS fix exists."
        )


def _parse_heartbeat_summary(response: object) -> str:
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
    if set(candidate) != {"summary"}:
        raise ValueError("Maneuver heartbeat response has invalid fields")
    summary = candidate["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Maneuver heartbeat response summary must be non-empty")
    return summary


def _heartbeat_summary_correction_state(
    response: object,
    *,
    original_messages: Sequence[object],
    error: StructuredOutputFailure,
) -> dict[str, object]:
    if not isinstance(response, Mapping):
        primary = sorted(set(error.issues))[0]
        raise StructuredOutputRetriesExhausted(primary.code) from None
    state = dict(response)
    state.pop("structured_response", None)
    response_messages = state.get("messages")
    messages = (
        list(response_messages)
        if isinstance(response_messages, Sequence)
        and not isinstance(response_messages, (str, bytes))
        else list(original_messages)
    )
    messages.append(
        HumanMessage(
            content=json.dumps(
                {
                    "errors": [
                        {
                            "code": issue.code,
                            "path": issue.path,
                            "expected": issue.expected,
                        }
                        for issue in sorted(set(error.issues))
                    ],
                    "completion_correction": (
                        "Call only ManeuverHeartbeatResponse with exactly one "
                        "non-empty summary field to finish this heartbeat. "
                        "This structured completion records no mission effect."
                    ),
                    "prohibition": (
                        "Do not call other tools or repeat mission effects, skill reads, "
                        "or todo updates. Preserve the successful work already recorded."
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    )
    state["messages"] = messages
    return state


def _current_focused_fsm_context(
    tool_context: ManeuverToolContext,
) -> ManeuverFSMContext:
    status = _run(tool_context.fsm_runner.status())
    if not isinstance(status, FSMStatus):
        raise TypeError("live FSM Runner did not return FSMStatus")
    journal = tool_context.transition_intents
    current = getattr(journal, "current", None)
    focused_context = getattr(journal, "focused_context", None)
    if not callable(current) or not callable(focused_context):
        raise TypeError("Maneuver heartbeat requires a Transition Intent journal")
    intent = current(status, invalidate_stale=True)
    focused = focused_context(status, intent)
    if not isinstance(focused, ManeuverFSMContext):
        raise TypeError("Transition Intent journal returned invalid focused context")
    return focused


def _require_final_transition_intent(
    tool_context: ManeuverToolContext,
) -> None:
    focused = _current_focused_fsm_context(tool_context)
    if focused.transition_candidates and focused.transition_intent is None:
        raise ManeuverHeartbeatOrderingError(
            "the final live FSM state has transition candidates but no valid "
            "selected Transition Intent"
        )


def _heartbeat_correction_state(
    response: object,
    *,
    original_messages: Sequence[object],
    tool_context: ManeuverToolContext,
    error: ManeuverHeartbeatOrderingError,
) -> dict[str, object]:
    if not isinstance(response, Mapping):
        raise ManeuverHeartbeatOrderingError(
            "Maneuver heartbeat cannot resume an invalid agent state"
        ) from error
    state = dict(response)
    state.pop("structured_response", None)
    response_messages = state.get("messages")
    messages = (
        list(response_messages)
        if isinstance(response_messages, Sequence)
        and not isinstance(response_messages, (str, bytes))
        else list(original_messages)
    )
    focused = _current_focused_fsm_context(tool_context)
    messages.append(
        HumanMessage(
            content=json.dumps(
                {
                    "completion_correction": str(error),
                    "fsm_context": focused.to_dict(),
                    "prohibition": (
                        "Do not call transition_fsm again in this heartbeat."
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    )
    state["messages"] = messages
    return state


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
    "DeepAgentsDecisionProvider",
    "DeepAgentsHeartbeatProvider",
    "DeepAgentsManeuverProvider",
    "ManeuverHeartbeatOrderingError",
    "create_maneuver_control_agent",
]
