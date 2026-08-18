from __future__ import annotations

from pathlib import Path

import pytest

from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport, InProcessTransportState
from onr.contracts.transport import Command, CommandOutcome, TransportEvent
from onr.ports.transport import Subscription


def _make_transport(kind: str, root: Path, subscriptions: tuple[Subscription, ...], state=None):
    if kind == "file":
        return FileTransport(root, subscriptions)
    return InProcessTransport(subscriptions, state=state)


@pytest.mark.parametrize("kind", ("file", "inprocess"))
def test_transport_ordering_fanout_ack_replay_and_identity(kind: str, tmp_path: Path) -> None:
    subscriptions = (
        Subscription("one", "mission", "plans"),
        Subscription("two", "mission", "plans"),
    )
    transport = _make_transport(kind, tmp_path, subscriptions)
    transport.publish_event("plans", TransportEvent(1, "event-1", "mission", 0, "plan", {"n": 1}))
    transport.publish_event("plans", TransportEvent(1, "event-2", "mission", 1, "plan", {"n": 2}))
    first = transport.open_consumer(subscriptions[0])
    second = transport.open_consumer(subscriptions[1])
    delivery = first.receive()
    assert delivery is not None and delivery.message.event_id == "event-1"
    assert first.receive() is not None  # unacknowledged messages replay
    assert transport.get_cursor(subscriptions[0]).get("event", -1) == -1
    delivery.ack()
    assert transport.get_cursor(subscriptions[0])["event"] == 0
    assert first.receive().message.event_id == "event-2"
    second_delivery = second.receive()
    assert second_delivery is not None and second_delivery.message.event_id == "event-1"
    second_delivery.ack()
    first.close()
    second.close()

    if kind == "file":
        restarted = _make_transport(kind, tmp_path, subscriptions)
    else:
        restarted = _make_transport(kind, tmp_path, subscriptions, transport.state)
    replay = restarted.open_consumer(subscriptions[0])
    replay_delivery = replay.receive()
    assert replay_delivery is not None and replay_delivery.message.event_id == "event-2"
    replay_delivery.ack()
    replay.close()

    duplicate = TransportEvent(1, "event-2", "mission", 1, "plan", {"n": 2})
    restarted.publish_event("plans", duplicate)
    with pytest.raises(ValueError):
        restarted.publish_event("plans", TransportEvent(1, "event-2", "mission", 1, "plan", {"n": 9}))


@pytest.mark.parametrize("kind", ("file", "inprocess"))
def test_transport_exclusive_retries_dead_letter_and_command_correlation(kind: str, tmp_path: Path) -> None:
    subscription = Subscription("planner", "mission", "plan", max_retries=2)
    transport = _make_transport(kind, tmp_path, (subscription,))
    consumer = transport.open_consumer(subscription)
    with pytest.raises(RuntimeError):
        transport.open_consumer(subscription)
    transport.publish_event("plan", TransportEvent(1, "bad", "mission", 0, "plan", {}))
    assert consumer.receive() is not None
    assert consumer.receive() is not None
    assert consumer.receive() is None  # exhausted and dead-lettered
    assert transport.get_dead_letters(subscription) == ({
        "identity": "bad",
        "attempt": 3,
        "message": TransportEvent(1, "bad", "mission", 0, "plan", {}).to_dict(),
    },)
    transport.publish_event("plan", TransportEvent(1, "good", "mission", 1, "plan", {}))
    good = consumer.receive()
    assert good is not None and good.message.event_id == "good"
    good.ack()

    command = Command(1, "command-1", "correlation-1", "mission", "planner", "plan", {})
    receipt = transport.send_command(command)
    assert receipt.command_id == command.command_id
    assert transport.send_command(command) == receipt
    command_delivery = consumer.receive()
    assert command_delivery is not None and command_delivery.message.command_id == command.command_id
    command_delivery.ack()
    outcome = CommandOutcome(1, command.command_id, command.correlation_id, command.mission_id, "completed", {})
    assert transport.publish_outcome(outcome) == outcome
    outcome_delivery = consumer.receive()
    assert outcome_delivery is not None and outcome_delivery.message.correlation_id == command.correlation_id
    outcome_delivery.ack()
    consumer.close()


def test_file_duplicate_command_repairs_missing_receipt(tmp_path: Path) -> None:
    subscription = Subscription("planner", "mission", "plan")
    transport = FileTransport(tmp_path, (subscription,))
    command = Command(1, "repair-command", "correlation", "mission", "planner", "plan", {})
    receipt = transport.send_command(command)
    receipt_path = tmp_path / "receipts" / "repair-command.json"
    receipt_path.unlink()
    assert transport.get_command_receipt(command.command_id) is None
    assert transport.send_command(command) == receipt
    assert transport.get_command_receipt(command.command_id) == receipt
