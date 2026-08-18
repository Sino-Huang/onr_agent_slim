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


class FSMTransport(Protocol):
    """Minimal transport surface required by the application FSM."""

    def next_event_sequence(self, topic: str, mission_id: str) -> int: ...

    def publish_event(
        self,
        topic: str,
        event: TransportEvent | NormalizedPlanTransportEvent,
    ) -> TransportEvent: ...
