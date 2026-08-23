"""Pure asynchronous-facing Statechart activation and execution service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Protocol, cast

from onr.contracts.fsm import (
    FSMExecutionRecord,
    FSMStatus,
    ManeuverDecision,
    Statechart,
    TransitionCandidate,
)
from onr.contracts.transport import TransportEvent
from onr.ports.fsm import (
    FSMStateStore,
    FSMTransport,
    RunningStateMachine,
    StateMachineFactory,
)
from onr.ports.operational_log import OperationalLog
from onr.ports.transport import Consumer, Delivery, Subscription


class _Receivable(Protocol):
    def receive(self) -> Delivery | None: ...


def _chart_from_message(message: object) -> Statechart:
    if isinstance(message, Statechart):
        return message
    if isinstance(message, TransportEvent) and message.event_kind == "statechart":
        return Statechart.from_dict(message.payload)
    raise TypeError("FSM Runner requires a Statechart or statechart event")


@dataclass
class InMemoryFSMStateStore:
    """Small JSON-backed store useful for application tests and composition."""

    statechart_json: str | None = None
    execution_record_json: str | None = None

    def load_statechart(self) -> Statechart | None:
        return Statechart.from_json(self.statechart_json) if self.statechart_json else None

    def load_execution_record(self) -> FSMExecutionRecord | None:
        return (
            FSMExecutionRecord.from_json(self.execution_record_json)
            if self.execution_record_json
            else None
        )

    def save_statechart(self, statechart: Statechart) -> None:
        self.statechart_json = statechart.to_canonical_json()

    def save_execution_record(self, record: FSMExecutionRecord) -> None:
        self.execution_record_json = record.to_canonical_json()


class FSMRunner:
    """Activate Statecharts and apply candidates authorized by Maneuver Control."""

    def __init__(
        self,
        transport: FSMTransport,
        *,
        store: FSMStateStore | None = None,
        status_topic: str = "fsm-status",
        subscription: Subscription | None = None,
        operational_log: OperationalLog | None = None,
        machine_factory: StateMachineFactory | None = None,
    ) -> None:
        self.transport = transport
        self.store = store or InMemoryFSMStateStore()
        self.status_topic = status_topic
        self.subscription = subscription
        self.operational_log = operational_log
        self.machine_factory = machine_factory
        self._chart = self.store.load_statechart()
        self._record = self.store.load_execution_record()
        if (self._chart is None) != (self._record is None):
            raise RuntimeError("FSM Statechart and Execution Record must be persisted together")
        if self._chart is not None and self._record is not None:
            if self._chart.mission_id != self._record.mission_id:
                raise RuntimeError("persisted FSM mission IDs do not match")
            if self._chart.plan_revision != self._record.plan_revision:
                raise RuntimeError("persisted FSM plan revisions do not match")
            if self._chart.statechart_revision != self._record.statechart_revision:
                raise RuntimeError("persisted FSM Statechart revisions do not match")
            configuration = self._record.active_configuration
            if (
                not configuration
                or self._record.active_state not in configuration
                or any(state not in self._chart.states for state in configuration)
                or self._record.active_state not in self._chart.states
            ):
                raise RuntimeError("persisted FSM configuration references undeclared state")
            if (
                self._record.superseded_plan_revision is not None
                and self._record.superseded_plan_revision >= self._record.plan_revision
            ):
                raise RuntimeError("persisted FSM superseded revision is not older than active plan")
        if self.subscription is not None and self._chart is not None and self.subscription.mission_id != self._chart.mission_id:
            raise ValueError("FSM subscription mission ID does not match persisted state")
        if self.subscription is None and self._chart is not None:
            self.subscription = self.subscription_for(self._chart.mission_id)
        self._superseded_plan_revision: int | None = (
            self._record.superseded_plan_revision if self._record else None
        )
        self._lock = asyncio.Lock()
        self._machine: RunningStateMachine | None = None
        if (
            self.machine_factory is not None
            and self._chart is not None
            and self._record is not None
        ):
            self._machine = self.machine_factory.build(
                self._chart, start_state=self._record.active_state
            )

    @staticmethod
    def subscription_for(
        mission_id: str,
        *,
        service_id: str = "fsm-runner",
        topic: str = "normalized-plans",
    ) -> Subscription:
        return Subscription(service_id=service_id, mission_id=mission_id, topic=topic)

    async def handle(self, message: object) -> FSMStatus:
        """Activate a Statechart transport event."""

        async with self._lock:
            chart = _chart_from_message(message)
            if self.subscription is None:
                self.subscription = self.subscription_for(chart.mission_id)
            elif self.subscription.mission_id != chart.mission_id:
                raise ValueError("FSM subscription mission ID does not match Statechart")
            if self._chart is not None:
                if chart.plan_revision < self._chart.plan_revision:
                    raise ValueError("FSM Runner cannot regress to an older plan revision")
                if chart.plan_revision == self._chart.plan_revision:
                    if chart != self._chart:
                        raise ValueError("same plan revision has different Statechart content")
                    return await self._publish_status("updated", publish=False)
                self._superseded_plan_revision = self._chart.plan_revision
            self._chart = chart
            self._record = FSMExecutionRecord(
                mission_id=chart.mission_id,
                plan_revision=chart.plan_revision,
                statechart_revision=chart.plan_revision,
                active_state=chart.entry_state,
                active_configuration=(chart.entry_state,),
                superseded_plan_revision=self._superseded_plan_revision,
                record_revision=(self._record.record_revision + 1 if self._record else 1),
            )
            self.store.save_statechart(chart)
            self.store.save_execution_record(self._record)
            if self.machine_factory is not None:
                self._machine = self.machine_factory.build(chart)
            return await self._publish_status(
                "superseded" if self._superseded_plan_revision is not None else "initialized"
            )

    async def activate(self, message: object) -> FSMStatus:
        return await self.handle(message)

    async def run_once(self, consumer_or_message: Consumer | object) -> FSMStatus | None:
        """Process one transport delivery, acknowledging only after activation."""

        if hasattr(consumer_or_message, "receive"):
            receiver = cast(_Receivable, consumer_or_message)
            while True:
                delivery = receiver.receive()
                if delivery is None:
                    return None
                message = delivery.message
                if (
                    isinstance(message, TransportEvent)
                    and message.event_kind != "statechart"
                ):
                    delivery.ack()
                    continue
                try:
                    result = await self.handle(message)
                except Exception:
                    delivery.nack()
                    if self.operational_log is not None:
                        mission_id = (
                            self.subscription.mission_id
                            if self.subscription is not None
                            else "unknown"
                        )
                        self.operational_log.emit(
                            mission_id,
                            "fsm-runner",
                            "error",
                            "failed",
                            details={
                                "operation": "run_once",
                                "error_type": "fsm_error",
                            },
                        )
                    raise
                delivery.ack()
                return result
        return await self.handle(consumer_or_message)

    async def apply(
        self,
        candidate: TransitionCandidate,
        decision: ManeuverDecision,
    ) -> FSMStatus:
        """Apply an exact current candidate with a current Maneuver decision."""

        async with self._lock:
            if self._chart is None or self._record is None:
                raise RuntimeError("FSM Runner is not initialized")
            current = next(
                (
                    item
                    for item in self._chart.transitions
                    if item.source == self._record.active_state
                    and item.event == candidate.event
                ),
                None,
            )
            expected_candidate = (
                TransitionCandidate(
                    event=current.event,
                    source=current.source,
                    target=current.target,
                    transition_context=current.context,
                    source_state_context=self._chart.context_for(current.source),
                    target_state_context=self._chart.context_for(current.target),
                )
                if current is not None
                else None
            )
            if current is None or candidate != expected_candidate:
                return await self._publish_status("unchanged")
            if not self._decision_authorizes(current.event, decision):
                return await self._publish_status("unchanged")
            event_identity = decision.event_id
            if event_identity in self._record.applied_event_identities:
                return await self._publish_status("unchanged")
            if self._machine is not None:
                self._machine.send(current.event)
                if self._machine.current_state != current.target:
                    raise RuntimeError(
                        "python-statemachine state does not match the Statechart transition"
                    )
            self._record = replace(
                self._record,
                record_revision=self._record.record_revision + 1,
                active_state=current.target,
                active_configuration=(current.target,),
                last_applied_event=current.event,
                last_applied_event_identity=event_identity,
                applied_event_identities=self._record.applied_event_identities + (event_identity,),
                transition_history=self._record.transition_history + (current.event,),
            )
            self.store.save_execution_record(self._record)
            return await self._publish_status("transitioned")

    async def status(self) -> FSMStatus | None:
        """Return and publish current status."""

        async with self._lock:
            if self._chart is None or self._record is None:
                return None
            return await self._publish_status("updated")

    async def _publish_status(self, reason: str, *, publish: bool = True) -> FSMStatus:
        assert self._chart is not None and self._record is not None
        candidates = tuple(
            TransitionCandidate(
                event=item.event,
                source=item.source,
                target=item.target,
                transition_context=item.context,
                source_state_context=self._chart.context_for(item.source),
                target_state_context=self._chart.context_for(item.target),
            )
            for item in self._chart.transitions
            if item.source == self._record.active_state
        )
        status = FSMStatus(
            mission_id=self._record.mission_id,
            plan_revision=self._record.plan_revision,
            statechart_revision=self._record.statechart_revision,
            active_state=self._record.active_state,
            transition_candidates=candidates,
            status=reason,
            superseded_plan_revision=self._superseded_plan_revision,
            last_applied_event=self._record.last_applied_event,
            active_state_context=self._chart.context_for(self._record.active_state),
        )
        if not publish:
            return status
        sequence = self.transport.next_event_sequence(self.status_topic, status.mission_id)
        self.transport.publish_event(
            self.status_topic,
            TransportEvent(
                schema_version=1,
                event_id=f"fsm-status:{status.mission_id}:{sequence}",
                mission_id=status.mission_id,
                sequence=sequence,
                event_kind="fsm-status",
                payload=status.to_dict(),
            ),
        )
        if self.operational_log is not None:
            self.operational_log.emit(
                status.mission_id,
                "fsm-runner",
                "fsm",
                status.status,
                details={
                    "operation": "publish_status",
                    "plan_revision": status.plan_revision,
                    "state": status.active_state,
                    "status": status.status,
                    "transport_sequence": sequence,
                },
            )
        return status

    def _decision_authorizes(self, event: str, decision: ManeuverDecision) -> bool:
        assert self._record is not None
        return (
            isinstance(decision, ManeuverDecision)
            and decision.mission_id == self._record.mission_id
            and decision.transition_event == event
            and decision.payload.get("plan_revision") == self._record.plan_revision
        )
