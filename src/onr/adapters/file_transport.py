"""Durable filesystem transport with at-least-once delivery."""

from __future__ import annotations

import json
import os
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    NormalizedPlanTransportEvent,
    TransportEvent,
    normalized_plan_transport_event_to_wire,
)
from onr.ports.transport import Subscription


def _part(value: str) -> str:
    return quote(value, safe="._-")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"transport state is corrupt: {path}") from exc


def _wire_event(event: TransportEvent | NormalizedPlanTransportEvent) -> TransportEvent:
    if isinstance(event, TransportEvent):
        return event
    if isinstance(event, NormalizedPlanTransportEvent):
        return normalized_plan_transport_event_to_wire(event)
    raise TypeError("published event must be a TransportEvent")


@dataclass
class _FileDelivery:
    consumer: "FileConsumer"
    message: object
    identity: str
    sequence: int
    attempt: int
    _done: bool = False

    @property
    def event(self) -> object:
        """Compatibility view for consumers that call deliveries events."""
        return self.message

    def ack(self) -> None:
        self.consumer.ack(self)

    def nack(self) -> None:
        self.consumer.nack(self)


class FileTransport:
    """A small durable transport implemented entirely with atomic file writes."""

    def __init__(
        self,
        root: Path,
        subscriptions: tuple[Subscription, ...] | list[Subscription] = (),
        *,
        max_retries: int = 3,
    ) -> None:
        self.root = Path(root)
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError("max_retries must be a positive integer")
        self.max_retries = max_retries
        self.subscriptions = tuple(subscriptions)
        if not all(isinstance(item, Subscription) for item in self.subscriptions):
            raise ValueError("subscriptions must contain Subscription records")
        self.root.mkdir(parents=True, exist_ok=True)
        self._consumers: list[FileConsumer] = []

    def publish_event(
        self,
        topic: str,
        event: TransportEvent | NormalizedPlanTransportEvent,
    ) -> TransportEvent:
        with self._publisher_lock():
            return self._publish_event(topic, event)

    def _publish_event(
        self,
        topic: str,
        event: TransportEvent | NormalizedPlanTransportEvent,
    ) -> TransportEvent:
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("publish_event requires a topic and event")
        wire = _wire_event(event)
        identity_path = self.root / "identity" / f"event-{_part(wire.event_id)}.json"
        existing = _read_json(identity_path, None)
        if existing is not None:
            if existing != wire.to_dict():
                raise ValueError("event identity conflicts with existing content")
            return wire
        stream = self.root / "topics" / _part(topic) / "missions" / _part(wire.mission_id)
        stream.mkdir(parents=True, exist_ok=True)
        for path in stream.glob("*.json"):
            raw = _read_json(path, {})
            if isinstance(raw, dict) and raw.get("sequence") == wire.sequence:
                if raw != wire.to_dict():
                    raise ValueError("event sequence conflicts with existing content")
                return wire
        target = stream / f"{wire.sequence:020d}-{_part(wire.event_id)}.json"
        _atomic_write(target, wire.to_canonical_json())
        _atomic_write(identity_path, wire.to_canonical_json())
        return wire

    def send_command(self, command: Command) -> CommandReceipt:
        with self._publisher_lock():
            return self._send_command(command)

    def _send_command(self, command: Command) -> CommandReceipt:
        if not isinstance(command, Command):
            raise TypeError("send_command requires a Command")
        identity_path = self.root / "identity" / f"command-{_part(command.command_id)}.json"
        existing = _read_json(identity_path, None)
        if existing is not None:
            if existing != command.to_dict():
                raise ValueError("command identity conflicts with existing content")
            receipt_path = self.root / "receipts" / f"{_part(command.command_id)}.json"
            if not receipt_path.exists():
                _atomic_write(receipt_path, self._receipt(command).to_canonical_json())
            return self._receipt(command)
        command_dir = self.root / "commands" / _part(command.target_service) / _part(command.mission_id)
        command_dir.mkdir(parents=True, exist_ok=True)
        sequence = self._next_sequence(command_dir)
        _atomic_write(
            command_dir / f"{sequence:020d}-{_part(command.command_id)}.json",
            json.dumps({"kind": "command", "sequence": sequence, "command_kind": command.command_kind, "command": command.to_dict()}, sort_keys=True, separators=(",", ":")),
        )
        _atomic_write(identity_path, command.to_canonical_json())
        _atomic_write(self.root / "receipts" / f"{_part(command.command_id)}.json", self._receipt(command).to_canonical_json())
        return self._receipt(command)

    def publish_outcome(self, outcome: CommandOutcome) -> CommandOutcome:
        with self._publisher_lock():
            return self._publish_outcome(outcome)

    def _publish_outcome(self, outcome: CommandOutcome) -> CommandOutcome:
        if not isinstance(outcome, CommandOutcome):
            raise TypeError("publish_outcome requires a CommandOutcome")
        command_path = self.root / "identity" / f"command-{_part(outcome.command_id)}.json"
        command = _read_json(command_path, None)
        if command is None:
            raise ValueError("cannot publish an outcome for an unknown command")
        identity_path = self.root / "identity" / f"outcome-{_part(outcome.command_id)}.json"
        existing = _read_json(identity_path, None)
        if existing is not None:
            if existing != outcome.to_dict():
                raise ValueError("outcome identity conflicts with existing content")
            return outcome
        if not isinstance(command, dict):
            raise RuntimeError("stored command is corrupt")
        command_kind = command.get("command_kind", "outcomes")
        directory = self.root / "commands" / _part(str(command["target_service"])) / _part(str(command["mission_id"]))
        sequence = self._next_sequence(directory)
        _atomic_write(
            directory / f"{sequence:020d}-{_part(outcome.command_id)}-outcome.json",
            json.dumps({"kind": "outcome", "sequence": sequence, "command_kind": command_kind, "outcome": outcome.to_dict()}, sort_keys=True, separators=(",", ":")),
        )
        _atomic_write(identity_path, outcome.to_canonical_json())
        return outcome

    record_outcome = publish_outcome

    def get_command_receipt(self, command_id: str) -> CommandReceipt | None:
        value = _read_json(self.root / "receipts" / f"{_part(command_id)}.json", None)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("stored command receipt is corrupt")
        return CommandReceipt.from_dict(value)

    def get_command_outcome(self, command_id: str) -> CommandOutcome | None:
        value = _read_json(self.root / "identity" / f"outcome-{_part(command_id)}.json", None)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("stored command outcome is corrupt")
        return CommandOutcome.from_dict(value)

    def get_event(self, event_id: str) -> TransportEvent | None:
        """Return a previously published event by its durable identity."""

        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("event ID must be a non-empty string")
        value = _read_json(self.root / "identity" / f"event-{_part(event_id)}.json", None)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("stored transport event is corrupt")
        return TransportEvent.from_dict(value)

    def open_consumer(
        self,
        subscription: Subscription,
    ) -> "FileConsumer":
        if subscription not in self.subscriptions:
            raise ValueError("consumer subscription was not supplied at adapter construction")
        state = self.root / "subscriptions" / _part(subscription.service_id) / _part(subscription.mission_id) / _part(subscription.topic)
        state.mkdir(parents=True, exist_ok=True)
        lock_handle = state.joinpath("consumer.lock").open("a+")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            lock_handle.close()
            raise RuntimeError("only one active consumer is allowed for a subscription") from exc
        consumer = FileConsumer(self, subscription, state, lock_handle)
        self._consumers.append(consumer)
        return consumer

    def _receipt(self, command: Command) -> CommandReceipt:
        return CommandReceipt(
            schema_version=command.schema_version,
            command_id=command.command_id,
            correlation_id=command.correlation_id,
            mission_id=command.mission_id,
            target_service=command.target_service,
        )

    @contextmanager
    def _publisher_lock(self):
        lock_path = self.root / ".publisher.lock"
        handle = lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @staticmethod
    def _next_sequence(directory: Path) -> int:
        sequences = []
        for path in directory.glob("*.json"):
            try:
                sequences.append(int(path.name.split("-", 1)[0]))
            except ValueError:
                pass
        return max(sequences, default=-1) + 1

    def next_event_sequence(self, topic: str, mission_id: str) -> int:
        if not isinstance(topic, str) or not topic.strip() or not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("topic and mission ID must be non-empty strings")
        with self._publisher_lock():
            stream = self.root / "topics" / _part(topic) / "missions" / _part(mission_id)
            sequences = []
            for path in stream.glob("*.json"):
                try:
                    sequences.append(int(path.name.split("-", 1)[0]))
                except ValueError:
                    pass
            return max(sequences, default=-1) + 1

    def latest_event(
        self, topic: str, mission_id: str, *, event_kind: str | None = None
    ) -> TransportEvent | None:
        stream = self.root / "topics" / _part(topic) / "missions" / _part(mission_id)
        candidates: list[TransportEvent] = []
        for path in stream.glob("*.json"):
            value = _read_json(path, None)
            if not isinstance(value, dict):
                continue
            try:
                event = TransportEvent.from_dict(value)
            except ValueError:
                continue
            if event_kind is None or event.event_kind == event_kind:
                candidates.append(event)
        return max(candidates, key=lambda event: (event.sequence, event.event_id), default=None)

    def get_dead_letters(self, subscription: Subscription) -> tuple[dict[str, object], ...]:
        state = self.root / "subscriptions" / _part(subscription.service_id) / _part(subscription.mission_id) / _part(subscription.topic) / "dead-letter"
        return tuple(
            value
            for path in sorted(state.glob("*.json"))
            if isinstance((value := _read_json(path, None)), dict)
        )

    def get_cursor(self, subscription: Subscription) -> dict[str, int]:
        state = self.root / "subscriptions" / _part(subscription.service_id) / _part(subscription.mission_id) / _part(subscription.topic)
        value = _read_json(state / "cursor.json", {"sequence": -1})
        return dict(value) if isinstance(value, dict) else {"sequence": -1}


class FileConsumer:
    def __init__(self, transport: FileTransport, subscription: Subscription, state: Path, lock_handle: TextIO) -> None:
        self._transport = transport
        self.subscription = subscription
        self._lock_handle = lock_handle
        self._closed = False
        self._state = state
        self._active_delivery: tuple[object, str, int, str] | None = None
        self._state.mkdir(parents=True, exist_ok=True)

    def receive(self) -> _FileDelivery | None:
        if self._closed:
            raise RuntimeError("consumer is closed")
        cursor = _read_json(self._state / "cursor.json", {"sequence": -1})
        cursor_value = dict(cursor) if isinstance(cursor, dict) else {"sequence": -1}
        processed = _read_json(self._state / "processed.json", [])
        processed = set(processed) if isinstance(processed, list) else set()
        attempts = _read_json(self._state / "attempts.json", {})
        attempts = dict(attempts) if isinstance(attempts, dict) else {}
        while True:
            candidate = self._active_delivery or self._next_candidate(cursor_value, processed)
            if candidate is None:
                return None
            message, identity, sequence, source = candidate
            attempt = int(attempts.get(identity, 0)) + 1
            attempts[identity] = attempt
            _atomic_write(self._state / "attempts.json", json.dumps(attempts, sort_keys=True, separators=(",", ":")))
            limit = self.subscription.max_retries or self._transport.max_retries
            if attempt > limit:
                self._dead_letter(message, identity, attempt)
                processed.add(identity)
                self._active_delivery = None
                self._save_ack(sequence, source, processed)
                cursor_value = self._cursor()
                continue
            delivery = _FileDelivery(self, message, identity, sequence, attempt)
            self._active_delivery = (message, identity, sequence, source)
            return delivery

    poll = receive

    def ack(self, delivery: _FileDelivery) -> None:
        self._check_delivery(delivery)
        if delivery._done:
            return
        processed = _read_json(self._state / "processed.json", [])
        identities = set(processed) if isinstance(processed, list) else set()
        identities.add(delivery.identity)
        source = self._active_delivery[3] if self._active_delivery and self._active_delivery[1] == delivery.identity else "event"
        self._save_ack(delivery.sequence, source, identities)
        if self._active_delivery and self._active_delivery[1] == delivery.identity:
            self._active_delivery = None
        delivery._done = True

    def nack(self, delivery: _FileDelivery) -> None:
        self._check_delivery(delivery)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()
            if self in self._transport._consumers:
                self._transport._consumers.remove(self)

    def __enter__(self) -> "FileConsumer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _check_delivery(self, delivery: _FileDelivery) -> None:
        if not isinstance(delivery, _FileDelivery) or delivery.consumer is not self:
            raise ValueError("delivery does not belong to this consumer")

    def _cursor(self) -> dict[str, int]:
        value = _read_json(self._state / "cursor.json", {"sequence": -1})
        return dict(value) if isinstance(value, dict) else {"sequence": -1}

    def _save_ack(self, cursor: int, source: str, processed: set[str]) -> None:
        values = self._cursor()
        default = values.get("sequence", -1) if source == "event" else -1
        values[source] = max(values.get(source, default), cursor)
        values["sequence"] = max(values.get("sequence", -1), cursor)
        _atomic_write(self._state / "cursor.json", json.dumps(values, separators=(",", ":")))
        _atomic_write(self._state / "processed.json", json.dumps(sorted(processed), separators=(",", ":")))

    def _dead_letter(self, message: object, identity: str, attempt: int) -> None:
        dead = self._state / "dead-letter" / f"{len(list((self._state / 'dead-letter').glob('*.json'))):020d}-{_part(identity)}.json"
        if not isinstance(message, (TransportEvent, Command, CommandOutcome)):
            raise TypeError("dead-letter message is not a transport message")
        _atomic_write(dead, json.dumps({"identity": identity, "attempt": attempt, "message": message.to_dict()}, sort_keys=True, separators=(",", ":")))

    def _next_candidate(self, cursor: dict[str, int], processed: set[str]) -> tuple[object, str, int, str] | None:
        candidates: list[tuple[int, str, object, str]] = []
        event_dir = self._transport.root / "topics" / _part(self.subscription.topic) / "missions" / _part(self.subscription.mission_id)
        for path in event_dir.glob("*.json"):
            value = _read_json(path, None)
            if isinstance(value, dict):
                try:
                    event = TransportEvent.from_dict(value)
                except ValueError:
                    continue
                candidates.append((event.sequence, event.event_id, event, "event"))
        command_dir = self._find_command_dir()
        for path in command_dir.glob("*.json") if command_dir.exists() else ():
            value = _read_json(path, None)
            if not isinstance(value, dict) or value.get("kind") not in {"command", "outcome"}:
                continue
            if value.get("command_kind") not in (None, self.subscription.topic, f"commands/{self.subscription.topic}", "outcomes"):
                continue
            if value["kind"] == "command":
                message = Command.from_dict(value["command"])
                identity = message.command_id
            else:
                message = CommandOutcome(**value["outcome"])
                identity = f"outcome:{message.command_id}"
            candidates.append((int(value["sequence"]), identity, message, "command"))
        for sequence, identity, message, source in sorted(candidates, key=lambda item: (item[0], item[1])):
            limit = cursor.get(source, cursor.get("sequence", -1) if source == "event" else -1)
            if identity not in processed and sequence > limit:
                return message, identity, sequence, source
        return None

    def _find_command_dir(self) -> Path:
        return self._transport.root / "commands" / _part(self.subscription.service_id) / _part(self.subscription.mission_id)
