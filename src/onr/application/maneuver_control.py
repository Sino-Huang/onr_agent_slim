"""Deterministic Maneuver Control application service."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Thread
from typing import Any, cast

from onr.contracts.bayesian_belief import BayesianBeliefSnapshot
from onr.contracts.communication import AgentMessage, AgentMessageKind
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, ManeuverDecision
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    ManeuverHeartbeatCompletion,
    ManeuverInvocation,
)
from onr.contracts.planning import ManeuverIntent
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    TransportEvent,
)
from onr.ports.maneuver import ManeuverAdapter
from onr.ports.operational_log import OperationalLog
from onr.ports.transport import Consumer, Transport


@dataclass(frozen=True, slots=True)
class ManeuverHeartbeatResult:
    """Transient result of one heartbeat; only ``command`` is transportable."""

    decision: ManeuverControlDecision
    command: ManeuverCommand | None = None
    receipt: CommandReceipt | None = None


DecisionProvider = Callable[[MissionSnapshot, FSMStatus, InvocationOverlay | None], object]


class ManeuverControl:
    """Run live tool heartbeats and durably dispatch code-owned physical choices."""

    def __init__(
        self,
        transport: Transport,
        adapter: ManeuverAdapter,
        decision_provider: object,
        *,
        target_service: str = "maneuver-adapter",
        submission_topic: str = "maneuver-submissions",
        operational_log: OperationalLog | None = None,
        fsm_runner: object | None = None,
        environment_authority: object | None = None,
        belief_service: object | None = None,
        communication_port: object | None = None,
    ) -> None:
        self.transport = transport
        self.adapter = adapter
        self.decision_provider = decision_provider
        self.target_service = target_service
        self.submission_topic = submission_topic
        self.operational_log = operational_log
        self.fsm_runner = fsm_runner
        self.environment_authority = environment_authority
        self.belief_service = belief_service
        self.communication_port = communication_port
        self._submitted: dict[str, CommandOutcome] = {}
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
            decision = self._coerce_decision(raw, snapshot.mission_id, status.plan_revision)
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
        if decision.mission_id != snapshot.mission_id or decision.mission_id != status.mission_id:
            raise ValueError("maneuver decision mission ID does not match context")
        if decision.plan_revision != status.plan_revision:
            raise ValueError("maneuver decision plan revision does not match FSM status")
        if decision.transition_event is not None and decision.transition_event not in status.enabled_events:
            raise ValueError("maneuver decision selected a transition that is not enabled")

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
                raise ValueError("tool-driven Maneuver heartbeat accepts only its invocation")
            return self._tool_heartbeat(self._live_invocation(snapshot))
        if status is None:
            raise TypeError("legacy Maneuver heartbeat requires FSMStatus")

        self._validate_context(snapshot, status, overlay)
        stored = self._stored_invocation(event_id, snapshot.mission_id) if event_id is not None else None
        if stored is None:
            decision = self.decide(snapshot, status, overlay)
            command = self._command_for_decision(decision, snapshot)
            if event_id is not None:
                self._publish_invocation_marker(event_id, snapshot.mission_id, decision, command)
        else:
            decision, command = stored
            self._validate_decision(decision, snapshot, status)
        if command is None:
            self._emit(
                snapshot.mission_id,
                "heartbeat",
                "completed",
                {"operation": "maneuver_heartbeat", "plan_revision": status.plan_revision},
            )
            return ManeuverHeartbeatResult(decision)
        receipt = self.transport.send_command(command.to_command(self.target_service))
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
        return ManeuverHeartbeatResult(decision, command, receipt)

    def handle_agent_message(self, message: AgentMessage) -> ManeuverHeartbeatCompletion:
        """Handle one correlated Hyper invocation synchronously."""

        if not isinstance(message, AgentMessage):
            raise TypeError("Maneuver Control communication requires AgentMessage")
        if message.recipient != "maneuver-control" or message.kind is not AgentMessageKind.INVOKE:
            raise ValueError("Maneuver Control accepts only correlated invoke messages")
        invocation = ManeuverInvocation.from_dict(message.payload)
        if (
            invocation.request_id != message.message_id
            or invocation.correlation_id != message.correlation_id
            or invocation.mission_id != message.mission_id
            or invocation.plan_revision != message.plan_revision
        ):
            raise ValueError("Maneuver invocation does not match its communication envelope")
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
        context = ManeuverToolContext(
            invocation=invocation,
            fsm_runner=self.fsm_runner,
            command_dispatcher=self,
            belief_service=self.belief_service,
            communication_port=self.communication_port,
            operational_log=self.operational_log,
        )
        completion = heartbeat(invocation, context)
        if not isinstance(completion, ManeuverHeartbeatCompletion):
            raise TypeError("Maneuver provider did not return a heartbeat completion")
        self.last_execution_record = context.execution_record
        self._emit(
            invocation.mission_id,
            "heartbeat",
            str(completion.outcome),
            {
                "operation": "maneuver_heartbeat",
                "plan_revision": invocation.plan_revision,
                "request_id": invocation.request_id,
                "successful_tool_calls": context.execution_record.successful_count,
            },
        )
        return completion

    def _live_invocation(self, invocation: ManeuverInvocation) -> ManeuverInvocation:
        if self.fsm_runner is None:
            return invocation
        status = _run_sync(cast(Any, self.fsm_runner).status())
        if not isinstance(status, FSMStatus):
            raise TypeError("live FSM Runner status did not return FSMStatus")
        environment = _current_environment_data(
            self.environment_authority, invocation.environment_data
        )
        belief = invocation.belief_snapshot
        if self.belief_service is not None:
            loader = getattr(self.belief_service, "load_current_snapshot", None)
            if callable(loader):
                current_belief = loader()
                if current_belief is not None and not isinstance(
                    current_belief, BayesianBeliefSnapshot
                ):
                    raise TypeError("belief service returned invalid current snapshot")
                if current_belief is not None:
                    belief = current_belief
        recipients = invocation.available_recipients
        if self.communication_port is not None:
            available = getattr(self.communication_port, "available_recipients", None)
            if callable(available):
                recipients = cast(
                    tuple[str, ...],
                    tuple(cast(Any, available("maneuver-control"))),
                )
        return ManeuverInvocation(
            request_id=invocation.request_id,
            correlation_id=invocation.correlation_id,
            mission_id=invocation.mission_id,
            plan_revision=invocation.plan_revision,
            statechart_reference=invocation.statechart_reference,
            fsm_status=status,
            environment_data=environment,
            belief_snapshot=belief,
            available_recipients=recipients,
            planning_snapshot=invocation.planning_snapshot,
        )

    def dispatch_physical(
        self,
        invocation: ManeuverInvocation,
        decision: ManeuverControlDecision,
        *,
        sequence: int,
    ) -> tuple[ManeuverCommand, CommandOutcome]:
        """Submit one code-owned physical choice without plan-action equality gates."""

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
        command = ManeuverCommand(
            command_id=f"maneuver:{invocation.request_id}:{sequence}",
            correlation_id=invocation.correlation_id,
            mission_id=invocation.mission_id,
            plan_revision=invocation.plan_revision,
            maneuver_id=decision.maneuver_id,
            intent=decision.physical_intent,
            mission_snapshot_id=(
                f"{invocation.mission_id}:snapshot:{invocation.planning_snapshot.version}"
                if invocation.planning_snapshot is not None
                else None
            ),
        )
        return command, self.handle_command(command)

    def handle_command(self, command: Command | ManeuverCommand) -> CommandOutcome:
        """Submit a command at most once and return its correlated outcome."""

        if isinstance(command, Command) and command.target_service != self.target_service:
            raise ValueError("maneuver command target service does not match adapter")
        typed = command if isinstance(command, ManeuverCommand) else ManeuverCommand.from_command(command)
        generic = typed.to_command(self.target_service)
        self.transport.send_command(generic)
        if typed.command_id in self._submitted:
            return self._submitted[typed.command_id]
        existing = self._command_outcome(typed.command_id)
        if existing is not None:
            self._submitted[typed.command_id] = existing
            return existing
        intent_marker = self._submission_intent_marker(typed)
        if intent_marker is not None:
            outcome = self._unknown_submission_outcome(typed)
            self.transport.publish_outcome(outcome)
            self._submitted[typed.command_id] = outcome
            return outcome
        marker = self._submission_marker(typed)
        if marker is not None:
            outcome = self._accepted_outcome(typed)
            self.transport.publish_outcome(outcome)
            self._submitted[typed.command_id] = outcome
            return outcome
        self.transport.publish_event(
            self._submission_intent_topic_for(typed.command_id),
            TransportEvent(
                schema_version=typed.schema_version,
                event_id=f"maneuver-submission-intent:{typed.command_id}",
                mission_id=typed.mission_id,
                sequence=1,
                event_kind="maneuver-submission-intent",
                payload={
                    "command_id": typed.command_id,
                    "mission_snapshot_id": typed.mission_snapshot_id,
                },
            ),
        )
        try:
            self.adapter.submit(typed)
        except Exception as exc:
            outcome = self._failed_outcome(typed, exc)
            self.transport.publish_outcome(outcome)
            self._submitted[typed.command_id] = outcome
            self._emit(
                typed.mission_id,
                "error",
                "failed",
                {"operation": "adapter_submit", "error_type": type(exc).__name__},
            )
            raise
        self.transport.publish_event(
            self.submission_topic_for(typed.command_id),
            TransportEvent(
                schema_version=typed.schema_version,
                event_id=f"maneuver-submitted:{typed.command_id}",
                mission_id=typed.mission_id,
                sequence=0,
                event_kind="maneuver-submitted",
                payload={
                    "command_id": typed.command_id,
                    "mission_snapshot_id": typed.mission_snapshot_id,
                },
            ),
        )
        outcome = self._accepted_outcome(typed)
        self.transport.publish_outcome(outcome)
        self._submitted[typed.command_id] = outcome
        self._emit(
            typed.mission_id,
            "control",
            outcome.status,
            {"operation": "adapter_submit", "command_id": typed.command_id},
        )
        return outcome

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

    def submission_topic_for(self, command_id: str) -> str:
        return f"{self.submission_topic}/{command_id}"

    def _submission_intent_topic_for(self, command_id: str) -> str:
        return f"{self.submission_topic}-intents/{command_id}"

    async def run_once(self, consumer_or_message: Consumer | object) -> ManeuverHeartbeatResult | object | None:
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
            if message.event_kind == "maneuver-submitted":
                return message
            payload = message.payload
            snapshot = MissionSnapshot.from_dict(_mapping(payload, "snapshot"))
            status = FSMStatus.from_dict(_mapping(payload, "fsm_status"))
            return self.heartbeat(snapshot, status, event_id=message.event_id)
        if isinstance(message, tuple) and len(message) == 2:
            snapshot, status = message
            return self.heartbeat(snapshot, status)
        raise TypeError("maneuver control message must be a TransportEvent, Command, or context pair")

    def _submission_marker(self, command: ManeuverCommand) -> TransportEvent | None:
        latest = self.transport.latest_event(
            self.submission_topic_for(command.command_id),
            command.mission_id,
            event_kind="maneuver-submitted",
        )
        if latest is None or latest.payload.get("command_id") != command.command_id:
            return None
        return latest

    def _submission_intent_marker(self, command: ManeuverCommand) -> TransportEvent | None:
        latest = self.transport.latest_event(
            self._submission_intent_topic_for(command.command_id),
            command.mission_id,
            event_kind="maneuver-submission-intent",
        )
        if latest is None or latest.payload.get("command_id") != command.command_id:
            return None
        return latest

    def _stored_invocation(
        self, event_id: str, mission_id: str
    ) -> tuple[ManeuverControlDecision, ManeuverCommand | None] | None:
        marker = self.transport.latest_event(
            self._invocation_topic(event_id), mission_id, event_kind="maneuver-invocation"
        )
        if marker is None or marker.payload.get("input_event_id") != event_id:
            return None
        raw_decision = marker.payload.get("decision")
        if not isinstance(raw_decision, Mapping):
            raise ValueError("maneuver invocation marker decision is invalid")
        raw_command = marker.payload.get("command")
        if raw_command is not None and not isinstance(raw_command, Mapping):
            raise ValueError("maneuver invocation marker command is invalid")
        decision = ManeuverControlDecision.from_dict(raw_decision)
        command = ManeuverCommand.from_dict(raw_command) if raw_command is not None else None
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

    @staticmethod
    def _command_for_decision(
        decision: ManeuverControlDecision, snapshot: MissionSnapshot
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
            mission_snapshot_id=f"{snapshot.mission_id}:snapshot:{snapshot.version}",
        )

    def _command_outcome(self, command_id: str) -> CommandOutcome | None:
        get_outcome = getattr(self.transport, "get_command_outcome", None)
        if not callable(get_outcome):
            return None
        outcome = get_outcome(command_id)
        return outcome if isinstance(outcome, CommandOutcome) else None

    @staticmethod
    def _accepted_outcome(command: ManeuverCommand) -> CommandOutcome:
        return CommandOutcome(
            schema_version=command.schema_version,
            command_id=command.command_id,
            correlation_id=command.correlation_id,
            mission_id=command.mission_id,
            status="accepted",
            payload={
                "adapter_submission": "accepted",
                "source": "maneuver-adapter-transport",
            },
        )

    @staticmethod
    def _unknown_submission_outcome(command: ManeuverCommand) -> CommandOutcome:
        return CommandOutcome(
            schema_version=command.schema_version,
            command_id=command.command_id,
            correlation_id=command.correlation_id,
            mission_id=command.mission_id,
            status="failed",
            payload={
                "adapter_submission": "unknown",
                "error": "prior adapter submission outcome is unknown; command will not be submitted again",
                "source": "maneuver-adapter-transport",
            },
        )

    @staticmethod
    def _failed_outcome(command: ManeuverCommand, error: Exception) -> CommandOutcome:
        return CommandOutcome(
            schema_version=command.schema_version,
            command_id=command.command_id,
            correlation_id=command.correlation_id,
            mission_id=command.mission_id,
            status="failed",
            payload={
                "adapter_submission": "failed",
                "error": str(error),
                "source": "maneuver-adapter-transport",
            },
        )

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
    def _coerce_decision(raw: object, mission_id: str, plan_revision: int) -> ManeuverControlDecision:
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
                    raw.decision_id, raw.mission_id, plan_revision, transition_event=raw.transition_event
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
        if not isinstance(snapshot, MissionSnapshot) or not isinstance(status, FSMStatus):
            raise TypeError("maneuver heartbeat requires MissionSnapshot and FSMStatus")
        if snapshot.mission_id != status.mission_id:
            raise ValueError("Mission Snapshot and FSM status mission IDs do not match")
        if snapshot.plan_revision != status.plan_revision:
            raise ValueError("Mission Snapshot and FSM status plan revisions do not match")
        if overlay is not None and overlay.mission_id != snapshot.mission_id:
            raise ValueError("invocation overlay mission ID does not match context")


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"maneuver event {key} must be an object")
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


def _run_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: list[object] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except Exception as exc:
            failure.append(exc)

    thread = Thread(target=target, name="maneuver-control-fsm-adapter")
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


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
