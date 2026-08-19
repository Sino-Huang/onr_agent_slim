"""Application seam for delivering one planning command to a planner."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from onr.contracts.planning import NormalizedPlan
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    NormalizedPlanTransportEvent,
    normalized_plan_transport_event_to_wire,
    create_normalized_plan_transport_event,
)
from onr.ports.operational_log import OperationalLog


class PlanningCommandHandler:
    """Execute each command identity once and publish its correlated result."""

    def __init__(
        self,
        transport: Any,
        planner: Callable[[Command], object] | object,
        *,
        topic: str = "normalized-plans",
        operational_log: OperationalLog | None = None,
    ) -> None:
        self._transport = transport
        self._planner = planner
        self._topic = topic
        self._operational_log = operational_log
        self._completed: dict[str, CommandOutcome] = {}

    def handle(self, command: Command) -> CommandOutcome:
        if not isinstance(command, Command):
            raise TypeError("planning command handler requires a Command")
        previous = self._completed.get(command.command_id)
        if previous is None and hasattr(self._transport, "get_command_outcome"):
            previous = self._transport.get_command_outcome(command.command_id)
        if previous is not None:
            self._completed[command.command_id] = previous
            return previous

        try:
            result = self._execute_planner(command)
            normalized_plan = self._normalized_plan(result)
            if normalized_plan.mission_spec.mission_id != command.mission_id:
                raise ValueError("planner result mission ID does not match command mission ID")
            event_id = f"normalized-plan:{command.command_id}"
            sequence = command.payload.get("sequence")
            if sequence is None:
                sequence = self._transport.next_event_sequence(self._topic, command.mission_id)
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError("planning command sequence must be a non-negative integer")
            event = create_normalized_plan_transport_event(
                normalized_plan,
                event_id=event_id,
                sequence=sequence,
            )
            self._transport.publish_event(
                self._topic,
                normalized_plan_transport_event_to_wire(
                    event,
                    correlation_id=command.correlation_id,
                ),
            )
            outcome = CommandOutcome(
                schema_version=command.schema_version,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                mission_id=command.mission_id,
                status="completed",
                payload={"event_id": event_id},
            )
            self._emit(
                command.mission_id,
                "solver",
                "completed",
                {"operation": "planning_command", "command_id": command.command_id},
            )
            self._emit(
                command.mission_id,
                "planning",
                "completed",
                {"operation": "planning_command", "command_id": command.command_id},
            )
        except Exception as exc:
            outcome = CommandOutcome(
                schema_version=command.schema_version,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                mission_id=command.mission_id,
                status="failed",
                payload={"error": str(exc)},
            )
            self._emit(
                command.mission_id,
                "planning",
                "failed",
                {"operation": "planning_command", "command_id": command.command_id},
            )
            self._emit(
                command.mission_id,
                "error",
                "failed",
                {"operation": "planning_command", "error_type": type(exc).__name__},
            )
        self._transport.publish_outcome(outcome)
        self._completed[command.command_id] = outcome
        return outcome

    def _emit(
        self,
        mission_id: str,
        event_kind: str,
        outcome: str,
        details: dict[str, object],
    ) -> None:
        if self._operational_log is not None:
            self._operational_log.emit(
                mission_id, "planning-command-handler", event_kind, outcome, details=details
            )

    handle_command = handle

    def run_once(self, consumer: Any) -> CommandOutcome | None:
        delivery = consumer.receive()
        if delivery is None:
            return None
        if not isinstance(delivery.message, Command):
            delivery.ack()
            return None
        outcome = self.handle(delivery.message)
        if outcome.status == "failed":
            delivery.nack()
        else:
            delivery.ack()
        return outcome

    def _execute_planner(self, command: Command) -> object:
        if callable(self._planner):
            return self._planner(command)
        plan_method = getattr(self._planner, "plan", None)
        if callable(plan_method):
            return plan_method(command)
        raise TypeError("planner must be callable or expose plan")

    @staticmethod
    def _normalized_plan(result: object) -> NormalizedPlan:
        if isinstance(result, NormalizedPlan):
            return result
        if isinstance(result, NormalizedPlanTransportEvent):
            return result.payload.normalized_plan
        normalized_plan = getattr(result, "normalized_plan", None)
        if isinstance(normalized_plan, NormalizedPlan):
            return normalized_plan
        raise TypeError("planner did not return a NormalizedPlan")
