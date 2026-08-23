"""Operational tools and opaque runtime context for Maneuver heartbeats."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Thread
from typing import Any, Literal, cast

from langchain.tools import ToolRuntime, tool

from onr.application.bayesian_belief import create_risk_observation_event
from onr.contracts.bayesian_belief import EntityAssociation, RiskObservation
from onr.contracts.communication import AgentMessage, AgentMessageKind
from onr.contracts.environment import EventObservation
from onr.contracts.fsm import FSMStatus, ManeuverDecision
from onr.contracts.hyper_agent import ReplanRequest
from onr.contracts.maneuver_control import (
    ManeuverControlDecision,
    ManeuverInvocation,
    NonPhysicalChoice,
)
from onr.contracts.planning import ManeuverIntent, ManeuverParameter
from onr.contracts.transport import CommandOutcome, TransportEvent

JsonScalar = str | int | float | bool | None


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
    """Per-heartbeat effects used to validate the final completion."""

    executions: list[ManeuverToolExecution] = field(default_factory=list)
    decisions: list[ManeuverControlDecision] = field(default_factory=list)

    @property
    def successful_count(self) -> int:
        return sum(item.successful for item in self.executions)

    def append(
        self,
        name: str,
        result: Mapping[str, object],
        *,
        successful: bool,
        decision: ManeuverControlDecision | None = None,
    ) -> None:
        self.executions.append(ManeuverToolExecution(name, successful, dict(result)))
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
    operational_log: Any = None
    # ToolRuntime asks Pydantic to serialize its context between graph steps.
    # Keep the audit record opaque like the service dependencies so immutable
    # MappingProxy payloads are never treated as model-visible dictionaries.
    execution_record: Any = field(default_factory=ManeuverHeartbeatExecutionRecord)
    perception_batch_ingested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.invocation, ManeuverInvocation):
            raise TypeError("Maneuver tools require a ManeuverInvocation")
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


def _candidate_result(status: FSMStatus) -> dict[str, object]:
    return {
        "current_state": status.active_state,
        "candidates": [item.to_dict() for item in status.transition_candidates],
    }


@tool(parse_docstring=True)
def transition_fsm(
    event: str,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
) -> str:
    """Apply an exact live FSM candidate with a current Maneuver decision.

    Args:
        event: Exact event name from the current live transition candidates.
        reflection: Concise public evidence summary for this transition choice.

    Returns:
        Canonical JSON containing rejection evidence or the updated live FSM status.
    """

    context = _context(runtime)
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
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    candidates = [item for item in status.transition_candidates if item.event == event]
    base = _candidate_result(status)
    if len(candidates) != 1:
        result = {
            "status": "rejected",
            "reason": "event is not an exact current transition candidate",
            **base,
        }
        context.execution_record.append("transition_fsm", result, successful=False)
        return _canonical_json(result)
    candidate = candidates[0]
    sequence = len(context.execution_record.executions) + 1
    decision_id = f"maneuver-transition:{context.invocation.request_id}:{sequence}"
    authorization = ManeuverDecision(
        decision_id=decision_id,
        mission_id=context.invocation.mission_id,
        transition_event=candidate.event,
        payload={"plan_revision": context.invocation.plan_revision},
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
        payload={"reflection": reflection},
    )
    result = {"status": "transitioned", "fsm_status": updated.to_dict()}
    context.execution_record.append(
        "transition_fsm", result, successful=True, decision=audit
    )
    return _canonical_json(result)


def _parameters(
    required: Mapping[str, JsonScalar],
    extra_parameters: Mapping[str, JsonScalar] | None,
) -> tuple[ManeuverParameter, ...]:
    values = dict(extra_parameters or {})
    values.update(required)
    return tuple(ManeuverParameter(name, value) for name, value in values.items())


def _physical(
    context: ManeuverToolContext,
    *,
    tool_name: str,
    maneuver_id: str,
    action: str,
    parameters: tuple[ManeuverParameter, ...],
    reflection: str,
) -> str:
    sequence = len(context.execution_record.executions) + 1
    decision = ManeuverControlDecision(
        decision_id=f"maneuver-action:{context.invocation.request_id}:{sequence}",
        mission_id=context.invocation.mission_id,
        plan_revision=context.invocation.plan_revision,
        maneuver_id=maneuver_id,
        physical_intent=ManeuverIntent(action, parameters),
        payload={"reflection": reflection},
    )
    command, outcome = context.command_dispatcher.dispatch_physical(
        context.invocation,
        decision,
        sequence=sequence,
    )
    if not isinstance(outcome, CommandOutcome) or str(outcome.status) not in {
        "accepted",
        "completed",
    }:
        raise RuntimeError("maneuver command dispatcher did not accept the action")
    result = {
        "status": "submitted",
        "command_id": command.command_id,
        "correlation_id": command.correlation_id,
        "maneuver_id": command.maneuver_id,
        "action": command.action,
    }
    context.execution_record.append(
        tool_name, result, successful=True, decision=decision
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
    observation_start: float | None = None,
    observation_duration: float | None = None,
    source_event_index: int | None = None,
    expected_observation_count: int | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit deadline-aware navigation and its correlated observation window.

    Args:
        maneuver_id: Action identity selected for this navigation.
        x: Target planar x coordinate.
        y: Target planar y coordinate.
        reflection: Concise public evidence summary for this action.
        z: Optional target altitude or depth.
        speed: Optional requested speed.
        deadline_time: Continuous Mission time by which the target must be reached.
        observation_start: Continuous Mission time at which sensing begins.
        observation_duration: Duration in seconds of the sensing window.
        source_event_index: Planner-correlated source event identity.
        expected_observation_count: Expected event observations in the window.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    required: dict[str, JsonScalar] = {"x": x, "y": y}
    if z is not None:
        required["z"] = z
    if speed is not None:
        required["speed"] = speed
    if deadline_time is not None:
        required["deadline_time"] = deadline_time
    if observation_start is not None:
        required["observation_start"] = observation_start
    if observation_duration is not None:
        required["observation_duration"] = observation_duration
    if source_event_index is not None:
        required["source_event_index"] = source_event_index
    if expected_observation_count is not None:
        required["expected_observation_count"] = expected_observation_count
    context = _context(runtime)
    return _physical(
        context,
        tool_name="navigate",
        maneuver_id=maneuver_id,
        action="navigate",
        parameters=_parameters(required, extra_parameters),
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
    area_id: str,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    altitude: float | None = None,
    speed: float | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit an area-search action.

    Args:
        maneuver_id: Action identity selected for the search.
        area_id: Environment area identifier to search.
        reflection: Concise public evidence summary for this action.
        altitude: Optional search altitude.
        speed: Optional requested speed.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    required: dict[str, JsonScalar] = {"area_id": area_id}
    if altitude is not None:
        required["altitude"] = altitude
    if speed is not None:
        required["speed"] = speed
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
    entity_id: str,
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
    entity_id: str,
    reflection: str,
    runtime: ToolRuntime[ManeuverToolContext],
    standoff_distance: float | None = None,
    extra_parameters: dict[str, JsonScalar] | None = None,
) -> str:
    """Submit an entity-investigation action.

    Args:
        maneuver_id: Action identity selected for investigation.
        entity_id: Environment entity identifier to investigate.
        reflection: Concise public evidence summary for this action.
        standoff_distance: Optional desired separation distance.
        extra_parameters: Additional JSON-scalar adapter-neutral parameters.
    """

    required: dict[str, JsonScalar] = {"entity_id": entity_id}
    if standoff_distance is not None:
        required["standoff_distance"] = standoff_distance
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
    runtime: ToolRuntime[ManeuverToolContext],
) -> str:
    """Send a correlated message to one invocation-authorized agent recipient.

    Args:
        recipient: Exact recipient from available_recipients.
        kind: Correlated request kind.
        message: Concise factual request or report.
        reflection: Concise public evidence summary for this communication.

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
        or str(outcome.status) != "completed"
    ):
        raise RuntimeError(
            "agent communication did not return a successful correlation"
        )
    audit = ManeuverControlDecision(
        decision_id=message_id,
        mission_id=invocation.mission_id,
        plan_revision=invocation.plan_revision,
        choice=choice,
        payload={"recipient": recipient, "kind": kind, "message": message},
    )
    result = outcome.to_dict()
    context.execution_record.append(
        "communicate", result, successful=True, decision=audit
    )
    return _canonical_json(result)


MANEUVER_OPERATIONAL_TOOLS = (
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
    "takeoff",
    "transition_fsm",
]
