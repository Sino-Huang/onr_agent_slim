from pathlib import Path

from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.planning_commands import PlanningCommandHandler
from onr.contracts.planning import (
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanProvenance,
    VerifiableReference,
)
from onr.contracts.transport import Command
from onr.ports.transport import Subscription


def _plan(mission_id: str, revision: int) -> NormalizedPlan:
    return NormalizedPlan(
        plan_revision=revision,
        mission_snapshot_id=f"snapshot-{revision}",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.UNSOLVABLE,
        provenance=PlanProvenance(
            mission_id=mission_id,
            source_authority="authority",
            mission_intent=VerifiableReference(f"mission-input:{mission_id}", "1" * 64),
            planning_decision=VerifiableReference(f"planner-choice:{mission_id}", "2" * 64),
            operational_scene_graph=VerifiableReference(f"scene:{mission_id}", "3" * 64),
            generated_assets={
                "model.mzn": VerifiableReference("model.mzn", "4" * 64),
            },
            solver_evidence={
                "stdout": VerifiableReference("solver.stdout", "5" * 64),
            },
        ),
    )



def test_planning_command_is_effective_once_and_correlated() -> None:
    normalized = _plan("mission", 0)
    subscriptions = (
        Subscription("planner", "mission", "plan"),
        Subscription("reader", "mission", "plans"),
    )
    transport = InProcessTransport(subscriptions)
    commands = transport.open_consumer(subscriptions[0])
    plans = transport.open_consumer(subscriptions[1])
    transport.send_command(Command(1, "command", "correlation", "mission", "planner", "plan", {}))
    calls: list[str] = []

    def planner(command: Command) -> NormalizedPlan:
        calls.append(command.command_id)
        return normalized

    handler = PlanningCommandHandler(transport, planner, topic="plans")
    first = handler.run_once(commands)
    assert first is not None and first.status == "completed"
    assert plans.receive().message.event_id == "normalized-plan:command"
    assert handler.run_once(commands) is None
    assert calls == ["command"]
    commands.close()
    plans.close()


def test_failed_planning_outcome_is_nacked_until_dead_letter() -> None:
    subscription = Subscription("planner", "mission", "plan", max_retries=2)
    transport = InProcessTransport((subscription,))
    consumer = transport.open_consumer(subscription)
    transport.send_command(Command(1, "failed-command", "correlation", "mission", "planner", "plan", {}))

    def planner(_: Command) -> NormalizedPlan:
        raise RuntimeError("planner unavailable")

    handler = PlanningCommandHandler(transport, planner)
    assert handler.run_once(consumer).status == "failed"
    assert transport.get_cursor(subscription).get("command", -1) == -1
    assert handler.run_once(consumer).status == "failed"
    assert handler.run_once(consumer) is None
    assert len(transport.get_dead_letters(subscription)) == 1
    assert transport.get_command_outcome("failed-command").status == "failed"
    consumer.close()


def test_planning_command_rejects_result_for_another_mission() -> None:
    command_subscription = Subscription("planner", "mission", "plan")
    transport = InProcessTransport((command_subscription,))
    consumer = transport.open_consumer(command_subscription)
    transport.send_command(Command(1, "mismatch-command", "correlation", "mission", "planner", "plan", {}))
    result = _plan("other-mission", 1)
    outcome = PlanningCommandHandler(transport, lambda _: result).run_once(consumer)
    assert outcome is not None and outcome.status == "failed"
    assert transport.next_event_sequence("normalized-plans", "mission") == 0
    assert transport.get_command_outcome("mismatch-command") == outcome
    consumer.close()


def test_file_planning_command_restarts_without_repeating_planner(tmp_path: Path) -> None:
    normalized = _plan("file-mission", 7)
    subscriptions = (
        Subscription("planner", "file-mission", "plan"),
        Subscription("reader", "file-mission", "plans"),
    )
    transport = FileTransport(tmp_path, subscriptions)
    commands = transport.open_consumer(subscriptions[0])
    plans = transport.open_consumer(subscriptions[1])
    command = Command(1, "file-command", "file-correlation", "file-mission", "planner", "plan", {})
    receipt = transport.send_command(command)
    assert transport.get_command_receipt(command.command_id) == receipt
    calls: list[str] = []

    def planner(value: Command) -> NormalizedPlan:
        calls.append(value.command_id)
        return normalized

    handler = PlanningCommandHandler(transport, planner, topic="plans")
    assert handler.run_once(commands).status == "completed"
    event_delivery = plans.receive()
    assert event_delivery is not None
    event = event_delivery.message
    assert event.event_kind == "normalized-plan"
    assert event.payload["correlation_id"] == "file-correlation"
    assert event.payload["plan_revision"] == 7
    assert event.payload["normalized_plan_document"] == normalized.to_canonical_json()
    assert event.payload["normalized_plan_sha256"]
    event_delivery.ack()
    outcome_delivery = commands.receive()
    assert outcome_delivery is not None and outcome_delivery.message.status == "completed"
    outcome_delivery.ack()
    commands.close()
    plans.close()

    restarted = FileTransport(tmp_path, subscriptions)
    restarted_commands = restarted.open_consumer(subscriptions[0])
    restarted_plans = restarted.open_consumer(subscriptions[1])
    restarted.send_command(command)
    assert restarted.get_command_receipt(command.command_id) == receipt
    assert PlanningCommandHandler(restarted, planner, topic="plans").run_once(restarted_commands) is None
    assert restarted_plans.receive() is None
    assert calls == ["file-command"]
    restarted_commands.close()
    restarted_plans.close()
