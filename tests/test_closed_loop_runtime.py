from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

from langchain.tools import ToolRuntime

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.file_transport import FileTransport
from onr.agents.maneuver_tools import ManeuverToolContext, ingest_perceptions
from onr.application.bayesian_belief import BayesianBeliefManager, BayesianBeliefService
from onr.application.communication import TransportCommunicationPort
from onr.application.context_coordination import (
    ActivePlanRevision,
    ContextCoordination,
)
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.application.hyper_supervisor import HyperSupervisor
from onr.application.maneuver_control import ManeuverControl
from onr.contracts.bayesian_belief import BeliefKey
from onr.contracts.communication import AgentMessage
from onr.contracts.fsm import ManeuverDecision, Statechart, StatechartTransition
from onr.contracts.hyper_agent import (
    HyperHeartbeatDecision,
    HyperHeartbeatInvocation,
    ReplanRequest,
)
from onr.contracts.maneuver_control import (
    ManeuverControlDecision,
    ManeuverHeartbeatCompletion,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    PlannerChoice,
    PlannerPlan,
    PlanningOutcome,
)
from onr.demo.environment_updates import (
    CoordinatorDrivenFakeEnvironment,
    EnvironmentDrivenFakeEnvironment,
)
from onr.demo.fake_environment import FakeEnvironment
from onr.ports.transport import Subscription


def _report(path: Path, records: list[dict[str, object]] | None = None) -> Path:
    path.write_text(
        json.dumps(
            records
            or [
                {
                    "time": 100.0,
                    "position": [0.0, 0.0, -250.0],
                    "event information": {"decision": "left"},
                    "event type": "intersection decision",
                    "entity_id": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


def _revision(number: int, *, terminal_event: str = "finish") -> ActivePlanRevision:
    snapshot_id = f"mission-1:snapshot:revision-{number}"
    plan = PlannerPlan(
        "mission-1",
        "operator",
        number,
        snapshot_id,
        PlannerChoice("temporal", "minizinc"),
        PlanningOutcome.SOLVED,
        f"planner-native-{number}.plan",
    )
    if number == 1:
        entry_state = "waiting"
        states = ("waiting", "observing", "complete")
        transitions = (
            StatechartTransition("begin", "waiting", "observing", {}),
            StatechartTransition(terminal_event, "observing", "complete", {}),
        )
    else:
        entry_state = "observing"
        states = ("observing", "complete")
        transitions = (
            StatechartTransition(terminal_event, "observing", "complete", {}),
        )
    chart = Statechart(
        mission_id="mission-1",
        plan_revision=number,
        mission_snapshot_id=snapshot_id,
        planning_profile="temporal",
        entry_state=entry_state,
        states=states,
        transitions=transitions,
        terminal_states=("complete",),
        state_context={state: {} for state in states},
    )
    return ActivePlanRevision(
        plan,
        f"planner-plan-{number}.json",
        chart,
        f"statechart-{number}.json",
    )


class _ManeuverProvider:
    def __init__(self, communication: TransportCommunicationPort) -> None:
        self.communication = communication
        self.invocations: list[Any] = []
        self.sent = False

    def heartbeat(self, invocation, context):  # type: ignore[no-untyped-def]
        self.invocations.append(invocation)
        now = invocation.environment_data["scene_graph"]["mission_time_seconds"]
        if now == 5:
            self._transition(invocation, context, "begin")
        elif now == 10 and not self.sent:
            status = asyncio.run(context.fsm_runner.status())
            context.transition_intents.select(
                status,
                status.transition_candidates[0].target,
                "Select the revision-one terminal target.",
                selected_at=now,
            )
            request = ReplanRequest(
                "maneuver-request-1",
                invocation.mission_id,
                "Evaluate new live evidence.",
                "maneuver-control",
                invocation.plan_revision,
                dict(invocation.planning_snapshot.source_revisions),
            )
            outcome = self.communication.request(
                AgentMessage(
                    message_id=request.request_id,
                    correlation_id=invocation.correlation_id,
                    mission_id=invocation.mission_id,
                    plan_revision=invocation.plan_revision,
                    sender="maneuver-control",
                    recipient="hyper-agent",
                    kind="replan",
                    payload={
                        "message": request.reason,
                        "replan_request": request.to_dict(),
                    },
                )
            )
            assert outcome.payload["disposition"] == "pending_hyper_evaluation"
            self.sent = True
        elif now == 15:
            event = "finish" if invocation.plan_revision == 1 else "finish-revision-2"
            self._transition(invocation, context, event)
        return ManeuverHeartbeatCompletion(
            invocation.mission_id,
            invocation.request_id,
            "Deterministic test heartbeat.",
        )

    @staticmethod
    def _transition(invocation, context, event: str) -> None:  # type: ignore[no-untyped-def]
        status = asyncio.run(context.fsm_runner.status())
        candidate = next(
            item for item in status.transition_candidates if item.event == event
        )
        decision = ManeuverDecision(
            decision_id=f"decision:{invocation.request_id}:{event}",
            mission_id=invocation.mission_id,
            transition_event=event,
            payload={"plan_revision": invocation.plan_revision},
        )
        asyncio.run(context.fsm_runner.apply(candidate, decision))


def _runtime_parts(
    tmp_path: Path,
    hyper_provider: object,
    *,
    report_records: list[dict[str, object]] | None = None,
    belief_keys: tuple[BeliefKey, ...] = (BeliefKey("1", "event-risk"),),
    environment_driven: bool = False,
    environment_cadence_seconds: float | None = None,
):
    mission_id = "mission-1"
    context_subscription = Subscription(
        "context-coordination", mission_id, "planning-evidence"
    )
    belief_subscription = Subscription(
        "belief-manager", mission_id, "belief-observations"
    )
    transport = FileTransport(
        tmp_path / "transport", (context_subscription, belief_subscription)
    )
    environment = FakeEnvironment(
        transport,
        mission_id,
        event_report_path=_report(tmp_path / "report.json", report_records),
        context_topic="planning-evidence",
    )
    environment.heartbeat()
    source_type = (
        EnvironmentDrivenFakeEnvironment
        if environment_driven
        else CoordinatorDrivenFakeEnvironment
    )
    environment_updates = source_type(
        environment,
        cadence_seconds=environment_cadence_seconds or environment.tick_seconds,
    )
    belief = BayesianBeliefService(
        BayesianBeliefManager(mission_id, belief_keys, particle_count=64, seed=3),
        FileBayesianBeliefStore(tmp_path / "belief"),
        transport,
        context_topic="planning-evidence",
        subscription=belief_subscription,
    )
    fsm = FSMRunner(transport, store=InMemoryFSMStateStore())
    supervisor = HyperSupervisor(hyper_provider, transport=transport)
    communication = TransportCommunicationPort(transport)
    communication.register("hyper-agent", supervisor.handle_agent_message)
    provider = _ManeuverProvider(communication)
    maneuver = ManeuverControl(
        transport,
        provider,
        fsm_runner=fsm,
        belief_service=belief,
        communication_port=communication,
    )

    def coordinator(
        replan_workflow,  # type: ignore[no-untyped-def]
        *,
        simulation_limit_seconds: float = 20,
    ) -> ContextCoordination:
        return ContextCoordination(
            transport,
            mission_id,
            input_topic="planning-evidence",
            subscription=context_subscription,
            clock=lambda: "2026-08-23T00:00:00+10:00",
            environment_update_source=environment_updates,
            fsm_runner=fsm,
            maneuver_control=maneuver,
            hyper_supervisor=supervisor,
            belief_service=belief,
            replan_workflow=replan_workflow,
            simulation_limit_seconds=simulation_limit_seconds,
        )

    return environment, coordinator, belief, fsm, supervisor, maneuver, provider


def test_fixed_rate_loop_coalesces_request_with_periodic_and_injects_outcome(
    tmp_path: Path,
) -> None:
    hyper_invocations: list[HyperHeartbeatInvocation] = []

    def hyper(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        hyper_invocations.append(invocation)
        return HyperHeartbeatDecision(
            invocation.mission_id,
            invocation.plan_revision,
            "no_change",
            "The new evidence does not invalidate the active route.",
            invocation.trigger_identities,
            ("maneuver-request-1",),
        )

    _environment, coordinator, _belief, _fsm, _supervisor, _maneuver, provider = (
        _runtime_parts(tmp_path, hyper)
    )
    active = _revision(1)
    result = coordinator(lambda *_: None).run(active)

    assert result.terminal
    assert result.simulated_duration_seconds == 15
    assert result.tick_count == 30
    assert result.maneuver_heartbeat_count == 4
    assert result.hyper_heartbeat_count == 1
    assert len(hyper_invocations) == 1
    assert hyper_invocations[0].trigger_identities == (
        "maneuver-request-1",
        "periodic:10",
    )
    assert hyper_invocations[0].maneuver_requests[0].coalesced_request_ids == (
        "maneuver-request-1",
    )
    assert provider.invocations[-1].hyper_outcomes[0].disposition == "no_change"


def test_successful_replan_supersedes_fsm_without_reentrant_activation(
    tmp_path: Path,
) -> None:
    def hyper(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        return HyperHeartbeatDecision(
            invocation.mission_id,
            invocation.plan_revision,
            "replan",
            "The requested revision is warranted.",
            invocation.trigger_identities,
            ("maneuver-request-1",),
        )

    environment, coordinator, _belief, _fsm, _supervisor, maneuver, provider = (
        _runtime_parts(tmp_path, hyper)
    )
    replacement = _revision(2, terminal_event="finish-revision-2")
    callback_times: list[float] = []

    def replan(*args):  # type: ignore[no-untyped-def]
        callback_times.append(environment.mission_time_seconds)
        assert not provider.invocations[-1].hyper_outcomes
        return replacement

    result = coordinator(replan).run(_revision(1))

    assert callback_times == [10]
    assert result.plan_revisions == (1, 2)
    assert result.final_fsm_state == "complete"
    assert provider.invocations[-1].plan_revision == 2
    reconciliation = next(
        invocation
        for invocation in provider.invocations
        if invocation.trigger_identities == ("replan-activated:2",)
    )
    assert reconciliation.environment_data["scene_graph"]["mission_time_seconds"] == 10
    assert reconciliation.hyper_outcomes[0].disposition == "replan"
    assert reconciliation.planning_snapshot.plan_revision == 2
    assert (
        reconciliation.fsm_context.current_state == replacement.statechart.entry_state
    )
    assert reconciliation.fsm_context.transition_intent is None
    assert maneuver.transition_intents.latest("mission-1").status == "invalidated"


def test_failed_replan_keeps_revision_one_authoritative(tmp_path: Path) -> None:
    def hyper(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        return HyperHeartbeatDecision(
            invocation.mission_id,
            invocation.plan_revision,
            "replan",
            "Evaluate a replacement.",
            invocation.trigger_identities,
            ("maneuver-request-1",),
        )

    _environment, coordinator, _belief, _fsm, _supervisor, _maneuver, _provider = (
        _runtime_parts(tmp_path, hyper)
    )
    result = coordinator(lambda *_: None).run(_revision(1))

    assert result.terminal
    assert result.plan_revisions == (1,)
    assert result.final_fsm_state == "complete"


def test_navigation_completion_feedback_triggers_maneuver_before_five_seconds(
    tmp_path: Path,
) -> None:
    def hyper(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        raise AssertionError(f"Hyper should not run: {invocation.trigger_identities}")

    _environment, coordinator, _belief, _fsm, _supervisor, maneuver, _provider = (
        _runtime_parts(tmp_path, hyper)
    )
    base = _revision(1)
    chart = Statechart(
        mission_id="mission-1",
        plan_revision=1,
        mission_snapshot_id=base.planner_plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="travelling",
        states=("travelling", "complete"),
        transitions=(
            StatechartTransition("navigation-complete", "travelling", "complete", {}),
        ),
        terminal_states=("complete",),
        state_context={"travelling": {}, "complete": {}},
    )
    active = ActivePlanRevision(
        base.planner_plan,
        base.planner_plan_reference,
        chart,
        "statechart-navigation.json",
    )

    class NavigationProvider:
        def __init__(self) -> None:
            self.times: list[float] = []

        def heartbeat(self, invocation, tool_context):  # type: ignore[no-untyped-def]
            now = invocation.environment_data["scene_graph"]["mission_time_seconds"]
            self.times.append(now)
            if now == 0:
                tool_context.command_dispatcher.dispatch_physical(
                    invocation,
                    ManeuverControlDecision(
                        "navigate-now",
                        invocation.mission_id,
                        invocation.plan_revision,
                        maneuver_id="short-navigation",
                        physical_intent=ManeuverIntent(
                            "navigate",
                            (
                                ManeuverParameter("x", 2),
                                ManeuverParameter("y", 0),
                                ManeuverParameter("z", -250),
                                ManeuverParameter("speed", 2),
                            ),
                        ),
                    ),
                    sequence=1,
                )
            elif (
                invocation.environment_data["scene_graph"]["navigation_status"]
                == "completed"
            ):
                candidate = asyncio.run(
                    tool_context.fsm_runner.status()
                ).transition_candidates[0]
                asyncio.run(
                    tool_context.fsm_runner.apply(
                        candidate,
                        ManeuverDecision(
                            "confirm-navigation",
                            invocation.mission_id,
                            transition_event=candidate.event,
                            payload={"plan_revision": invocation.plan_revision},
                        ),
                    )
                )
            return ManeuverHeartbeatCompletion(
                invocation.mission_id,
                invocation.request_id,
                "Handled current navigation lifecycle evidence.",
            )

    navigation_provider = NavigationProvider()
    maneuver.decision_provider = navigation_provider
    result = coordinator(lambda *_: None, simulation_limit_seconds=10).run(active)

    assert navigation_provider.times == [0.0, 0.5, 1.0]
    assert result.maneuver_heartbeat_count == 3
    assert result.environment_triggered_maneuver_heartbeat_count == 2
    assert result.simulated_duration_seconds == 1.0
    assert result.final_fsm_state == "complete"


def test_four_perceptions_commit_four_ordered_beliefs_without_agent_belief(
    tmp_path: Path,
) -> None:
    records = [
        {
            "time": 0.5,
            "position": [10.0, 0.0, -250.0],
            "event information": {"decision": f"choice-{index}"},
            "event type": "intersection decision",
            "entity_id": index,
        }
        for index in range(1, 5)
    ]

    hyper_invocations: list[HyperHeartbeatInvocation] = []

    def hyper(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        hyper_invocations.append(invocation)
        return HyperHeartbeatDecision(
            invocation.mission_id,
            invocation.plan_revision,
            "no_change",
            "The active plan remains safe.",
            invocation.trigger_identities,
            (),
        )

    keys = tuple(BeliefKey(str(index), "event-risk") for index in range(1, 5))
    environment, coordinator, belief, _fsm, _supervisor, maneuver, _provider = (
        _runtime_parts(
            tmp_path,
            hyper,
            report_records=records,
            belief_keys=keys,
        )
    )

    class BatchProvider:
        def __init__(self) -> None:
            self.invocations: list[Any] = []

        def heartbeat(self, invocation, context):  # type: ignore[no-untyped-def]
            self.invocations.append(invocation)
            now = invocation.environment_data["scene_graph"]["mission_time_seconds"]
            if now == 0:
                context.command_dispatcher.dispatch_physical(
                    invocation,
                    ManeuverControlDecision(
                        "observe-four-events",
                        invocation.mission_id,
                        invocation.plan_revision,
                        maneuver_id="observe-four-events",
                        physical_intent=ManeuverIntent(
                            "navigate",
                            (
                                ManeuverParameter("x", 100),
                                ManeuverParameter("y", 0),
                                ManeuverParameter("z", -250),
                                ManeuverParameter("speed", 20),
                            ),
                        ),
                    ),
                    sequence=1,
                )
            elif now == 10:
                runtime = ToolRuntime(
                    state={"messages": []},
                    context=cast(ManeuverToolContext, context),
                    config={},
                    stream_writer=lambda _: None,
                    tool_call_id="batch",
                    store=None,
                )
                cast(Any, ingest_perceptions).func(
                    reflection="Ingest all four pending report events.",
                    runtime=runtime,
                )
            return ManeuverHeartbeatCompletion(
                invocation.mission_id,
                invocation.request_id,
                "Applied the current serialized effects.",
            )

    provider = BatchProvider()
    maneuver.decision_provider = provider
    result = coordinator(lambda *_: None, simulation_limit_seconds=10).run(_revision(1))

    assert [len(item.pending_perceptions) for item in provider.invocations] == [
        0,
        8,
        8,
        8,
    ]
    assert all(not hasattr(item, "belief_snapshot") for item in provider.invocations)
    assert all(len(item.request_id) < 100 for item in provider.invocations)
    assert any("periodic:5" in item.trigger_identities for item in provider.invocations)
    assert any(
        item.startswith("environment:")
        for item in provider.invocations[1].trigger_identities
    )
    assert result.environment_triggered_maneuver_heartbeat_count == 2
    assert result.perception_count == 8
    assert result.belief_revisions == (1, 2, 3, 4)
    assert hyper_invocations[0].belief_snapshot is not None
    assert hyper_invocations[0].belief_snapshot.belief_revision == 4
    assert belief.load_current_snapshot().belief_revision == 4  # type: ignore[union-attr]
    assert (
        environment.transport.next_event_sequence(belief.observation_topic, "mission-1")
        == 4
    )


def test_direct_maneuver_invocation_is_queued_without_overlap(tmp_path: Path) -> None:
    def hyper(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        raise AssertionError(f"Hyper should not run: {invocation.trigger_identities}")

    _environment, coordinator, _belief, _fsm, _supervisor, maneuver, _provider = (
        _runtime_parts(tmp_path, hyper)
    )
    base = _revision(1)
    direct_chart = Statechart(
        mission_id="mission-1",
        plan_revision=1,
        mission_snapshot_id=base.planner_plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="working",
        states=("working", "complete"),
        transitions=(StatechartTransition("finish", "working", "complete", {}),),
        terminal_states=("complete",),
        state_context={"working": {}, "complete": {}},
    )
    active = ActivePlanRevision(
        base.planner_plan,
        base.planner_plan_reference,
        direct_chart,
        "direct-statechart.json",
    )
    coordination = coordinator(lambda *_: None, simulation_limit_seconds=2)

    class DirectProvider:
        def __init__(self) -> None:
            self.depth = 0
            self.max_depth = 0
            self.times: list[float] = []

        def heartbeat(self, invocation, context):  # type: ignore[no-untyped-def]
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            now = invocation.environment_data["scene_graph"]["mission_time_seconds"]
            self.times.append(now)
            if now == 0:
                outcome = coordination.handle_agent_message(
                    AgentMessage(
                        message_id="direct-invoke-1",
                        correlation_id=invocation.correlation_id,
                        mission_id=invocation.mission_id,
                        plan_revision=invocation.plan_revision,
                        sender="hyper-agent",
                        recipient="maneuver-control",
                        kind="invoke",
                        payload={},
                    )
                )
                assert outcome["status"] == "queued"
            else:
                candidate = asyncio.run(
                    context.fsm_runner.status()
                ).transition_candidates[0]
                asyncio.run(
                    context.fsm_runner.apply(
                        candidate,
                        ManeuverDecision(
                            "direct-finish",
                            invocation.mission_id,
                            transition_event=candidate.event,
                            payload={"plan_revision": invocation.plan_revision},
                        ),
                    )
                )
            self.depth -= 1
            return ManeuverHeartbeatCompletion(
                invocation.mission_id,
                invocation.request_id,
                "Handled the serialized direct invocation.",
            )

    provider = DirectProvider()
    maneuver.decision_provider = provider
    result = coordination.run(active)

    assert result.terminal
    assert provider.times == [0.0, 0.5]
    assert provider.max_depth == 1


def test_environment_driven_updates_fold_during_blocked_inference(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()
    errors: list[BaseException] = []
    active_invocations = 0
    maximum_active_invocations = 0

    def hyper(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        nonlocal active_invocations, maximum_active_invocations
        active_invocations += 1
        maximum_active_invocations = max(maximum_active_invocations, active_invocations)
        active_invocations -= 1
        return HyperHeartbeatDecision(
            invocation.mission_id,
            invocation.plan_revision,
            "no_change",
            "The active plan remains valid.",
            invocation.trigger_identities,
            (),
        )

    records = [
        {
            "time": 0.5,
            "position": [1.0, 0.0, -250.0],
            "event information": {"decision": "left"},
            "event type": "intersection decision",
            "entity_id": 1,
        }
    ]
    environment, coordinator, _belief, _fsm, _supervisor, maneuver, _provider = (
        _runtime_parts(
            tmp_path,
            hyper,
            report_records=records,
            environment_driven=True,
            environment_cadence_seconds=0.01,
        )
    )
    base = _revision(1)
    chart = Statechart(
        mission_id="mission-1",
        plan_revision=1,
        mission_snapshot_id=base.planner_plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="working",
        states=("working", "complete"),
        transitions=(StatechartTransition("finish", "working", "complete", {}),),
        terminal_states=("complete",),
        state_context={"working": {}, "complete": {}},
    )
    revision = ActivePlanRevision(
        base.planner_plan,
        base.planner_plan_reference,
        chart,
        "environment-driven-statechart.json",
    )

    class BarrierProvider:
        def __init__(self) -> None:
            self.invocations: list[Any] = []

        def heartbeat(self, invocation, context):  # type: ignore[no-untyped-def]
            nonlocal active_invocations, maximum_active_invocations
            active_invocations += 1
            maximum_active_invocations = max(
                maximum_active_invocations, active_invocations
            )
            try:
                self.invocations.append(invocation)
                if len(self.invocations) == 1:
                    context.command_dispatcher.dispatch_physical(
                        invocation,
                        ManeuverControlDecision(
                            "blocked-navigation",
                            invocation.mission_id,
                            invocation.plan_revision,
                            maneuver_id="blocked-navigation",
                            physical_intent=ManeuverIntent(
                                "navigate",
                                (
                                    ManeuverParameter("x", 1),
                                    ManeuverParameter("y", 0),
                                    ManeuverParameter("z", -250),
                                    ManeuverParameter("speed", 20),
                                ),
                            ),
                        ),
                        sequence=1,
                    )
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("barrier was not released")
                else:
                    status = asyncio.run(context.fsm_runner.status())
                    candidate = status.transition_candidates[0]
                    asyncio.run(
                        context.fsm_runner.apply(
                            candidate,
                            ManeuverDecision(
                                "finish-after-catch-up",
                                invocation.mission_id,
                                transition_event=candidate.event,
                                payload={"plan_revision": invocation.plan_revision},
                            ),
                        )
                    )
                return ManeuverHeartbeatCompletion(
                    invocation.mission_id,
                    invocation.request_id,
                    "Completed the serialized barrier heartbeat.",
                )
            finally:
                active_invocations -= 1

    provider = BarrierProvider()
    maneuver.decision_provider = provider
    coordination = coordinator(lambda *_: None, simulation_limit_seconds=15)
    completed: list[Any] = []

    def run() -> None:
        try:
            completed.append(coordination.run(revision))
        except BaseException as exc:  # noqa: BLE001 - asserted below.
            errors.append(exc)

    thread = Thread(target=run, name="environment-driven-test")
    thread.start()
    assert entered.wait(5), errors
    deadline = time.monotonic() + 2
    while environment.current_time < 15 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert environment.current_time >= 15
    release.set()
    thread.join(3)

    assert not thread.is_alive()
    assert errors == []
    result = completed[0]
    assert result.terminal
    assert maximum_active_invocations == 1
    assert len(provider.invocations) == 2
    catch_up = provider.invocations[1]
    assert catch_up.environment_data["scene_graph"]["mission_time_seconds"] >= 15
    assert len(catch_up.pending_perceptions) == 2
    assert {
        identity
        for identity in catch_up.trigger_identities
        if identity.startswith("environment:")
    } == {
        "environment:maneuver-feedback:maneuver:maneuver-heartbeat:mission-1:0:1:active",
        "environment:maneuver-feedback:maneuver:maneuver-heartbeat:mission-1:0:1:completed",
    }
    periodic = [
        identity
        for identity in catch_up.trigger_identities
        if identity.startswith("periodic:")
    ]
    assert periodic == ["periodic:15"]
    assert result.maximum_update_batch >= 30
    assert result.coalesced_update_count >= 2
    assert result.inference_windows[0].evidence_time_seconds == 0
    assert result.inference_windows[0].completion_time_seconds >= 15
    assert not cast(Any, coordination._environment_source).is_alive
