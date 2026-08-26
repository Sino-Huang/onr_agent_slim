"""Deterministic Maneuver Control application service."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Any, cast

from onr.application.transition_intents import TransitionIntentJournal
from onr.contracts.communication import AgentMessage, AgentMessageKind
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, ManeuverDecision
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    ManeuverHeartbeatCompletion,
    ManeuverInvocation,
    PhysicalAction,
)
from onr.contracts.planning import ManeuverIntent
from onr.contracts.transition_intent import ManeuverFSMContext
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    TransportEvent,
)
from onr.ports.operational_log import OperationalLog
from onr.ports.transport import Consumer, Transport


@dataclass(frozen=True, slots=True)
class ManeuverHeartbeatResult:
    """Transient result of one heartbeat and its queued command, when any."""

    decision: ManeuverControlDecision
    command: ManeuverCommand | None = None


DecisionProvider = Callable[
    [MissionSnapshot, FSMStatus, InvocationOverlay | None], object
]


class ManeuverControl:
    """Run live tool heartbeats and durably dispatch code-owned physical choices."""

    def __init__(
        self,
        transport: Transport,
        decision_provider: object,
        *,
        target_service: str = "maneuver-adapter",
        command_topic: str = "maneuver",
        command_protocol_version: int = 1,
        supported_actions: tuple[PhysicalAction | str, ...] = tuple(PhysicalAction),
        operational_log: OperationalLog | None = None,
        fsm_runner: object | None = None,
        belief_service: object | None = None,
        communication_port: object | None = None,
        transition_intents: TransitionIntentJournal | None = None,
    ) -> None:
        self.transport = transport
        self.decision_provider = decision_provider
        self.target_service = target_service
        if not isinstance(command_topic, str) or not command_topic.strip():
            raise ValueError("Maneuver Command topic must be non-empty")
        self.command_topic = command_topic
        if (
            isinstance(command_protocol_version, bool)
            or not isinstance(command_protocol_version, int)
            or command_protocol_version <= 0
        ):
            raise ValueError("Maneuver Command protocol version must be positive")
        self.command_protocol_version = command_protocol_version
        try:
            self.supported_actions = frozenset(
                PhysicalAction(item) for item in supported_actions
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("supported physical action is invalid") from exc
        if not self.supported_actions:
            raise ValueError("Maneuver Control requires a supported physical action")
        self.operational_log = operational_log
        self.fsm_runner = fsm_runner
        self.belief_service = belief_service
        self.communication_port = communication_port
        self.transition_intents = transition_intents or TransitionIntentJournal(
            transport
        )
        self._heartbeat_physical: dict[tuple[str, str, str], ManeuverCommand] = {}
        self._heartbeat_physical_lock = Lock()
        self._heartbeat_tool_lock = RLock()
        self._live_fsm_context: ManeuverFSMContext | None = None
        self._live_fsm_context_lock = Lock()
        self.last_execution_record: object | None = None

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
    ) -> ManeuverControlDecision:
        """Invoke the provider and apply the public contract validation gate."""

        self._validate_context(snapshot, status, overlay)
        try:
            raw = self._invoke_provider(snapshot, status, overlay)
            decision = self._coerce_decision(
                raw, snapshot.mission_id, status.plan_revision
            )
            self._validate_decision(decision, snapshot, status)
        except Exception as exc:
            self._emit(
                snapshot.mission_id,
                "error",
                "failed",
                {"operation": "decide", "error_type": type(exc).__name__},
            )
            raise
        self._emit(
            snapshot.mission_id,
            "control",
            "completed",
            {"operation": "decide", "plan_revision": status.plan_revision},
        )
        return decision

    @staticmethod
    def _validate_decision(
        decision: ManeuverControlDecision,
        snapshot: MissionSnapshot,
        status: FSMStatus,
    ) -> None:
        if (
            decision.mission_id != snapshot.mission_id
            or decision.mission_id != status.mission_id
        ):
            raise ValueError("maneuver decision mission ID does not match context")
        if decision.plan_revision != status.plan_revision:
            raise ValueError(
                "maneuver decision plan revision does not match FSM status"
            )
        if (
            decision.transition_event is not None
            and decision.transition_event not in status.enabled_events
        ):
            raise ValueError(
                "maneuver decision selected a transition that is not enabled"
            )

    def heartbeat(
        self,
        snapshot: MissionSnapshot | ManeuverInvocation,
        status: FSMStatus | None = None,
        overlay: InvocationOverlay | None = None,
        *,
        event_id: str | None = None,
    ) -> ManeuverHeartbeatResult | ManeuverHeartbeatCompletion:
        """Run a live tool heartbeat, or the retained deterministic legacy seam."""

        if isinstance(snapshot, ManeuverInvocation):
            if status is not None or overlay is not None or event_id is not None:
                raise ValueError(
                    "tool-driven Maneuver heartbeat accepts only its invocation"
                )
            return self._tool_heartbeat(snapshot)
        if status is None:
            raise TypeError("legacy Maneuver heartbeat requires FSMStatus")

        self._validate_context(snapshot, status, overlay)
        stored = (
            self._stored_invocation(event_id, snapshot.mission_id)
            if event_id is not None
            else None
        )
        if stored is None:
            decision = self.decide(snapshot, status, overlay)
            command = self._command_for_decision(decision, snapshot)
            if event_id is not None:
                self._publish_invocation_marker(
                    event_id, snapshot.mission_id, decision, command
                )
        else:
            decision, command = stored
            self._validate_decision(decision, snapshot, status)
        if command is None:
            self._emit(
                snapshot.mission_id,
                "heartbeat",
                "completed",
                {
                    "operation": "maneuver_heartbeat",
                    "plan_revision": status.plan_revision,
                },
            )
            return ManeuverHeartbeatResult(decision)
        self.handle_command(command)
        self._emit(
            snapshot.mission_id,
            "heartbeat",
            "completed",
            {
                "operation": "maneuver_heartbeat",
                "command_id": command.command_id,
                "plan_revision": status.plan_revision,
            },
        )
        return ManeuverHeartbeatResult(decision, command)

    def handle_agent_message(
        self, message: AgentMessage
    ) -> ManeuverHeartbeatCompletion:
        """Handle one correlated Hyper invocation synchronously."""

        if not isinstance(message, AgentMessage):
            raise TypeError("Maneuver Control communication requires AgentMessage")
        if (
            message.recipient != "maneuver-control"
            or message.kind is not AgentMessageKind.INVOKE
        ):
            raise ValueError("Maneuver Control accepts only correlated invoke messages")
        invocation = ManeuverInvocation.from_dict(message.payload)
        if (
            invocation.request_id != message.message_id
            or invocation.correlation_id != message.correlation_id
            or invocation.mission_id != message.mission_id
            or invocation.plan_revision != message.plan_revision
        ):
            raise ValueError(
                "Maneuver invocation does not match its communication envelope"
            )
        return cast(ManeuverHeartbeatCompletion, self.heartbeat(invocation))

    def _tool_heartbeat(
        self, invocation: ManeuverInvocation
    ) -> ManeuverHeartbeatCompletion:
        from onr.agents.maneuver_tools import ManeuverToolContext

        if self.fsm_runner is None:
            raise RuntimeError("tool-driven Maneuver Control has no live FSM Runner")
        provider = self.decision_provider
        heartbeat = getattr(provider, "heartbeat", None)
        if not callable(heartbeat):
            raise TypeError("tool-driven Maneuver provider must expose heartbeat")
        self._heartbeat_physical = {}
        self.update_live_fsm_context(invocation.fsm_context)
        context = ManeuverToolContext(
            invocation=invocation,
            fsm_runner=self.fsm_runner,
            command_dispatcher=self,
            belief_service=self.belief_service,
            communication_port=self.communication_port,
            transition_intents=self.transition_intents,
            operational_log=self.operational_log,
        )
        try:
            completion = heartbeat(invocation, context)
            if not isinstance(completion, ManeuverHeartbeatCompletion):
                raise TypeError(
                    "Maneuver provider did not return a heartbeat completion"
                )
        except Exception as exc:
            self.last_execution_record = context.execution_record
            self._emit(
                invocation.mission_id,
                "heartbeat",
                "failed",
                {
                    "operation": "maneuver_heartbeat",
                    "plan_revision": invocation.plan_revision,
                    "request_id": invocation.request_id,
                    "error_type": type(exc).__name__,
                    "successful_tool_calls": (
                        context.execution_record.successful_count
                    ),
                    "successful_transition_count": (
                        context.execution_record.successful_transition_count
                    ),
                    "initial_intent_id": (context.execution_record.initial_intent_id),
                },
            )
            raise
        self.last_execution_record = context.execution_record
        self._emit(
            invocation.mission_id,
            "heartbeat",
            "completed",
            {
                "operation": "maneuver_heartbeat",
                "plan_revision": invocation.plan_revision,
                "request_id": invocation.request_id,
                "successful_tool_calls": context.execution_record.successful_count,
                "successful_transition_count": (
                    context.execution_record.successful_transition_count
                ),
                "initial_intent_id": context.execution_record.initial_intent_id,
            },
        )
        return completion

    def update_live_fsm_context(self, context: ManeuverFSMContext) -> None:
        """Retain the latest focused context across copied heartbeat tool contexts."""

        if not isinstance(context, ManeuverFSMContext):
            raise TypeError("live Maneuver FSM context must be focused")
        with self._live_fsm_context_lock:
            self._live_fsm_context = context

    def current_maneuver_fsm_context(self) -> ManeuverFSMContext | None:
        with self._live_fsm_context_lock:
            return self._live_fsm_context

    @property
    def heartbeat_tool_lock(self) -> object:
        """Serialize live-FSM tool sections across copied DeepAgents contexts."""

        return self._heartbeat_tool_lock

    def dispatch_physical(
        self,
        invocation: ManeuverInvocation,
        decision: ManeuverControlDecision,
        *,
        sequence: int,
    ) -> tuple[ManeuverCommand, bool]:
        """Durably queue one code-owned physical choice."""

        if not isinstance(invocation, ManeuverInvocation):
            raise TypeError("physical dispatch requires ManeuverInvocation")
        if (
            not isinstance(decision, ManeuverControlDecision)
            or decision.physical_intent is None
            or decision.maneuver_id is None
        ):
            raise ValueError("physical dispatch requires a physical audit decision")
        if (
            decision.mission_id != invocation.mission_id
            or decision.plan_revision != invocation.plan_revision
        ):
            raise ValueError("physical decision identity does not match invocation")
        action = str(decision.physical_intent.action)
        identity = (invocation.request_id, decision.maneuver_id, action)
        with self._heartbeat_physical_lock:
            existing = self._heartbeat_physical.get(identity)
            if existing is not None:
                return existing, False
            action_value = PhysicalAction(action)
            if action_value not in self.supported_actions:
                raise ValueError(
                    "physical action is not supported by the environment profile"
                )
            command = ManeuverCommand(
                command_id=f"maneuver:{invocation.request_id}:{sequence}",
                correlation_id=invocation.correlation_id,
                mission_id=invocation.mission_id,
                plan_revision=invocation.plan_revision,
                maneuver_id=decision.maneuver_id,
                intent=decision.physical_intent,
                schema_version=self.command_protocol_version,
                mission_snapshot_id=(
                    f"{invocation.mission_id}:snapshot:"
                    f"{invocation.planning_snapshot.version}"
                    if invocation.planning_snapshot is not None
                    else None
                ),
            )
            already_queued = self._command_receipt(command.command_id) is not None
            self.handle_command(command)
            self._heartbeat_physical[identity] = command
            return command, not already_queued

    def handle_command(self, command: Command | ManeuverCommand) -> CommandReceipt:
        """Durably enqueue one idempotently identified Maneuver Command."""

        if (
            isinstance(command, Command)
            and command.target_service != self.target_service
        ):
            raise ValueError("maneuver command target service does not match profile")
        typed = (
            command
            if isinstance(command, ManeuverCommand)
            else ManeuverCommand.from_command(command, self.command_topic)
        )
        if typed.schema_version != self.command_protocol_version:
            raise ValueError("Maneuver Command protocol version does not match profile")
        if PhysicalAction(typed.action) not in self.supported_actions:
            raise ValueError(
                "physical action is not supported by the environment profile"
            )
        receipt = self.transport.send_command(
            typed.to_command(self.target_service, self.command_topic)
        )
        self._emit(
            typed.mission_id,
            "control",
            "queued",
            {"operation": "queue_command", "command_id": typed.command_id},
        )
        return receipt

    def _emit(
        self,
        mission_id: str,
        event_kind: str,
        outcome: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if self.operational_log is not None:
            self.operational_log.emit(
                mission_id, "maneuver-control", event_kind, outcome, details=details
            )

    handle = handle_command

    async def run_once(
        self, consumer_or_message: Consumer | object
    ) -> ManeuverHeartbeatResult | object | None:
        """Process one event or command delivery, acknowledging after success."""

        if hasattr(consumer_or_message, "receive"):
            consumer = cast(Consumer, consumer_or_message)
            delivery = consumer.receive()
            if delivery is None:
                return None
            try:
                result = self._handle_message(delivery.message)
            except Exception:
                delivery.nack()
                raise
            delivery.ack()
            return result
        return self._handle_message(consumer_or_message)

    def _handle_message(self, message: object) -> ManeuverHeartbeatResult | object:
        if isinstance(message, Command):
            return self.handle_command(message)
        if isinstance(message, CommandOutcome):
            return message
        if isinstance(message, TransportEvent):
            payload = message.payload
            snapshot = MissionSnapshot.from_dict(_mapping(payload, "snapshot"))
            status = FSMStatus.from_dict(_mapping(payload, "fsm_status"))
            return self.heartbeat(snapshot, status, event_id=message.event_id)
        if isinstance(message, tuple) and len(message) == 2:
            snapshot, status = message
            return self.heartbeat(snapshot, status)
        raise TypeError(
            "maneuver control message must be a TransportEvent, Command, "
            "or context pair"
        )

    def _stored_invocation(
        self, event_id: str, mission_id: str
    ) -> tuple[ManeuverControlDecision, ManeuverCommand | None] | None:
        marker = self.transport.latest_event(
            self._invocation_topic(event_id),
            mission_id,
            event_kind="maneuver-invocation",
        )
        if marker is None or marker.payload.get("input_event_id") != event_id:
            return None
        raw_decision = marker.payload.get("decision")
        if not isinstance(raw_decision, Mapping):
            raise TypeError("maneuver invocation marker decision is invalid")
        raw_command = marker.payload.get("command")
        if raw_command is not None and not isinstance(raw_command, Mapping):
            raise ValueError("maneuver invocation marker command is invalid")
        decision = ManeuverControlDecision.from_dict(raw_decision)
        command = (
            ManeuverCommand.from_dict(raw_command) if raw_command is not None else None
        )
        return decision, command

    def _publish_invocation_marker(
        self,
        event_id: str,
        mission_id: str,
        decision: ManeuverControlDecision,
        command: ManeuverCommand | None,
    ) -> None:
        topic = self._invocation_topic(event_id)
        marker = TransportEvent(
            schema_version=1,
            event_id=f"maneuver-invocation:{event_id}",
            mission_id=mission_id,
            sequence=self.transport.next_event_sequence(topic, mission_id),
            event_kind="maneuver-invocation",
            payload={
                "input_event_id": event_id,
                "decision": decision.to_dict(),
                "command": command.to_dict() if command is not None else None,
            },
        )
        self.transport.publish_event(topic, marker)

    @staticmethod
    def _invocation_topic(event_id: str) -> str:
        return f"maneuver-invocations/{event_id}"

    def _command_for_decision(
        self, decision: ManeuverControlDecision, snapshot: MissionSnapshot
    ) -> ManeuverCommand | None:
        if decision.physical_intent is None:
            return None
        command_id = _physical_command_id(
            decision.mission_id,
            decision.plan_revision,
            cast(str, decision.maneuver_id),
            decision.physical_intent,
        )
        return ManeuverCommand(
            command_id=command_id,
            correlation_id=command_id,
            mission_id=decision.mission_id,
            plan_revision=decision.plan_revision,
            maneuver_id=cast(str, decision.maneuver_id),
            intent=decision.physical_intent,
            schema_version=self.command_protocol_version,
            mission_snapshot_id=f"{snapshot.mission_id}:snapshot:{snapshot.version}",
        )

    def _command_receipt(self, command_id: str) -> CommandReceipt | None:
        get_receipt = getattr(self.transport, "get_command_receipt", None)
        if not callable(get_receipt):
            return None
        receipt = get_receipt(command_id)
        return receipt if isinstance(receipt, CommandReceipt) else None

    def _invoke_provider(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None,
    ) -> object:
        provider = self.decision_provider
        method = getattr(provider, "decide", None)
        if callable(method):
            return method(snapshot, status, overlay)
        method = getattr(provider, "invoke", None)
        if callable(method):
            return method(snapshot, status, overlay)
        if callable(provider):
            return provider(snapshot, status, overlay)
        raise TypeError("decision provider must be callable or expose decide/invoke")

    @staticmethod
    def _coerce_decision(
        raw: object, mission_id: str, plan_revision: int
    ) -> ManeuverControlDecision:
        if isinstance(raw, ManeuverControlDecision):
            return raw
        if isinstance(raw, ManeuverDecision):
            physical = raw.physical_maneuver
            if physical is not None:
                return ManeuverControlDecision(
                    raw.decision_id,
                    raw.mission_id,
                    plan_revision,
                    maneuver_id=raw.maneuver_id,
                    physical_maneuver=physical,
                    payload=raw.payload,
                )
            if raw.transition_event is not None:
                return ManeuverControlDecision(
                    raw.decision_id,
                    raw.mission_id,
                    plan_revision,
                    transition_event=raw.transition_event,
                )
        if isinstance(raw, Mapping):
            return ManeuverControlDecision.from_dict(raw)
        raise TypeError("decision provider did not return a Maneuver Decision")

    @staticmethod
    def _validate_context(
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None,
    ) -> None:
        if not isinstance(snapshot, MissionSnapshot) or not isinstance(
            status, FSMStatus
        ):
            raise TypeError("maneuver heartbeat requires MissionSnapshot and FSMStatus")
        if snapshot.mission_id != status.mission_id:
            raise ValueError("Mission Snapshot and FSM status mission IDs do not match")
        if snapshot.plan_revision != status.plan_revision:
            raise ValueError(
                "Mission Snapshot and FSM status plan revisions do not match"
            )
        if overlay is not None and overlay.mission_id != snapshot.mission_id:
            raise ValueError("invocation overlay mission ID does not match context")


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise TypeError(f"maneuver event {key} must be an object")
    return item


def _physical_command_id(
    mission_id: str,
    plan_revision: int,
    maneuver_id: str,
    intent: ManeuverIntent,
) -> str:
    canonical = json.dumps(
        intent.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"maneuver:{mission_id}:{plan_revision}:{maneuver_id}:{digest}"


def _current_environment_data(
    authority: object | None,
    fallback: Mapping[str, object],
) -> Mapping[str, object]:
    if authority is None:
        return fallback
    getter = getattr(authority, "current_environment_data", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
        raise TypeError("environment authority returned invalid current data")
    for name in ("latest_environment_event", "last_environment_event"):
        event = getattr(authority, name, None)
        payload = getattr(event, "payload", None)
        if isinstance(payload, Mapping):
            return payload
    return fallback


__all__ = [
    "DecisionProvider",
    "ManeuverControl",
    "ManeuverHeartbeatResult",
]
