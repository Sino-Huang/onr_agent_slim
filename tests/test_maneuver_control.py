from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from collections.abc import Mapping, MutableMapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from onr.adapters.inprocess_transport import InProcessTransport, InProcessTransportState
from onr.application.context_coordination import ContextCoordination
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.application.maneuver_control import ManeuverControl, ManeuverHeartbeatResult
from onr.application.planning_commands import PlanningCommandHandler
from onr.agents.maneuver_control import DeepAgentsDecisionProvider
from onr.application.symbolic_planning import SymbolicPlanning
from onr.application.temporal_planning import TemporalPlanning
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus, ManeuverDecision, ManeuverFeedback, TransitionCandidate
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    NonPhysicalChoice,
    PhysicalAction,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    NormalizedPlan,
    PlannerChoice,
    PlannerExecutionResult,
    PlanningOutcome,
    SymbolicActionCall,
    SymbolicManeuver,
    SymbolicMissionSpec,
    SymbolicPlannerExecutionResult,
    TemporalAssignment,
    TemporalManeuver,
)
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    TransportEvent,
    create_normalized_plan_transport_event,
)
from onr.ports.transport import Subscription


class RecordingAdapter:
    def __init__(self) -> None:
        self.commands: list[ManeuverCommand] = []

    def submit(self, command: ManeuverCommand) -> object:
        self.commands.append(command)
        return {"adapter_receipt": command.command_id}


class FailingAdapter:
    def __init__(self) -> None:
        self.attempts = 0

    def submit(self, command: ManeuverCommand) -> object:
        self.attempts += 1
        raise RuntimeError("adapter unavailable")


class InterruptingAdapter:
    def __init__(self) -> None:
        self.commands: list[ManeuverCommand] = []

    def submit(self, command: ManeuverCommand) -> object:
        self.commands.append(command)
        raise KeyboardInterrupt("process interrupted")


class FixedDecisionProvider:
    def __init__(self, decision: ManeuverControlDecision) -> None:
        self.decision = decision
        self.calls = 0

    def decide(self, snapshot: MissionSnapshot, status: FSMStatus, overlay: InvocationOverlay | None = None) -> ManeuverControlDecision:
        self.calls += 1
        return self.decision


def test_deep_agents_decision_provider_unwraps_message_state() -> None:
    expected = ManeuverControlDecision(
        decision_id="decision-1",
        mission_id="mission-1",
        plan_revision=1,
        maneuver_id="survey",
        physical_intent=ManeuverIntent(PhysicalAction.NAVIGATE),
    )

    class Agent:
        def invoke(self, _: object) -> dict[str, object]:
            return {
                "messages": [SimpleNamespace(content=json.dumps(expected.to_dict()))],
                "files": {},
            }

    provider = DeepAgentsDecisionProvider(Agent())
    result = provider.decide(
        cast(MissionSnapshot, SimpleNamespace(to_dict=lambda: {})),
        cast(FSMStatus, SimpleNamespace(to_dict=lambda: {})),
    )

    assert result == expected


class AlternatingDecisionProvider:
    def __init__(self, decisions: tuple[ManeuverControlDecision, ...]) -> None:
        self.decisions = decisions
        self.calls = 0

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
    ) -> ManeuverControlDecision:
        _ = snapshot, status, overlay
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return decision


class FakeTemporalExecutor:
    def __init__(self, result: PlannerExecutionResult) -> None:
        self.result = result

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        _ = assets
        return self.result


class FakeSymbolicExecutor:
    def __init__(self, result: SymbolicPlannerExecutionResult) -> None:
        self.result = result

    def execute(self, assets: Mapping[str, bytes]) -> SymbolicPlannerExecutionResult:
        _ = assets
        return self.result


def _normalized_plan(planning_profile: str) -> NormalizedPlan:
    intent = ManeuverIntent(
        PhysicalAction.NAVIGATE,
        (ManeuverParameter("waypoint", "area-7"),),
    )
    if planning_profile == "temporal":
        choice = PlannerChoice("temporal", "minizinc")
        mission = MissionSpec(
            "mission-temporal",
            "navigate",
            choice,
            (TemporalManeuver("survey", intent, (), 1),),
            2,
            "authority",
        )
        result = TemporalPlanning(
            FakeTemporalExecutor(
                PlannerExecutionResult(
                    PlanningOutcome.SOLVED,
                    (TemporalAssignment("survey", 0, 1),),
                )
            )
        ).plan(mission, 7, "snapshot-7")
        return result.normalized_plan

    choice = PlannerChoice("symbolic", "fast-downward")
    mission = SymbolicMissionSpec(
        "mission-symbolic",
        "navigate",
        choice,
        (SymbolicManeuver("survey", intent, (), 1),),
        "authority",
        1,
    )
    result = SymbolicPlanning(
        FakeSymbolicExecutor(
            SymbolicPlannerExecutionResult(
                PlanningOutcome.SOLVED,
                (SymbolicActionCall("survey"),),
                1,
            )
        )
    ).plan(mission, 7, "snapshot-7")
    return result.normalized_plan


def _snapshot(mission_id: str, revision: int) -> MissionSnapshot:
    return MissionSnapshot(
        mission_id=mission_id,
        version=revision,
        created_at=f"t-{revision}",
        plan_revision=revision,
        plan_reference=f"plan-{revision}",
        source_revisions={"plan": revision},
        source_references={"plan": f"plan-{revision}"},
        source_health={"plan": "ok"},
        source_freshness={"plan": True},
    )


def _status(mission_id: str, revision: int) -> FSMStatus:
    return FSMStatus(
        mission_id=mission_id,
        plan_revision=revision,
        statechart_revision=revision,
        active_state="ready",
        transition_candidates=(
            TransitionCandidate("advance:survey", "ready", "active"),
        ),
    )


@pytest.mark.parametrize(
    ("planning_profile", "planner_path"),
    (("temporal", "MiniZinc"), ("symbolic", "Fast Downward")),
)
def test_plan_revision_reaches_one_authoritative_maneuver_once(
    planning_profile: str, planner_path: str
) -> None:
    """Run the real planner-normalization/context/FSM/transport vertical seam."""

    normalized = _normalized_plan(planning_profile)
    mission_id = normalized.mission_spec.mission_id
    revision = normalized.plan_revision
    planning_subscription = Subscription("planner", mission_id, "plan")
    context_subscription = ContextCoordination.subscription_for(mission_id)
    fsm_subscription = FSMRunner.subscription_for(mission_id)
    maneuver_subscription = Subscription("maneuver-control", mission_id, "maneuver-input")
    transport = InProcessTransport(
        (planning_subscription, context_subscription, fsm_subscription, maneuver_subscription)
    )
    planning_consumer = transport.open_consumer(planning_subscription)
    context_consumer = transport.open_consumer(context_subscription)
    fsm_consumer = transport.open_consumer(fsm_subscription)
    transport.send_command(
        Command(
            1,
            f"plan-command-{planning_profile}",
            "plan-correlation",
            mission_id,
            "planner",
            "plan",
            {},
        )
    )
    planning_outcome = PlanningCommandHandler(
        cast(Any, transport), lambda _: normalized
    ).run_once(planning_consumer)
    assert planning_outcome is not None and planning_outcome.status == "completed"

    context = ContextCoordination(
        cast(Any, transport),
        mission_id,
        clock=lambda: "t-1",
        subscription=context_subscription,
    )
    snapshot = context.run_once(context_consumer)
    assert snapshot is not None and snapshot.plan_revision == revision
    fsm = FSMRunner(
        cast(Any, transport),
        store=InMemoryFSMStateStore(),
        clock=lambda: 1,
        subscription=fsm_subscription,
    )
    status = asyncio.run(fsm.run_once(fsm_consumer))
    assert isinstance(status, FSMStatus)
    context.publish_source_fact(
        "fsm_status", revision, reference=f"fsm-status:{mission_id}:{revision}"
    )
    snapshot = context.run_once(context_consumer)
    assert snapshot is not None and snapshot.fsm_status is not None

    decision = ManeuverControlDecision(
        "decision-7",
        mission_id,
        revision,
        maneuver_id="survey",
        physical_intent=normalized.maneuvers[0].intent,
    )
    provider = FixedDecisionProvider(decision)
    adapter = RecordingAdapter()
    control = ManeuverControl(cast(Any, transport), adapter, provider, target_service="maneuver-adapter")
    event = TransportEvent(
        1,
        f"plan-event-{planner_path.lower().replace(' ', '-')}",
        mission_id,
        0,
        "maneuver-input",
        {"snapshot": snapshot.to_dict(), "fsm_status": status.to_dict()},
    )
    transport.publish_event("maneuver-input", event)
    maneuver_consumer = transport.open_consumer(maneuver_subscription)
    result = asyncio.run(control.run_once(maneuver_consumer))
    maneuver_consumer.close()
    transport.publish_event("maneuver-input", event)
    replay_consumer = transport.open_consumer(maneuver_subscription)
    assert asyncio.run(control.run_once(replay_consumer)) is None
    replay_consumer.close()
    planning_consumer.close()
    context_consumer.close()
    fsm_consumer.close()

    assert isinstance(result, ManeuverHeartbeatResult) and result.command is not None
    assert result.command.plan_revision == revision
    assert result.command.mission_snapshot_id == f"{mission_id}:snapshot:{snapshot.version}"
    generic = result.command.to_command("maneuver-adapter")
    outcome = control.handle_command(generic)
    assert isinstance(outcome, CommandOutcome)
    assert outcome.status == "accepted"
    assert outcome.command_id == generic.command_id
    assert outcome.correlation_id == generic.correlation_id
    assert outcome.mission_id == generic.mission_id
    assert outcome.payload == {
        "adapter_submission": "accepted",
        "source": "maneuver-adapter-transport",
    }
    assert control.handle_command(generic) == outcome
    candidate = status.transition_candidates[0]
    transition_decision = ManeuverDecision(
        "transition-after-feedback", mission_id, transition_event=candidate.event
    )
    if planning_profile == "symbolic":
        without_feedback = asyncio.run(fsm.apply(candidate, transition_decision))
        assert without_feedback.active_state == status.active_state
    moved = asyncio.run(
        fsm.apply(
            candidate,
            ManeuverFeedback("feedback-after-submit", mission_id, "survey", "completed"),
            transition_decision,
        )
    )
    assert moved.active_state == candidate.target
    queued = transport.state.commands[("maneuver-adapter", mission_id)]
    assert sum(isinstance(message, Command) for _, message in queued) == 1
    assert sum(isinstance(message, CommandOutcome) for _, message in queued) == 1
    assert len(adapter.commands) == 1
    assert adapter.commands[0].intent.action == PhysicalAction.NAVIGATE
    assert provider.calls == 1


def test_physical_decision_matches_enabled_fsm_maneuver_and_plan() -> None:
    mission_id = "mission-context"
    snapshot = _snapshot(mission_id, 1)
    status = _status(mission_id, 1)
    stale = ManeuverControlDecision(
        "stale", mission_id, 1, maneuver_id="unknown", action="navigate"
    )
    control = ManeuverControl(
        cast(Any, InProcessTransport()), RecordingAdapter(), FixedDecisionProvider(stale)
    )
    with pytest.raises(ValueError, match="not enabled"):
        control.decide(snapshot, status)

    plan = _normalized_plan("symbolic")
    mismatch = ManeuverControlDecision(
        "mismatch",
        plan.mission_spec.mission_id,
        plan.plan_revision,
        maneuver_id="survey",
        action="land",
    )
    plan_control = ManeuverControl(
        cast(Any, InProcessTransport()), RecordingAdapter(), FixedDecisionProvider(mismatch)
    )
    with pytest.raises(ValueError, match="does not match the normalized plan"):
        plan_control.decide(
            _snapshot(plan.mission_spec.mission_id, plan.plan_revision),
            _status(plan.mission_spec.mission_id, plan.plan_revision),
            plan=plan,
        )


def test_adapter_failure_publishes_failed_outcome_and_retries_once() -> None:
    mission_id = "mission-failure"
    subscription = Subscription("maneuver-adapter", mission_id, "maneuver")
    transport = InProcessTransport((subscription,))
    adapter = FailingAdapter()
    decision = ManeuverControlDecision(
        "failure-decision", mission_id, 1, maneuver_id="survey", action="navigate"
    )
    control = ManeuverControl(
        cast(Any, transport), adapter, FixedDecisionProvider(decision)
    )
    result = control.heartbeat(_snapshot(mission_id, 1), _status(mission_id, 1))
    assert result.command is not None
    consumer = transport.open_consumer(subscription)
    with pytest.raises(RuntimeError, match="adapter unavailable"):
        asyncio.run(control.run_once(consumer))
    failed = transport.get_command_outcome(result.command.command_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.command_id == result.command.command_id
    assert failed.correlation_id == result.command.correlation_id
    assert failed.mission_id == mission_id
    assert failed.payload == {
        "adapter_submission": "failed",
        "error": "adapter unavailable",
        "source": "maneuver-adapter-transport",
    }
    assert asyncio.run(control.run_once(consumer)) == failed
    assert asyncio.run(control.run_once(consumer)) == failed
    assert adapter.attempts == 1
    assert transport.latest_event("maneuver-feedback", mission_id) is None
    consumer.close()


def test_adapter_submission_marker_survives_maneuver_control_restart() -> None:
    mission_id = "mission-restart"
    revision = 3
    snapshot = _snapshot(mission_id, revision)
    status = _status(mission_id, revision)
    first_decision = ManeuverControlDecision(
        "decision-first", mission_id, revision, maneuver_id="survey", action="navigate"
    )
    adapter = RecordingAdapter()
    state = InProcessTransportState()
    first_transport = InProcessTransport(state=state)
    first = ManeuverControl(
        cast(Any, first_transport), adapter, FixedDecisionProvider(first_decision)
    )
    first_result = first.heartbeat(snapshot, status)
    assert first_result.command is not None
    generic = first_result.command.to_command("maneuver-adapter")
    first_outcome = first.handle_command(generic)
    assert isinstance(first_outcome, CommandOutcome)
    assert first_outcome.status == "accepted"

    second_decision = ManeuverControlDecision(
        "decision-second", mission_id, revision, maneuver_id="survey", action="navigate"
    )
    marker_topic = f"maneuver-submissions/{generic.command_id}"
    second_transport = InProcessTransport(
        (
            Subscription("maneuver-adapter", mission_id, "maneuver"),
            Subscription("maneuver-control", mission_id, marker_topic),
        ),
        state=state,
    )
    second = ManeuverControl(
        cast(Any, second_transport), adapter, FixedDecisionProvider(second_decision)
    )
    second_result = second.heartbeat(snapshot, status)
    assert second_result.command is not None
    assert second_result.command.command_id == first_result.command.command_id
    assert second.handle_command(generic) == first_outcome
    consumer = second_transport.open_consumer(
        Subscription("maneuver-adapter", mission_id, "maneuver")
    )
    assert asyncio.run(second.run_once(consumer)) == first_outcome
    assert asyncio.run(second.run_once(consumer)) == first_outcome
    consumer.close()
    marker_consumer = second_transport.open_consumer(
        Subscription("maneuver-control", mission_id, marker_topic)
    )
    assert isinstance(asyncio.run(second.run_once(marker_consumer)), TransportEvent)
    assert asyncio.run(second.run_once(marker_consumer)) is None
    marker_consumer.close()
    assert len(adapter.commands) == 1


def test_submission_intent_prevents_resubmit_after_crash_window() -> None:
    mission_id = "mission-crash-window"
    revision = 1
    state = InProcessTransportState()
    first_transport = InProcessTransport(state=state)
    interrupted = InterruptingAdapter()
    decision = ManeuverControlDecision(
        "crash-window-decision", mission_id, revision, maneuver_id="survey", action="navigate"
    )
    first = ManeuverControl(
        cast(Any, first_transport), interrupted, FixedDecisionProvider(decision)
    )
    command = ManeuverCommand(
        "crash-window-command",
        "crash-window-command",
        mission_id,
        revision,
        "survey",
        ManeuverIntent("navigate"),
    )
    generic = command.to_command("maneuver-adapter")

    with pytest.raises(KeyboardInterrupt, match="process interrupted"):
        first.handle_command(generic)
    assert len(interrupted.commands) == 1
    assert first_transport.latest_event(
        "maneuver-submissions-intents/crash-window-command",
        mission_id,
        event_kind="maneuver-submission-intent",
    ) is not None
    assert first_transport.get_command_outcome(command.command_id) is None

    restarted_adapter = RecordingAdapter()
    second_transport = InProcessTransport(state=state)
    second = ManeuverControl(
        cast(Any, second_transport), restarted_adapter, FixedDecisionProvider(decision)
    )
    outcome = second.handle_command(generic)

    assert outcome.status == "failed"
    assert outcome.payload == {
        "adapter_submission": "unknown",
        "error": "prior adapter submission outcome is unknown; command will not be submitted again",
        "source": "maneuver-adapter-transport",
    }
    assert second_transport.get_command_outcome(command.command_id) == outcome
    assert restarted_adapter.commands == []
    assert second_transport.latest_event(
        "maneuver-submissions/crash-window-command",
        mission_id,
        event_kind="maneuver-submitted",
    ) is None


def test_adapter_submission_does_not_advance_fsm_without_feedback() -> None:
    plan = _normalized_plan("symbolic")
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    initial = asyncio.run(
        runner.activate(
            create_normalized_plan_transport_event(plan, event_id="plan-event", sequence=0)
        )
    )
    candidate = initial.transition_candidates[0]
    snapshot = _snapshot(plan.mission_spec.mission_id, plan.plan_revision)
    decision = ManeuverControlDecision(
        "physical-decision",
        plan.mission_spec.mission_id,
        plan.plan_revision,
        maneuver_id="survey",
        physical_intent=plan.maneuvers[0].intent,
    )
    adapter = RecordingAdapter()
    control = ManeuverControl(cast(Any, transport), adapter, FixedDecisionProvider(decision))
    result = control.heartbeat(snapshot, initial)
    assert result.command is not None
    control.handle_command(result.command.to_command("maneuver-adapter"))
    unchanged = asyncio.run(
        runner.apply(
            candidate,
            ManeuverDecision("transition-decision", plan.mission_spec.mission_id, transition_event=candidate.event),
        )
    )
    assert unchanged.active_state == initial.active_state
    assert unchanged.lifecycle_facts == {}
    assert transport.latest_event("maneuver-feedback", plan.mission_spec.mission_id) is None

    moved = asyncio.run(
        runner.apply(
            candidate,
            ManeuverFeedback(
                "feedback-1", plan.mission_spec.mission_id, "survey", "completed"
            ),
            ManeuverDecision("transition-decision", plan.mission_spec.mission_id, transition_event=candidate.event),
        )
    )
    assert moved.active_state == candidate.target
    assert moved.lifecycle_facts["survey"] == "completed"
    assert len(adapter.commands) == 1


def test_cancel_maneuver_is_non_physical_until_feedback() -> None:
    mission_id = "mission-cancel"
    revision = 1
    decision = ManeuverControlDecision(
        "cancel-request",
        mission_id,
        revision,
        maneuver_id="survey",
        choice=NonPhysicalChoice.CANCEL_MANEUVER,
    )
    adapter = RecordingAdapter()
    control = ManeuverControl(
        cast(Any, InProcessTransport()), adapter, FixedDecisionProvider(decision)
    )
    result = control.heartbeat(_snapshot(mission_id, revision), _status(mission_id, revision))
    assert result.command is None
    assert adapter.commands == []
    assert "cancelled" not in result.decision.payload


def test_decisions_validate_physical_actions_and_non_physical_choices() -> None:
    for action in PhysicalAction:
        decision = ManeuverControlDecision(
            f"decision-{action}", "mission", 1, maneuver_id="m", physical_intent=ManeuverIntent(action)
        )
        assert decision.physical_intent is not None

    for choice in NonPhysicalChoice:
        if choice is NonPhysicalChoice.TRANSITION:
            with pytest.raises(ValueError):
                ManeuverControlDecision(f"decision-{choice}", "mission", 1, choice=choice)
            continue
        if choice is NonPhysicalChoice.CANCEL_MANEUVER:
            with pytest.raises(ValueError):
                ManeuverControlDecision(f"decision-{choice}", "mission", 1, choice=choice)
            decision = ManeuverControlDecision(
                f"decision-{choice}", "mission", 1, maneuver_id="m", choice=choice
            )
        else:
            decision = ManeuverControlDecision(f"decision-{choice}", "mission", 1, choice=choice)
        assert decision.choice is choice
    transition = ManeuverControlDecision(
        "transition", "mission", 1, choice=NonPhysicalChoice.TRANSITION, transition_event="advance"
    )
    assert transition.event == "advance"
    normalized = ManeuverControlDecision("normalized", "mission", 1, transition_event="advance")
    assert normalized.choice is NonPhysicalChoice.TRANSITION

    advisory = ManeuverControlDecision(
        "advisory", "mission", 1, choice=NonPhysicalChoice.REPLAN,
        maneuver_id="m", physical_intent=ManeuverIntent("navigate")
    )
    assert advisory.physical_intent is not None and advisory.choice is NonPhysicalChoice.REPLAN
    with pytest.raises(ValueError):
        ManeuverControlDecision(
            "physical-transition",
            "mission",
            1,
            choice=NonPhysicalChoice.TRANSITION,
            transition_event="advance",
            maneuver_id="m",
            physical_intent=ManeuverIntent("navigate"),
        )
    with pytest.raises(ValueError):
        ManeuverControlDecision(
            "physical-cancel",
            "mission",
            1,
            choice=NonPhysicalChoice.CANCEL_MANEUVER,
            maneuver_id="m",
            physical_intent=ManeuverIntent("navigate"),
        )
    for payload in (
        {"nested": {"status": "completed"}},
        {"items": [{"lifecycle": "active"}]},
    ):
        with pytest.raises(ValueError):
            ManeuverControlDecision(
                "nested-lifecycle", "mission", 1, choice=NonPhysicalChoice.REPORT, payload=payload
            )


def test_overlay_is_immutable_and_transient() -> None:
    snapshot = _snapshot("mission", 1)
    status = _status("mission", 1)
    overlay = InvocationOverlay(
        "mission", "request-1", {"snapshot": snapshot.to_dict(), "fsm_status": status.to_dict()}
    )
    assert overlay.to_dict()["values"] == {
        "snapshot": snapshot.to_dict(),
        "fsm_status": status.to_dict(),
    }
    with pytest.raises((FrozenInstanceError, TypeError)):
        setattr(cast(object, overlay), "request_id", "request-2")
    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], overlay.values)["new"] = "not-authority"
    assert snapshot.plan_revision == 1
    assert status.active_state == "ready"


def test_replaying_event_rehydrates_the_stored_maneuver_decision_and_command() -> None:
    mission_id = "mission-replay"
    snapshot = _snapshot(mission_id, 1)
    status = _status(mission_id, 1)
    first = ManeuverControlDecision(
        "decision-first", mission_id, 1, maneuver_id="survey", action="navigate"
    )
    second = ManeuverControlDecision(
        "decision-second", mission_id, 1, choice=NonPhysicalChoice.REPORT
    )
    provider = AlternatingDecisionProvider((first, second))
    transport = InProcessTransport()
    control = ManeuverControl(cast(Any, transport), RecordingAdapter(), provider)
    event = TransportEvent(
        1,
        "maneuver-input-1",
        mission_id,
        0,
        "maneuver-input",
        {"snapshot": snapshot.to_dict(), "fsm_status": status.to_dict()},
    )

    original = asyncio.run(control.run_once(event))
    replay = asyncio.run(control.run_once(event))

    assert isinstance(original, ManeuverHeartbeatResult)
    assert isinstance(replay, ManeuverHeartbeatResult)
    assert original.decision == first
    assert replay.decision == original.decision
    assert replay.command == original.command
    assert replay.receipt == original.receipt
    assert provider.calls == 1


def test_maneuver_command_converts_to_generic_command_without_feedback() -> None:
    command = ManeuverCommand(
        "maneuver-command",
        "correlation",
        "mission",
        3,
        "survey",
        ManeuverIntent("investigate"),
    )
    generic = command.to_command("maneuver-adapter")
    restored = ManeuverCommand.from_command(generic)
    assert restored == command
    assert ManeuverCommand.from_json(command.to_canonical_json()) == command
    assert generic.command_kind == "maneuver"
