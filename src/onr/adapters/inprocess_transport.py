"""In-process transport with the same durable-delivery semantics as FileTransport."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from onr.adapters.file_transport import _wire_event
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    NormalizedPlanTransportEvent,
    TransportEvent,
)
from onr.ports.transport import Subscription


@dataclass
class InProcessTransportState:
    """Injectable state store used to model an adapter restart in tests."""

    events: dict[tuple[str, str], list[TransportEvent]] = field(default_factory=dict)
    commands: dict[tuple[str, str], list[tuple[int, object]]] = field(default_factory=dict)
    identities: dict[str, str] = field(default_factory=dict)
    receipts: dict[str, CommandReceipt] = field(default_factory=dict)
    outcomes: dict[str, CommandOutcome] = field(default_factory=dict)
    cursors: dict[tuple[str, str, str], dict[str, int]] = field(default_factory=dict)
    processed: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    attempts: dict[tuple[str, str, str], dict[str, int]] = field(default_factory=dict)
    dead_letters: dict[tuple[str, str, str], list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class _InProcessDelivery:
    consumer: "InProcessConsumer"
    message: object
    identity: str
    sequence: int
    attempt: int
    _done: bool = False

    @property
    def event(self) -> object:
        return self.message

    def ack(self) -> None:
        self.consumer.ack(self)

    def nack(self) -> None:
        self.consumer.nack(self)


class InProcessTransport:
    _active: set[tuple[int, str, str, str]] = set()

    def __init__(
        self,
        subscriptions: tuple[Subscription, ...] | list[Subscription] = (),
        *,
        state: InProcessTransportState | None = None,
        max_retries: int = 3,
    ) -> None:
        if isinstance(subscriptions, InProcessTransportState) and state is None:
            state = subscriptions
            subscriptions = ()
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError("max_retries must be a positive integer")
        self.state = state or InProcessTransportState()
        self.subscriptions = tuple(subscriptions)
        self.max_retries = max_retries
        if not all(isinstance(item, Subscription) for item in self.subscriptions):
            raise ValueError("subscriptions must contain Subscription records")

    def publish_event(
        self,
        topic: str,
        event: TransportEvent | NormalizedPlanTransportEvent,
    ) -> TransportEvent:
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("publish_event requires a topic and event")
        wire = _wire_event(event)
        identity = f"event:{wire.event_id}"
        content = wire.to_canonical_json()
        existing = self.state.identities.get(identity)
        if existing is not None:
            if existing != content:
                raise ValueError("event identity conflicts with existing content")
            return wire
        stream = self.state.events.setdefault((topic, wire.mission_id), [])
        if any(item.sequence == wire.sequence and item != wire for item in stream):
            raise ValueError("event sequence conflicts with existing content")
        stream.append(wire)
        stream.sort(key=lambda item: (item.sequence, item.event_id))
        self.state.identities[identity] = content
        return wire

    def send_command(self, command: Command) -> CommandReceipt:
        if not isinstance(command, Command):
            raise TypeError("send_command requires a Command")
        identity = f"command:{command.command_id}"
        content = command.to_canonical_json()
        existing = self.state.identities.get(identity)
        if existing is not None:
            if existing != content:
                raise ValueError("command identity conflicts with existing content")
            return self.state.receipts[command.command_id]
        queue = self.state.commands.setdefault((command.target_service, command.mission_id), [])
        sequence = len(queue)
        queue.append((sequence, command))
        self.state.identities[identity] = content
        receipt = CommandReceipt(
            schema_version=command.schema_version,
            command_id=command.command_id,
            correlation_id=command.correlation_id,
            mission_id=command.mission_id,
            target_service=command.target_service,
        )
        self.state.receipts[command.command_id] = receipt
        return receipt

    def publish_outcome(self, outcome: CommandOutcome) -> CommandOutcome:
        if not isinstance(outcome, CommandOutcome):
            raise TypeError("publish_outcome requires a CommandOutcome")
        command = self._command(outcome.command_id)
        if command is None:
            raise ValueError("cannot publish an outcome for an unknown command")
        identity = f"outcome:{outcome.command_id}"
        content = outcome.to_canonical_json()
        existing = self.state.identities.get(identity)
        if existing is not None:
            if existing != content:
                raise ValueError("outcome identity conflicts with existing content")
            return outcome
        queue = self.state.commands.setdefault((command.target_service, command.mission_id), [])
        queue.append((len(queue), outcome))
        self.state.identities[identity] = content
        self.state.outcomes[outcome.command_id] = outcome
        return outcome

    record_outcome = publish_outcome

    def get_command_receipt(self, command_id: str) -> CommandReceipt | None:
        return self.state.receipts.get(command_id)

    def get_command_outcome(self, command_id: str) -> CommandOutcome | None:
        return self.state.outcomes.get(command_id)

    def next_event_sequence(self, topic: str, mission_id: str) -> int:
        if not isinstance(topic, str) or not topic.strip() or not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("topic and mission ID must be non-empty strings")
        return max((event.sequence for event in self.state.events.get((topic, mission_id), ())), default=-1) + 1

    def latest_event(
        self, topic: str, mission_id: str, *, event_kind: str | None = None
    ) -> TransportEvent | None:
        events = self.state.events.get((topic, mission_id), ())
        candidates = (
            event for event in events if event_kind is None or event.event_kind == event_kind
        )
        return max(candidates, key=lambda event: (event.sequence, event.event_id), default=None)

    def get_dead_letters(self, subscription: Subscription) -> tuple[dict[str, object], ...]:
        return tuple(self.state.dead_letters.get((subscription.service_id, subscription.mission_id, subscription.topic), ()))

    def get_cursor(self, subscription: Subscription) -> dict[str, int]:
        value = self.state.cursors.get((subscription.service_id, subscription.mission_id, subscription.topic), {"sequence": -1})
        return dict(value) if isinstance(value, dict) else {"sequence": int(value)}

    def open_consumer(
        self,
        subscription: Subscription,
    ) -> "InProcessConsumer":
        if subscription not in self.subscriptions:
            raise ValueError("consumer subscription was not supplied at adapter construction")
        key = (id(self.state), subscription.service_id, subscription.mission_id, subscription.topic)
        if key in self._active:
            raise RuntimeError("only one active consumer is allowed for a subscription")
        self._active.add(key)
        return InProcessConsumer(self, subscription, key)

    def _command(self, command_id: str) -> Command | None:
        for queue in self.state.commands.values():
            for _, message in queue:
                if isinstance(message, Command) and message.command_id == command_id:
                    return message
        return None


class InProcessConsumer:
    def __init__(self, transport: InProcessTransport, subscription: Subscription, key: tuple[int, str, str, str]) -> None:
        self._transport = transport
        self.subscription = subscription
        self._key = key
        self._closed = False
        self._active_delivery: tuple[object, str, int, str] | None = None

    def receive(self) -> _InProcessDelivery | None:
        if self._closed:
            raise RuntimeError("consumer is closed")
        state = self._transport.state
        cursor_value = state.cursors.get(self._key[1:], {"sequence": -1})
        cursor = dict(cursor_value) if isinstance(cursor_value, dict) else {"sequence": int(cursor_value)}
        processed = state.processed.setdefault(self._key[1:], set())
        attempts = state.attempts.setdefault(self._key[1:], {})
        while True:
            candidate = self._active_delivery or self._next(cursor, processed)
            if candidate is None:
                return None
            message, identity, sequence, source = candidate
            attempt = attempts.get(identity, 0) + 1
            attempts[identity] = attempt
            if attempt > self.subscription.max_retries:
                if not isinstance(message, (TransportEvent, Command, CommandOutcome)):
                    raise TypeError("dead-letter message is not a transport message")
                state.dead_letters.setdefault(self._key[1:], []).append({"identity": identity, "attempt": attempt, "message": message.to_dict()})
                processed.add(identity)
                self._active_delivery = None
                self._save_cursor(sequence, source)
                cursor = self._cursor()
                continue
            delivery = _InProcessDelivery(self, message, identity, sequence, attempt)
            self._active_delivery = (message, identity, sequence, source)
            return delivery

    poll = receive

    def ack(self, delivery: _InProcessDelivery) -> None:
        if not isinstance(delivery, _InProcessDelivery) or delivery.consumer is not self:
            raise ValueError("delivery does not belong to this consumer")
        if delivery._done:
            return
        state = self._transport.state
        key = self._key[1:]
        state.processed.setdefault(key, set()).add(delivery.identity)
        source = self._active_delivery[3] if self._active_delivery and self._active_delivery[1] == delivery.identity else "event"
        self._save_cursor(delivery.sequence, source)
        if self._active_delivery and self._active_delivery[1] == delivery.identity:
            self._active_delivery = None
        delivery._done = True

    def nack(self, delivery: _InProcessDelivery) -> None:
        if not isinstance(delivery, _InProcessDelivery) or delivery.consumer is not self:
            raise ValueError("delivery does not belong to this consumer")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            InProcessTransport._active.discard(self._key)

    def __enter__(self) -> "InProcessConsumer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _cursor(self) -> dict[str, int]:
        value = self._transport.state.cursors.get(self._key[1:], {"sequence": -1})
        return dict(value) if isinstance(value, dict) else {"sequence": int(value)}

    def _save_cursor(self, sequence: int, source: str) -> None:
        cursor = self._cursor()
        default = cursor.get("sequence", -1) if source == "event" else -1
        cursor[source] = max(cursor.get(source, default), sequence)
        cursor["sequence"] = max(cursor.get("sequence", -1), sequence)
        self._transport.state.cursors[self._key[1:]] = cursor

    def _next(self, cursor: dict[str, int], processed: set[str]) -> tuple[object, str, int, str] | None:
        state = self._transport.state
        candidates: list[tuple[int, str, object, str]] = []
        for event in state.events.get((self.subscription.topic, self.subscription.mission_id), ()):
            candidates.append((event.sequence, event.event_id, event, "event"))
        for sequence, message in state.commands.get((self.subscription.service_id, self.subscription.mission_id), ()):
            if isinstance(message, Command) and message.command_kind not in {
                self.subscription.topic, f"commands/{self.subscription.topic}", "commands"
            }:
                continue
            if isinstance(message, CommandOutcome) or isinstance(message, Command):
                identity = message.command_id if isinstance(message, Command) else f"outcome:{message.command_id}"
                candidates.append((sequence, identity, message, "command"))
        for sequence, identity, message, source in sorted(candidates, key=lambda item: (item[0], item[1])):
            limit = cursor.get(source, cursor.get("sequence", -1) if source == "event" else -1)
            if identity not in processed and sequence > limit:
                return message, identity, sequence, source
        return None
