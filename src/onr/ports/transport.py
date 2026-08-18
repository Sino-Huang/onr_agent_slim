"""Transport seams used by mission-control services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    NormalizedPlanTransportEvent,
    TransportEvent,
)


TransportMessage: TypeAlias = TransportEvent | Command | CommandOutcome


@dataclass(frozen=True, slots=True)
class Subscription:
    """A static consumer registration supplied when an adapter is built."""

    service_id: str
    mission_id: str
    topic: str
    max_retries: int = 3

    def __post_init__(self) -> None:
        for name in ("service_id", "mission_id", "topic"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"subscription {name} must be non-empty")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise ValueError("subscription max_retries must be an integer")
        if self.max_retries < 1:
            raise ValueError("subscription max_retries must be positive")


class Delivery(Protocol):
    """A message delivery whose cursor advances only after acknowledgement."""

    message: TransportMessage
    attempt: int

    def ack(self) -> None: ...

    def nack(self) -> None: ...


class Consumer(Protocol):
    """At-least-once consumer for one exact static subscription."""

    def receive(self) -> Delivery | None: ...

    def ack(self, delivery: Delivery) -> None: ...

    def nack(self, delivery: Delivery) -> None: ...

    def close(self) -> None: ...


class Transport(Protocol):
    """Static-subscription transport port."""

    def publish_event(self, topic: str, event: TransportEvent | NormalizedPlanTransportEvent) -> TransportEvent: ...

    def send_command(self, command: Command) -> CommandReceipt: ...

    def publish_outcome(self, outcome: CommandOutcome) -> CommandOutcome: ...

    def next_event_sequence(self, topic: str, mission_id: str) -> int: ...

    def latest_event(
        self, topic: str, mission_id: str, *, event_kind: str | None = None
    ) -> TransportEvent | None: ...

    def get_cursor(self, subscription: Subscription) -> dict[str, int]: ...

    def open_consumer(self, subscription: Subscription) -> Consumer: ...
