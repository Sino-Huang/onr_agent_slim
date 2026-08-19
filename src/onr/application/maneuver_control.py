"""Deterministic Maneuver Control application service."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any, cast

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, ManeuverDecision
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
)
from onr.contracts.planning import ManeuverIntent, NormalizedPlan
from onr.contracts.transport import Command, CommandOutcome, CommandReceipt, TransportEvent
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
    """Validate Maneuver Decisions, emit one command, and submit each command once."""

    def __init__(
        self,
        transport: Transport,
        adapter: ManeuverAdapter,
        decision_provider: object,
        *,
        target_service: str = "maneuver-adapter",
        submission_topic: str = "maneuver-submissions",
        operational_log: OperationalLog | None = None,
    ) -> None:
        self.transport = transport
        self.adapter = adapter
        self.decision_provider = decision_provider
        self.target_service = target_service
        self.submission_topic = submission_topic
        self.operational_log = operational_log
        self._submitted: dict[str, CommandOutcome] = {}

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
        *,
        plan: NormalizedPlan | None = None,
    ) -> ManeuverControlDecision:
        """Invoke the provider and apply the public contract validation gate."""

        self._validate_context(snapshot, status, overlay)
        try:
            raw = self._invoke_provider(snapshot, status, overlay)
            decision = self._coerce_decision(raw, snapshot.mission_id, status.plan_revision)
            self._validate_decision(decision, snapshot, status, plan)
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
        plan: NormalizedPlan | None,
    ) -> None:
        if decision.mission_id != snapshot.mission_id or decision.mission_id != status.mission_id:
            raise ValueError("maneuver decision mission ID does not match context")
        if decision.plan_revision != status.plan_revision:
            raise ValueError("maneuver decision plan revision does not match FSM status")
        if decision.transition_event is not None and decision.transition_event not in status.enabled_events:
            raise ValueError("maneuver decision selected a transition that is not enabled")
        ManeuverControl._validate_physical_context(decision, snapshot, status, plan)

    def heartbeat(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
        *,
        event_id: str | None = None,
        plan: NormalizedPlan | None = None,
    ) -> ManeuverHeartbeatResult:
        """Run one stateless heartbeat and publish at most one physical command."""

        self._validate_context(snapshot, status, overlay)
        stored = self._stored_invocation(event_id, snapshot.mission_id) if event_id is not None else None
        if stored is None:
            decision = self.decide(snapshot, status, overlay, plan=plan)
            command = self._command_for_decision(decision, snapshot)
            if event_id is not None:
                self._publish_invocation_marker(event_id, snapshot.mission_id, decision, command)
        else:
            decision, command = stored
            self._validate_decision(decision, snapshot, status, plan)
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

    @staticmethod
    def _validate_physical_context(
        decision: ManeuverControlDecision,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        plan: NormalizedPlan | None,
    ) -> None:
        if decision.physical_intent is None:
            return
        maneuver_id = decision.maneuver_id
        enabled_maneuvers = {
            event.removeprefix("advance:")
            for event in status.enabled_events
            if event.startswith("advance:") and event.removeprefix("advance:")
        }
        if maneuver_id not in enabled_maneuvers:
            raise ValueError("physical maneuver is not enabled by the current FSM status")
        if plan is None:
            return
        if plan.mission_id != snapshot.mission_id or plan.plan_revision != status.plan_revision:
            raise ValueError("normalized plan does not match the current maneuver context")
        planned = next(
            (maneuver for maneuver in plan.maneuvers if maneuver.maneuver_id == maneuver_id),
            None,
        )
        if planned is None or planned.intent != decision.physical_intent:
            raise ValueError("physical maneuver does not match the normalized plan")

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


__all__ = [
    "ManeuverControl",
    "ManeuverHeartbeatResult",
    "DecisionProvider",
]
