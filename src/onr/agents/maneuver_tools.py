"""Operational tools and opaque runtime context for Maneuver heartbeats."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import RLock, Thread
from typing import Annotated, Any, Literal, cast

from langchain.tools import ToolRuntime, tool
from pydantic import Field
from typing_extensions import TypedDict

from onr.contracts.bayesian_belief import EntityAssociation, RiskObservation
from onr.contracts.communication import AgentMessage, AgentMessageKind
from onr.contracts.environment import (
    EntityId,
    EventObservation,
    environment_mission_time,
)
from onr.contracts.fsm import FSMStatus, ManeuverDecision
from onr.contracts.hyper_agent import ReplanRequest
from onr.contracts.maneuver_control import (
    ManeuverControlDecision,
    ManeuverInvocation,
    NonPhysicalChoice,
)
from onr.contracts.planning import ManeuverIntent, ManeuverParameter
from onr.contracts.transition_intent import (
    ManeuverFSMContext,
    TransitionAssessment,
    TransitionIntent,
)
from onr.contracts.transport import CommandOutcome, TransportEvent

JsonScalar = str | int | float | bool | None
_RETIRED_OBSERVATION_PARAMETERS = frozenset(
    {
        "observation_start",
        "observation_duration",
        "source_event_index",
        "expected_observation_count",
    }
)


class PlanarPoint(TypedDict):
    x: float
    y: float


Polygon = Annotated[list[PlanarPoint], Field(min_length=3)]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _run(awaitable: Any) -> Any:
    """Run an FSM coroutine from synchronous tools, including async test hosts."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: list[object] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except Exception as exc:  # noqa: BLE001 - propagate service failure.
            failure.append(exc)

    thread = Thread(target=target, name="maneuver-fsm-adapter")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


@dataclass(frozen=True, slots=True)
class ManeuverToolExecution:
    """One code-owned audit entry for a tool attempt."""

    name: str
    successful: bool
    result: Mapping[str, object]


@dataclass(slots=True)
class ManeuverHeartbeatExecutionRecord:
    """Code-owned audit of one heartbeat's initial intent and tool effects."""

    executions: list[ManeuverToolExecution] = field(default_factory=list)
    decisions: list[ManeuverControlDecision] = field(default_factory=list)
    initial_intent_id: str | None = None
    successful_transition_count: int = 0
    tool_lock: Any = field(default_factory=RLock, repr=False)

    @property
    def successful_count(self) -> int:
        return sum(item.successful for item in self.executions)

    @property
    def selected_intent_ids(self) -> tuple[str, ...]:
        selected: list[str] = []
        for execution in self.executions:
            if execution.name != "set_transition_target" or not execution.successful:
                continue
            intent = execution.result.get("transition_intent")
            if isinstance(intent, Mapping):
                intent_id = intent.get("intent_id")
                if isinstance(intent_id, str):
                    selected.append(intent_id)
        return tuple(selected)

    def append(
        self,
        name: str,
        result: Mapping[str, object],
        *,
        successful: bool,
        decision: ManeuverControlDecision | None = None,
    ) -> None:
        self.executions.append(ManeuverToolExecution(name, successful, dict(result)))
        if name == "transition_fsm" and successful:
            self.successful_transition_count += 1
        if decision is not None:
            self.decisions.append(decision)


@dataclass(slots=True)
class ManeuverToolContext:
    """Opaque dependencies for one heartbeat; never serialized to the model."""

    invocation: Any
    fsm_runner: Any
    command_dispatcher: Any
    belief_service: Any = None
    communication_port: Any = None
    transition_intents: Any = None
    operational_log: Any = None
    # ToolRuntime asks Pydantic to serialize its context between graph steps.
    # Keep the audit record opaque like the service dependencies so immutable
    # MappingProxy payloads are never treated as model-visible dictionaries.
    execution_record: Any = field(default_factory=ManeuverHeartbeatExecutionRecord)
    perception_batch_ingested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.invocation, ManeuverInvocation):
            raise TypeError("Maneuver tools require a ManeuverInvocation")
        initial_intent = self.invocation.fsm_context.transition_intent
        initial_intent_id = (
            initial_intent.intent_id if initial_intent is not None else None
        )
        if self.execution_record.initial_intent_id is None:
            self.execution_record.initial_intent_id = initial_intent_id
        elif self.execution_record.initial_intent_id != initial_intent_id:
            raise ValueError("Maneuver execution record initial intent does not match")
        for dependency, method, label in (
            (self.fsm_runner, "status", "FSM Runner"),
            (self.fsm_runner, "apply", "FSM Runner"),
            (self.command_dispatcher, "dispatch_physical", "command dispatcher"),
        ):
            if not callable(getattr(dependency, method, None)):
                raise TypeError(f"Maneuver {label} must expose {method}")
        if self.belief_service is not None and not callable(
            getattr(self.belief_service, "handle", None)
        ):
            raise TypeError("Maneuver belief service must expose handle")
        if self.communication_port is not None and not callable(
            getattr(self.communication_port, "request", None)
        ):
            raise TypeError("Maneuver communication port must expose request")


def _context(runtime: ToolRuntime[ManeuverToolContext]) -> ManeuverToolContext:
    context = runtime.context
    if not isinstance(context, ManeuverToolContext):
        raise TypeError("Maneuver tool requires ManeuverToolContext")
    return context


def _intent_journal(context: ManeuverToolContext) -> Any:
    journal = context.transition_intents
    for method in (
        "current",
        "select",
        "consume",
        "exact_candidate",
        "focused_context",
    ):
        if not callable(getattr(journal, method, None)):
            raise TypeError(
                f"Maneuver Transition Intent journal must expose {method}"
            )
    return journal


def _candidate_result(status: FSMStatus) -> dict[str, object]:
    return {
        "current_state": status.active_state,
        "candidates": [
            {
                "target_state": item.target,
                "condition": item.to_dict()["transition_context"],
            }
            for item in status.transition_candidates
        ],
    }


def _update_live_fsm_context(
    context: ManeuverToolContext, focused: ManeuverFSMContext
) -> None:
    update = getattr(context.command_dispatcher, "update_live_fsm_context", None)
    if callable(update):
        update(focused)


def _live_fsm_context(context: ManeuverToolContext) -> ManeuverFSMContext:
    current = getattr(
        context.command_dispatcher, "current_maneuver_fsm_context", None
    )
    if callable(current):
        focused = current()
        if isinstance(focused, ManeuverFSMContext):
            return focused
    return context.invocation.fsm_context


def _heartbeat_tool_lock(context: ManeuverToolContext) -> Any:
    lock = getattr(context.command_dispatcher, "heartbeat_tool_lock", None)
    return lock if lock is not None else context.execution_record.tool_lock


@tool(parse_docstring=True)
def set_transition_target(
    target_state: str,
    rationale: str,
    runtime: ToolRuntime[ManeuverToolContext],
) -> str:
    """Select one exact live target without changing FSM state.

    Args:
        target_state: Exact target state from the current live candidates.
        rationale: Concise public rationale for selecting this target.

    Returns:
        Canonical JSON containing the selected intent or current live candidates.
    """

    context = _context(runtime)
    with _heartbeat_tool_lock(context):
        return _set_transition_target(context, target_state, rationale)


def _set_transition_target(
    context: ManeuverToolContext,
    target_state: str,
    rationale: str,
) -> str:
    journal = _intent_journal(context)
    status = _run(context.fsm_runner.status())
    if not isinstance(status, FSMStatus):
        raise TypeError("live FSM Runner did not return FSMStatus")
    if (
        status.mission_id != context.invocation.mission_id
        or status.plan_revision != context.invocation.plan_revision
    ):
        result = {
            "status": "rejected",
            "reason": "live FSM status does not match the current Maneuver invocation",
            **_candidate_result(status),
        }
        context.execution_record.append(
            "set_transition_target", result, successful=False
        )
        return _canonical_json(result)
    try:
        journal.exact_candidate(status, target_state)
    except ValueError:
        result = {
            "status": "rejected",
            "reason": "target state is not an exact current transition candidate",
            **_candidate_result(status),
        }
        context.execution_record.append(
            "set_transition_target", result, successful=False
        )
        return _canonical_json(result)
    before = journal.current(status, invalidate_stale=True)
    selected_at = environment_mission_time(context.invocation.environment_data)
    intent = journal.select(
        status,
        target_state,
        rationale,
        selected_at=float(selected_at),
    )
    _update_live_fsm_context(
        context, journal.focused_context(status, intent)
    )
    result = {
        "status": (
            "retained"
            if before is not None and before.intent_id == intent.intent_id
            else "selected"
        ),
        "transition_intent": intent.to_dict(),
        **_candidate_result(status),
    }
    context.execution_record.append(
        "set_transition_target", result, successful=True
    )
    return _canonical_json(result)


@tool(parse_docstring=True)
def transition_fsm(
    current_state: str,
    next_state: str,
    assessment: Literal["satisfied", "satisfied_with_uncertainty"],
    evidence: str,
    uncertainty: str,
    runtime: ToolRuntime[ManeuverToolContext],
) -> str:
    """Consume the selected intent and apply its exact internal FSM event.

    Args:
        current_state: Exact current state returned by the live FSM context.
        next_state: Exact selected target state from the current Transition Intent.
        assessment: Maneuver's semantic assessment of the selected condition.
        evidence: Concise public evidence supporting the assessment.
        uncertainty: Concise public uncertainty or accepted missingness summary.

    Returns:
        Canonical JSON containing rejection evidence or the updated focused FSM context.
    """

    context = _context(runtime)
    with _heartbeat_tool_lock(context):
        return _transition_fsm(
            context,
            current_state=current_state,
            next_state=next_state,
            assessment=assessment,
            evidence=evidence,
            uncertainty=uncertainty,
        )


def _transition_fsm(
    context: ManeuverToolContext,
    *,
    current_state: str,
    next_state: str,
    assessment: Literal["satisfied", "satisfied_with_uncertainty"],
    evidence: str,
    uncertainty: str,
) -> str:
    journal = _intent_journal(context)
    status = _run(context.fsm_runner.status())
    if not isinstance(status, FSMStatus):
        raise TypeError("live FSM Runner did not return FSMStatus")
    if context.execution_record.successful_transition_count >= 1:
        result = {
            "status": "rejected",
            "reason": (
                "one successful FSM transition is already recorded for this "
                "Maneuver heartbeat"
            ),
            **_candidate_result(status),
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    if (
        status.mission_id != context.invocation.mission_id
        or status.plan_revision != context.invocation.plan_revision
        or status.active_state != current_state
    ):
        result = {
            "status": "rejected",
            "reason": "live FSM identity does not match the transition request",
            **_candidate_result(status),
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    try:
        parsed_assessment = TransitionAssessment(assessment)
        candidate = journal.exact_candidate(status, next_state)
    except ValueError:
        result = {
            "status": "rejected",
            "reason": "source and target are not an exact current transition candidate",
            **_candidate_result(status),
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    intent = journal.current(status, invalidate_stale=True)
    if (
        not isinstance(intent, TransitionIntent)
        or intent.source_state != current_state
        or intent.target_state != next_state
        or intent.condition != candidate.transition_context
    ):
        result = {
            "status": "rejected",
            "reason": "transition request does not match the current Transition Intent",
            **_candidate_result(status),
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    initial_intent_id = context.execution_record.initial_intent_id
    if (
        initial_intent_id is not None
        and intent.intent_id != initial_intent_id
    ):
        result = {
            "status": "rejected",
            "reason": (
                "a replacement Transition Intent cannot be assessed until a "
                "fresh Maneuver heartbeat"
            ),
            **_candidate_result(status),
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    if (
        initial_intent_id is None
        and intent.intent_id not in context.execution_record.selected_intent_ids
    ):
        result = {
            "status": "rejected",
            "reason": (
                "a heartbeat that began without a Transition Intent must select "
                "one before transition"
            ),
            **_candidate_result(status),
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    sequence = len(context.execution_record.executions) + 1
    decision_id = f"maneuver-transition:{context.invocation.request_id}:{sequence}"
    authorization = ManeuverDecision(
        decision_id=decision_id,
        mission_id=context.invocation.mission_id,
        transition_event=candidate.event,
        payload={
            "plan_revision": context.invocation.plan_revision,
            "transition_intent": intent.intent_id,
        },
    )
    updated = _run(context.fsm_runner.apply(candidate, authorization))
    if not isinstance(updated, FSMStatus) or updated.active_state != candidate.target:
        result = {
            "status": "rejected",
            "reason": "live FSM Runner did not apply the candidate",
            **_candidate_result(updated if isinstance(updated, FSMStatus) else status),
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    audit = ManeuverControlDecision(
        decision_id=decision_id,
        mission_id=context.invocation.mission_id,
        plan_revision=context.invocation.plan_revision,
        transition_event=candidate.event,
        payload={
            "assessment": parsed_assessment,
            "evidence": evidence,
            "uncertainty": uncertainty,
            "transition_intent": intent.intent_id,
        },
    )
    journal.consume(intent)
    focused = journal.focused_context(updated, None)
    _update_live_fsm_context(context, focused)
    result = {
        "status": "transitioned",
        "fsm_context": focused.to_dict(),
    }
    context.execution_record.append(
        "transition_fsm", result, successful=True, decision=audit
    )
    return _canonical_json(result)


def _parameters(
    required: Mapping[str, object],
    extra_parameters: Mapping[str, JsonScalar] | None,
) -> tuple[ManeuverParameter, ...]:
    extras = dict(extra_parameters or {})
    retired = _RETIRED_OBSERVATION_PARAMETERS.intersection(extras)
    if retired:
        retired_names = ", ".join(sorted(retired))
        raise ValueError(
            f"retired observation parameters are not accepted: {retired_names}"
        )
    for value in extras.values():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("extra parameters must contain only JSON scalars")
    values: dict[str, Any] = dict(extras)
    values.update(required)
    return tuple(ManeuverParameter(name, value) for name, value in values.items())


def _deadline(value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError("deadline_time must be a finite non-negative Mission time")
    return value


def _polygon(value: object) -> list[dict[str, float]]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise ValueError("polygon must contain at least three planar points")
    result: list[dict[str, float]] = []
    for point in value:
        if not isinstance(point, Mapping) or set(point) != {"x", "y"}:
            raise ValueError("polygon points must contain exactly x and y")
        coordinates: dict[str, float] = {}
        for name in ("x", "y"):
            coordinate = point[name]
            if (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(float(coordinate))
            ):
                raise ValueError("polygon coordinates must be finite numbers")
            coordinates[name] = float(coordinate)
        result.append(coordinates)
    return result


def _physical(
    context: ManeuverToolContext,
    *,
    tool_name: str,
    maneuver_id: str,
    action: str,
    parameters: tuple[ManeuverParameter, ...],
    reflection: str,
) -> str:
    with _heartbeat_tool_lock(context):
        return _physical_once(
            context,
            tool_name=tool_name,
            maneuver_id=maneuver_id,
            action=action,
            parameters=parameters,
            reflection=reflection,
        )


def _physical_once(
    context: ManeuverToolContext,
    *,
    tool_name: str,
    maneuver_id: str,
    action: str,
    parameters: tuple[ManeuverParameter, ...],
    reflection: str,
) -> str:
    status = _run(context.fsm_runner.status())
    if not isinstance(status, FSMStatus):
        raise TypeError("live FSM Runner did not return FSMStatus")
    if (
        status.mission_id != context.invocation.mission_id
        or status.plan_revision != context.invocation.plan_revision
    ):
        result = {
            "status": "rejected",
            "reason": "live FSM status does not match the current Maneuver invocation",
            **_candidate_result(status),
        }
        context.execution_record.append(tool_name, result, successful=False)
        return _canonical_json(result)
    if status.transition_candidates:
        intent = _intent_journal(context).current(
            status, invalidate_stale=True
        )
        if not isinstance(intent, TransitionIntent):
            result = {
                "status": "rejected",
                "reason": (
                    "a valid Transition Intent is required before a physical "
                    "action in the current state"
                ),
                **_candidate_result(status),
            }
            context.execution_record.append(tool_name, result, successful=False)
            return _canonical_json(result)
    sequence = len(context.execution_record.executions) + 1
    decision = ManeuverControlDecision(
        decision_id=f"maneuver-action:{context.invocation.request_id}:{sequence}",
        mission_id=context.invocation.mission_id,
        plan_revision=context.invocation.plan_revision,
        maneuver_id=maneuver_id,
        physical_intent=ManeuverIntent(action, parameters),
        payload={"reflection": reflection},
    )
    command, queued = context.command_dispatcher.dispatch_physical(
        context.invocation,
        decision,
        sequence=sequence,
    )
    result = {
        "status": "queued" if queued else "already_queued",
        "command_id": command.command_id,
        "correlation_id": command.correlation_id,
        "maneuver_id": command.maneuver_id,
        "action": command.action,
    }
    context.execution_record.append(
        tool_name,
        result,
        successful=True,
        decision=decision if queued else None,
    )
    return _canonical_json(result)


@tool(parse_docstring=True)
def navigate(
    maneuver_id: str,
    x: float,
    y: float,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    z: float | None = None,
    speed: float | None = None,
    deadline_time: float | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit deadline-aware navigation.

    Args:
        maneuver_id: Action identity selected for this navigation.
        x: Target planar x coordinate.
        y: Target planar y coordinate.
        reflection: Concise public evidence summary for this action.
        z: Optional target altitude or depth.
        speed: Optional requested speed.
        deadline_time: Absolute non-negative Mission time by which to reach the target.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    context = _context(runtime)
    try:
        required: dict[str, JsonScalar] = {"x": x, "y": y}
        if z is not None:
            required["z"] = z
        if speed is not None:
            required["speed"] = speed
        if deadline_time is not None:
            required["deadline_time"] = _deadline(deadline_time)
        parameters = _parameters(required, extra_parameters)
    except ValueError as exc:
        result = {"status": "rejected", "reason": str(exc)}
        context.execution_record.append("navigate", result, successful=False)
        return _canonical_json(result)
    return _physical(
        context,
        tool_name="navigate",
        maneuver_id=maneuver_id,
        action="navigate",
        parameters=parameters,
        reflection=reflection,
    )


@tool(parse_docstring=True)
def takeoff(
    maneuver_id: str,
    altitude: float,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit a takeoff action.

    Args:
        maneuver_id: Action identity selected for takeoff.
        altitude: Requested takeoff altitude.
        reflection: Concise public evidence summary for this action.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    context = _context(runtime)
    return _physical(
        context,
        tool_name="takeoff",
        maneuver_id=maneuver_id,
        action="takeoff",
        parameters=_parameters({"altitude": altitude}, extra_parameters),
        reflection=reflection,
    )


@tool(parse_docstring=True)
def land(
    maneuver_id: str,
    x: float,
    y: float,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    z: float | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit a planar landing action.

    Args:
        maneuver_id: Action identity selected for landing.
        x: Landing x coordinate.
        y: Landing y coordinate.
        reflection: Concise public evidence summary for this action.
        z: Optional landing altitude or depth.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    required: dict[str, JsonScalar] = {"x": x, "y": y}
    if z is not None:
        required["z"] = z
    context = _context(runtime)
    return _physical(
        context,
        tool_name="land",
        maneuver_id=maneuver_id,
        action="land",
        parameters=_parameters(required, extra_parameters),
        reflection=reflection,
    )


@tool(parse_docstring=True)
def search_area(
    maneuver_id: str,
    polygon: Polygon,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    altitude: float | None = None,
    speed: float | None = None,
    deadline_time: float | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit a perimeter search over an ordered planar polygon.

    Args:
        maneuver_id: Action identity selected for the search.
        polygon: At least three finite x/y points in authoritative traversal order.
        reflection: Concise public evidence summary for this action.
        altitude: Optional search altitude.
        speed: Optional requested speed.
        deadline_time: Absolute non-negative Mission time by which to finish the route.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    required: dict[str, object] = {"polygon": _polygon(polygon)}
    if altitude is not None:
        required["altitude"] = altitude
    if speed is not None:
        required["speed"] = speed
    if deadline_time is not None:
        required["deadline_time"] = _deadline(deadline_time)
    context = _context(runtime)
    return _physical(
        context,
        tool_name="search_area",
        maneuver_id=maneuver_id,
        action="search_area",
        parameters=_parameters(required, extra_parameters),
        reflection=reflection,
    )


@tool(parse_docstring=True)
def pursue(
    maneuver_id: str,
    entity_id: EntityId,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    standoff_distance: float | None = None,
    speed: float | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit an entity-pursuit action.

    Args:
        maneuver_id: Action identity selected for pursuit.
        entity_id: Environment entity identifier to pursue.
        reflection: Concise public evidence summary for this action.
        standoff_distance: Optional desired separation distance.
        speed: Optional requested speed.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    required: dict[str, JsonScalar] = {"entity_id": entity_id}
    if standoff_distance is not None:
        required["standoff_distance"] = standoff_distance
    if speed is not None:
        required["speed"] = speed
    context = _context(runtime)
    return _physical(
        context,
        tool_name="pursue",
        maneuver_id=maneuver_id,
        action="pursue",
        parameters=_parameters(required, extra_parameters),
        reflection=reflection,
    )


@tool(parse_docstring=True)
def investigate(
    maneuver_id: str,
    entity_id: EntityId,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    standoff_distance: float | None = None,
    deadline_time: float | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit an entity-investigation action.

    Args:
        maneuver_id: Action identity selected for investigation.
        entity_id: Environment entity identifier to investigate.
        reflection: Concise public evidence summary for this action.
        standoff_distance: Optional desired separation distance.
        deadline_time: Absolute non-negative Mission time by which to reach standoff.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    required: dict[str, JsonScalar] = {"entity_id": entity_id}
    if standoff_distance is not None:
        required["standoff_distance"] = standoff_distance
    if deadline_time is not None:
        required["deadline_time"] = _deadline(deadline_time)
    context = _context(runtime)
    return _physical(
        context,
        tool_name="investigate",
        maneuver_id=maneuver_id,
        action="investigate",
        parameters=_parameters(required, extra_parameters),
        reflection=reflection,
    )


@tool(parse_docstring=True)
def ingest_perceptions(
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
) -> str:
    """Ingest every pending event perception as an ordered Bayesian update.

    Args:
        reflection: Concise public evidence summary for this perception batch.

    Returns:
        Completed batch status; new belief content enters later Hyper invocations.
    """

    from onr.application.bayesian_belief import create_risk_observation_event

    context = _context(runtime)
    if context.perception_batch_ingested:
        raise RuntimeError("perception batch tool is unavailable after success")
    if context.belief_service is None:
        raise RuntimeError("Maneuver heartbeat has no Bayesian belief service")
    perceptions = tuple(
        item
        for item in context.invocation.pending_perceptions
        if isinstance(item, EventObservation)
    )
    if not perceptions:
        raise RuntimeError("Maneuver heartbeat has no pending event perceptions")
    revisions: list[int] = []
    for perception in perceptions:
        event_id = f"risk.observed:{perception.observation_id}"
        get_event = getattr(context.belief_service.transport, "get_event", None)
        existing = (
            cast(TransportEvent | None, get_event(event_id))
            if callable(get_event)
            else None
        )
        if existing is None:
            sequence = context.belief_service.transport.next_event_sequence(
                context.belief_service.observation_topic,
                context.invocation.mission_id,
            )
            observation = RiskObservation(
                event_id=event_id,
                input_revision=sequence + 1,
                risk_type="event-risk",
                associations=(EntityAssociation(perception.entity_id, 1.0),),
                likelihood_given_risk=1.0 - perception.uncertainty_score,
                likelihood_given_safe=perception.uncertainty_score,
            )
            existing = context.belief_service.transport.publish_event(
                context.belief_service.observation_topic,
                create_risk_observation_event(
                    context.invocation.mission_id,
                    observation,
                    sequence=sequence,
                ),
            )
        input_revision = existing.payload.get("input_revision")
        last_revision = context.belief_service.manager.last_input_revision
        if (
            isinstance(input_revision, int)
            and not isinstance(input_revision, bool)
            and last_revision is not None
            and input_revision <= last_revision
        ):
            continue
        snapshot = context.belief_service.handle(existing)
        if snapshot is not None:
            revisions.append(snapshot.belief_revision)
    context.perception_batch_ingested = True
    result = {
        "status": "updated_complete",
        "event_count": len(perceptions),
        "belief_revisions": revisions,
        "reflection": reflection,
    }
    context.execution_record.append("ingest_perceptions", result, successful=True)
    return _canonical_json(result)


@tool(parse_docstring=True)
def communicate(
    recipient: str,
    kind: Literal["invoke", "query", "report", "replan"],
    message: str,
    reflection: str,
    evaluation_id: str | None = None,
    delivery_policy: Literal[
        "unrestricted", "once_per_state_entry"
    ] = "unrestricted",
    *,
    runtime: ToolRuntime[ManeuverToolContext],
) -> str:
    """Send a correlated message to one invocation-authorized agent recipient.

    Args:
        recipient: Exact recipient from available_recipients.
        kind: Correlated request kind.
        message: Concise factual request or report.
        reflection: Concise public evidence summary for this communication.
        evaluation_id: Stable current-state evaluation identity, when declared.
        delivery_policy: Unrestricted delivery or one evaluation per state entry.

    Returns:
        Canonical JSON containing the correlated response envelope.
    """

    context = _context(runtime)
    invocation = context.invocation
    if recipient not in invocation.available_recipients:
        raise ValueError("communication recipient is not available in this invocation")
    if context.communication_port is None:
        raise RuntimeError("Maneuver heartbeat has no communication port")
    sequence = len(context.execution_record.executions) + 1
    focused = _live_fsm_context(context)
    state_entry_revision = focused.state_entry_revision
    if delivery_policy == "once_per_state_entry":
        evaluation = focused.current_state_context.get("hyper_evaluation")
        if (
            not isinstance(evaluation, Mapping)
            or evaluation.get("evaluation_id") != evaluation_id
            or evaluation.get("delivery_policy") != delivery_policy
            or evaluation.get("kind") != kind
            or not isinstance(evaluation.get("reason"), str)
            or not cast(str, evaluation.get("reason")).strip()
        ):
            result = {
                "status": "rejected",
                "reason": (
                    "once-per-state-entry communication does not match the "
                    "current live hyper_evaluation"
                ),
                "current_state": focused.current_state,
                "state_entry_revision": state_entry_revision,
            }
            context.execution_record.append(
                "communicate", result, successful=False
            )
            return _canonical_json(result)
        message = cast(str, evaluation["reason"])
        message_id = (
            f"hyper-evaluation:{invocation.mission_id}:"
            f"{invocation.plan_revision}:"
            f"{state_entry_revision}:{evaluation_id}"
        )
    else:
        message_id = f"maneuver-message:{invocation.request_id}:{sequence}"
    payload: dict[str, object]
    choice: NonPhysicalChoice
    if kind == AgentMessageKind.REPLAN:
        source_revisions = (
            dict(invocation.planning_snapshot.source_revisions)
            if invocation.planning_snapshot is not None
            else {}
        )
        replan = ReplanRequest(
            request_id=message_id,
            mission_id=invocation.mission_id,
            reason=message,
            requester="maneuver-control",
            observed_plan_revision=invocation.plan_revision,
            source_revisions=source_revisions,
        )
        payload = {"message": message, "replan_request": replan.to_dict()}
        choice = NonPhysicalChoice.REPLAN
    else:
        payload = {"message": message}
        choice = {
            AgentMessageKind.QUERY: NonPhysicalChoice.QUERY,
            AgentMessageKind.REPORT: NonPhysicalChoice.REPORT,
            AgentMessageKind.INVOKE: NonPhysicalChoice.REPORT,
        }[AgentMessageKind(kind)]
    payload.update(
        {
            "delivery_policy": delivery_policy,
            "evaluation_id": evaluation_id,
            "state_entry_revision": state_entry_revision,
        }
    )
    request = AgentMessage(
        message_id=message_id,
        correlation_id=invocation.correlation_id,
        mission_id=invocation.mission_id,
        plan_revision=invocation.plan_revision,
        sender="maneuver-control",
        recipient=recipient,
        kind=kind,
        payload=payload,
    )
    outcome = context.communication_port.request(request)
    if (
        not isinstance(outcome, CommandOutcome)
        or outcome.command_id != message_id
        or outcome.correlation_id != invocation.correlation_id
        or outcome.mission_id != invocation.mission_id
        or str(outcome.status) not in {"completed", "already_in_flight"}
    ):
        raise RuntimeError(
            "agent communication did not return a successful correlation"
        )
    audit = ManeuverControlDecision(
        decision_id=message_id,
        mission_id=invocation.mission_id,
        plan_revision=invocation.plan_revision,
        choice=choice,
        payload={
            "recipient": recipient,
            "kind": kind,
            "message": message,
            "evaluation_id": evaluation_id,
            "delivery_policy": delivery_policy,
        },
    )
    result = outcome.to_dict()
    context.execution_record.append(
        "communicate", result, successful=True, decision=audit
    )
    return _canonical_json(result)


MANEUVER_OPERATIONAL_TOOLS = (
    set_transition_target,
    transition_fsm,
    navigate,
    takeoff,
    land,
    search_area,
    pursue,
    investigate,
    ingest_perceptions,
    communicate,
)


__all__ = [
    "MANEUVER_OPERATIONAL_TOOLS",
    "ManeuverHeartbeatExecutionRecord",
    "ManeuverToolContext",
    "ManeuverToolExecution",
    "communicate",
    "ingest_perceptions",
    "investigate",
    "land",
    "navigate",
    "pursue",
    "search_area",
    "set_transition_target",
    "takeoff",
    "transition_fsm",
]
