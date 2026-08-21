from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from langchain.tools import ToolRuntime

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.agents.hyper_workflow import HyperWorkflowContext, handoff_execution
from onr.agents.maneuver_control import DeepAgentsHeartbeatProvider
from onr.agents.maneuver_tools import (
    MANEUVER_OPERATIONAL_TOOLS,
    EntityAssociationInput,
    ManeuverToolContext,
    communicate,
    land,
    navigate,
    transition_fsm,
    update_belief,
)
from onr.application.bayesian_belief import BayesianBeliefManager, BayesianBeliefService
from onr.application.communication import TransportCommunicationPort
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.application.maneuver_control import ManeuverControl
from onr.application.minizinc_translation import MiniZincTranslation
from onr.contracts.bayesian_belief import BeliefKey
from onr.contracts.communication import AgentMessage
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import Statechart, StatechartCondition, StatechartTransition
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.maneuver_control import (
    ManeuverCommand,
    ManeuverHeartbeatOutcome,
    ManeuverInvocation,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanProvenance,
    ScheduledManeuver,
    VerifiableReference,
)
from onr.contracts.transport import TransportEvent
from onr.demo.fake_environment import FakeEnvironment


def _plan() -> NormalizedPlan:
    mission_id = "mission-tools"
    return NormalizedPlan(
        plan_revision=2,
        mission_snapshot_id="mission-tools:snapshot:1",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            ScheduledManeuver(
                "move-anywhere",
                ManeuverIntent(
                    "navigate",
                    (ManeuverParameter("x", 4), ManeuverParameter("y", 8)),
                ),
                (),
                0,
                1,
            ),
        ),
        provenance=PlanProvenance(
            mission_id=mission_id,
            source_authority="operator",
            mission_intent=VerifiableReference("mission", "1" * 64),
            planning_decision=VerifiableReference("choice", "2" * 64),
            environment_data=VerifiableReference("environment", "3" * 64),
            generated_assets={"model": VerifiableReference("model", "4" * 64)},
            solver_evidence={"result": VerifiableReference("result", "5" * 64)},
        ),
    )


def _chart(plan: NormalizedPlan) -> Statechart:
    return Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        normalized_plan_sha256="a" * 64,
        entry_state="arbitrary origin",
        terminal_states=("arbitrary destination",),
        states=("arbitrary origin", "arbitrary destination"),
        state_context={
            "arbitrary origin": {"phase": "waiting"},
            "arbitrary destination": {"phase": "moving", "x": 4, "y": 8},
        },
        transitions=(
            StatechartTransition(
                event="any event text",
                source="arbitrary origin",
                target="arbitrary destination",
                requires_decision=True,
                conditions=(StatechartCondition(10, 1),),
            ),
        ),
    )


class _Dispatcher:
    def dispatch_physical(self, *_: object, **__: object) -> object:
        raise AssertionError("physical dispatch was not expected")


def _runtime(context: ManeuverToolContext) -> ToolRuntime[ManeuverToolContext]:
    return ToolRuntime(
        state={"messages": []},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="tool-call",
        store=None,
    )


def test_operational_tools_have_typed_model_visible_schemas() -> None:
    assert [item.name for item in MANEUVER_OPERATIONAL_TOOLS] == [
        "transition_fsm",
        "navigate",
        "takeoff",
        "land",
        "search_area",
        "pursue",
        "investigate",
        "update_belief",
        "communicate",
    ]
    navigate_schema = cast(
        Any, MANEUVER_OPERATIONAL_TOOLS[1].tool_call_schema
    ).model_json_schema()
    assert set(navigate_schema["properties"]) == {
        "maneuver_id",
        "x",
        "y",
        "z",
        "speed",
        "extra_parameters",
        "reflection",
    }


def test_transition_tool_rejects_early_then_updates_the_live_runner() -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        request_id="heartbeat-1",
        correlation_id="correlation-1",
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        normalized_plan=plan,
        statechart_reference="accepted-statechart.json",
        fsm_status=status,
        environment_data={"mission_time_seconds": 9},
    )
    context = ManeuverToolContext(invocation, runner, _Dispatcher())

    early = json.loads(
        cast(Any, transition_fsm).func(
            event="any event text",
            reflection="The temporal threshold has not arrived.",
            runtime=_runtime(context),
        )
    )

    assert early["status"] == "rejected"
    assert early["current_state"] == "arbitrary origin"
    assert early["candidates"][0]["conditions"] == [
        {
            "kind": "environment_time_at_or_after",
            "time_tick": 10,
            "time_scale": 1,
        }
    ]
    assert asyncio.run(runner.status()).active_state == "arbitrary origin"  # type: ignore[union-attr]

    ready = ManeuverInvocation(
        request_id=invocation.request_id,
        correlation_id=invocation.correlation_id,
        mission_id=invocation.mission_id,
        plan_revision=invocation.plan_revision,
        normalized_plan=plan,
        statechart_reference=invocation.statechart_reference,
        fsm_status=cast(Any, asyncio.run(runner.status())),
        environment_data={"mission_time_seconds": 10},
    )
    ready_context = ManeuverToolContext(ready, runner, _Dispatcher())
    moved = json.loads(
        cast(Any, transition_fsm).func(
            event="any event text",
            reflection="The exact candidate threshold is satisfied.",
            runtime=_runtime(ready_context),
        )
    )
    assert moved["status"] == "transitioned"
    assert moved["fsm_status"]["active_state"] == "arbitrary destination"


def test_fake_environment_activates_ticks_and_overrides(tmp_path: Path) -> None:
    transport = FileTransport(tmp_path)
    environment = FakeEnvironment(transport, "mission-tools")
    first = ManeuverCommand(
        "command-1",
        "correlation",
        "mission-tools",
        2,
        "first",
        ManeuverIntent("navigate", (ManeuverParameter("x", 1), ManeuverParameter("y", 2))),
    )
    second = ManeuverCommand(
        "command-2",
        "correlation",
        "mission-tools",
        2,
        "emergency",
        ManeuverIntent("land", (ManeuverParameter("x", 1), ManeuverParameter("y", 2))),
    )

    environment.submit(first)
    assert environment.navigation_status == "active"
    environment.submit(second)
    assert environment.last_override_feedback is not None
    assert (
        cast(Any, environment.last_override_feedback.payload["payload"])["reason"]
        == "overridden"
    )
    assert environment.current_maneuver["command_id"] == "command-2"  # type: ignore[index]
    environment.tick(mission_time_seconds=12)
    assert environment.navigation_status == "completed"
    graph = environment.current_environment_data()["scene_graph"]
    assert graph["mission_time_seconds"] == 12  # type: ignore[index]


def test_physical_tools_submit_and_override_without_application_gate(
    tmp_path: Path,
) -> None:
    plan = _plan()
    transport = FileTransport(tmp_path / "transport")
    environment = FakeEnvironment(transport, plan.mission_id)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-actions",
        "action-correlation",
        plan.mission_id,
        plan.plan_revision,
        plan,
        "statechart.json",
        status,
        {"mission_time_seconds": 10},
    )
    control = ManeuverControl(
        cast(Any, transport),
        environment,
        object(),
        fsm_runner=runner,
        environment_authority=environment,
    )
    context = ManeuverToolContext(invocation, runner, control)

    first = json.loads(
        cast(Any, navigate).func(
            maneuver_id="not-in-normalized-plan",
            x=100,
            y=200,
            reflection="Current state context calls for movement.",
            runtime=_runtime(context),
        )
    )
    second = json.loads(
        cast(Any, land).func(
            maneuver_id="emergency-landing",
            x=10,
            y=20,
            reflection="Emergency evidence warrants immediate override.",
            runtime=_runtime(context),
        )
    )

    assert first["status"] == second["status"] == "submitted"
    assert environment.navigation_status == "active"
    assert environment.current_maneuver["maneuver_id"] == "emergency-landing"  # type: ignore[index]
    assert environment.last_override_feedback is not None
    assert (
        cast(Any, environment.last_override_feedback.payload["payload"])["reason"]
        == "overridden"
    )


def test_correlated_communication_is_persisted_and_idempotent() -> None:
    port = TransportCommunicationPort(cast(Any, InProcessTransport()))
    seen: list[AgentMessage] = []
    port.register(
        "maneuver-control",
        lambda message: seen.append(message) or {"status": "received"},
    )
    message = AgentMessage(
        "message-1",
        "correlation-1",
        "mission-tools",
        2,
        "hyper-agent",
        "maneuver-control",
        "invoke",
        {"request": "heartbeat"},
    )
    first = port.request(message)
    second = port.request(message)
    assert first == second
    assert len(seen) == 1
    assert first.correlation_id == message.correlation_id


def test_communicate_builds_a_correlated_replan_request() -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-communication",
        "correlation-replan",
        plan.mission_id,
        plan.plan_revision,
        plan,
        "statechart.json",
        status,
        {"mission_time_seconds": 0},
        available_recipients=("hyper-agent",),
    )
    communication = TransportCommunicationPort(cast(Any, InProcessTransport()))
    seen: list[AgentMessage] = []
    communication.register(
        "hyper-agent",
        lambda message: seen.append(message) or {"disposition": "no_change"},
    )
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        communication_port=communication,
    )

    result = json.loads(
        cast(Any, communicate).func(
            recipient="hyper-agent",
            kind="replan",
            message="The verified route is blocked by current evidence.",
            reflection="Current environment evidence warrants Hyper evaluation.",
            runtime=_runtime(context),
        )
    )

    assert result["correlation_id"] == invocation.correlation_id
    assert seen[0].sender == "maneuver-control"
    assert seen[0].mission_id == invocation.mission_id
    assert seen[0].plan_revision == invocation.plan_revision
    assert seen[0].payload["replan_request"]["requester"] == "maneuver-control"  # type: ignore[index]


def test_belief_tool_uses_durable_service_without_mutating_invocation(
    tmp_path: Path,
) -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-belief",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        plan,
        "statechart.json",
        status,
        {"mission_time_seconds": 0},
    )
    manager = BayesianBeliefManager(
        plan.mission_id,
        (BeliefKey("ship-1", "collision"),),
        particle_count=128,
        seed=3,
    )
    service = BayesianBeliefService(
        manager,
        FileBayesianBeliefStore(tmp_path),
        InProcessTransport(),
    )
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        belief_service=service,
    )

    result = json.loads(
        cast(Any, update_belief).func(
            risk_type="collision",
            associations=[EntityAssociationInput(entity_id="ship-1", weight=1.0)],
            likelihood_given_risk=0.9,
            likelihood_given_safe=0.1,
            reflection="The current sensor association supports a risk update.",
            runtime=_runtime(context),
        )
    )

    assert result == {"status": "updated_complete"}
    assert invocation.belief_snapshot is None
    assert service.load_current_snapshot() is not None


def test_completion_consistency_uses_tool_execution_record() -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-no-change",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        plan,
        "statechart.json",
        status,
        {"mission_time_seconds": 0},
    )

    class Agent:
        def invoke(self, *_: object, **__: object) -> dict[str, object]:
            return {
                "structured_response": {
                    "mission_id": plan.mission_id,
                    "request_id": invocation.request_id,
                    "outcome": "no_change",
                    "summary": "Current evidence warrants no effect.",
                }
            }

    context = ManeuverToolContext(invocation, runner, _Dispatcher())
    completion = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)
    assert completion.outcome is ManeuverHeartbeatOutcome.NO_CHANGE


def test_hyper_handoff_activates_runner_before_correlated_invocation(
    tmp_path: Path,
) -> None:
    plan = _plan()
    chart = _chart(plan)
    scene = TransportEvent(
        1,
        "environment-1",
        plan.mission_id,
        0,
        "environment_data",
        {"mission_time_seconds": 0},
    )
    snapshot = MissionSnapshot(
        plan.mission_id,
        1,
        "2026-08-21T00:00:00+00:00",
        environment_data=scene.event_id,
    )

    class Planner:
        def check(self, _: object) -> object:
            raise AssertionError("planner was not expected")

        def execute(self, _: object) -> object:
            raise AssertionError("planner was not expected")

    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    communication = TransportCommunicationPort(cast(Any, InProcessTransport()))
    seen: list[ManeuverInvocation] = []

    def invoke_maneuver(message: AgentMessage) -> dict[str, object]:
        invocation = ManeuverInvocation.from_dict(message.payload)
        seen.append(invocation)
        return {
            "mission_id": invocation.mission_id,
            "request_id": invocation.request_id,
            "outcome": "no_change",
            "summary": "The activated entry state requires no immediate effect.",
        }

    communication.register("hyper-agent", lambda _: {"status": "received"})
    communication.register("maneuver-control", invoke_maneuver)
    context = HyperWorkflowContext(
        mission_input=MissionInput(plan.mission_id, "Proceed.", "operator"),
        mission_snapshot=snapshot,
        environment_event=scene,
        artifact_root=tmp_path,
        minizinc_translation=MiniZincTranslation(
            cast(Any, Planner()), tmp_path / "attempts"
        ),
        state_machine_factory=PythonStateMachineFactory(),
        fsm_runner=runner,
        communication_port=communication,
    )
    context.translation = SimpleNamespace(normalized_plan=plan)
    context.statechart = chart
    context.statechart_reference = str(tmp_path / "accepted-statechart.json")
    context.handoff_correlation_id = "planning-run-1"
    runtime = ToolRuntime(
        state={"messages": []},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="handoff",
        store=None,
    )

    result = json.loads(
        cast(Any, handoff_execution).func(
            reflection="The verified Statechart is ready for live execution.",
            runtime=runtime,
        )
    )

    assert result["status"] == "completed"
    assert context.handoff_outcome is not None
    assert context.initial_fsm_status is not None
    assert seen[0].fsm_status.active_state == chart.entry_state
    assert seen[0].available_recipients == ("hyper-agent",)
