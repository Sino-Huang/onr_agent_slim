"""Persistence seam for the application-owned FSM Runner."""

from __future__ import annotations

from typing import Protocol

from onr.contracts.fsm import FSMExecutionRecord, Statechart
from onr.contracts.transport import NormalizedPlanTransportEvent, TransportEvent


class FSMStateStore(Protocol):
    """Stores only canonical JSON-derived FSM authority records."""

    def load_statechart(self) -> Statechart | None: ...

    def load_execution_record(self) -> FSMExecutionRecord | None: ...

    def save_statechart(self, statechart: Statechart) -> None: ...

    def save_execution_record(self, record: FSMExecutionRecord) -> None: ...


class RunningStateMachine(Protocol):
    """Live transition engine reconstructed from Statechart data."""

    @property
    def current_state(self) -> str: ...

    @property
    def allowed_events(self) -> tuple[str, ...]: ...

    def send(self, event: str) -> None: ...


class StateMachineFactory(Protocol):
    """Build a live machine without making it durable authority."""

    def build(
        self, statechart: Statechart, *, start_state: str | None = None
    ) -> RunningStateMachine: ...


class FSMTransport(Protocol):
    """Minimal transport surface required by the application FSM."""

    def next_event_sequence(self, topic: str, mission_id: str) -> int: ...

    def publish_event(
        self,
        topic: str,
        event: TransportEvent | NormalizedPlanTransportEvent,
    ) -> TransportEvent: ...
