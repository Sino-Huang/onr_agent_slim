from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

import pytest
from langchain.tools import ToolRuntime

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.agents.maneuver_control import DeepAgentsHeartbeatProvider
from onr.agents.maneuver_tools import (
    MANEUVER_OPERATIONAL_TOOLS,
    ManeuverToolContext,
    communicate,
    ingest_perceptions,
    investigate,
    land,
    navigate,
    search_area,
    set_transition_target,
    transition_fsm,
)
from onr.application.bayesian_belief import BayesianBeliefManager, BayesianBeliefService
from onr.application.communication import TransportCommunicationPort
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.application.maneuver_control import ManeuverControl
from onr.application.transition_intents import TransitionIntentJournal
from onr.contracts.bayesian_belief import BeliefKey
from onr.contracts.communication import AgentMessage
from onr.contracts.environment import EventObservation
from onr.contracts.fsm import ManeuverDecision, Statechart, StatechartTransition
from onr.contracts.maneuver_control import (
    ManeuverCommand,
    ManeuverControlDecision,
    ManeuverHeartbeatOutcome,
    ManeuverInvocation,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    ScheduledManeuver,
)
from onr.contracts.transition_intent import ManeuverFSMContext
from onr.demo.fake_environment import FakeEnvironment


def _plan() -> NormalizedPlan:
    mission_id = "mission-tools"
    return NormalizedPlan(
        mission_id=mission_id,
        source_authority="operator",
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
    )


def _chart(plan: NormalizedPlan) -> Statechart:
    return Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
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
                context={
                    "readiness": {"mission_clock": {"minimum": 10, "unit": "seconds"}}
                },
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


def _focused(status: object) -> ManeuverFSMContext:
    return TransitionIntentJournal(InProcessTransport()).focused_context(
        cast(Any, status), None
    )


def test_operational_tools_have_typed_model_visible_schemas() -> None:
    assert [item.name for item in MANEUVER_OPERATIONAL_TOOLS] == [
        "set_transition_target",
        "transition_fsm",
        "navigate",
        "takeoff",
        "land",
        "search_area",
        "pursue",
        "investigate",
        "ingest_perceptions",
        "communicate",
    ]
    transition_schema = cast(
        Any, MANEUVER_OPERATIONAL_TOOLS[1].tool_call_schema
    ).model_json_schema()
    assert set(transition_schema["properties"]) == {
        "current_state",
        "next_state",
        "assessment",
        "evidence",
        "uncertainty",
    }
    assert "event" not in transition_schema["properties"]
    navigate_schema = cast(
        Any, MANEUVER_OPERATIONAL_TOOLS[2].tool_call_schema
    ).model_json_schema()
    assert set(navigate_schema["properties"]) == {
        "maneuver_id",
        "x",
        "y",
        "z",
        "speed",
        "deadline_time",
        "extra_parameters",
        "reflection",
    }
    search_schema = cast(
        Any, MANEUVER_OPERATIONAL_TOOLS[5].tool_call_schema
    ).model_json_schema()
    assert "polygon" in search_schema["required"]
    assert set(search_schema["properties"]) == {
        "maneuver_id",
        "polygon",
        "reflection",
        "altitude",
        "speed",
        "deadline_time",
        "extra_parameters",
    }
    investigate_schema = cast(
        Any, MANEUVER_OPERATIONAL_TOOLS[7].tool_call_schema
    ).model_json_schema()
    assert "deadline_time" in investigate_schema["properties"]
    for tool_index in (3, 4, 6):
        schema = cast(
            Any, MANEUVER_OPERATIONAL_TOOLS[tool_index].tool_call_schema
        ).model_json_schema()
        assert "deadline_time" not in schema["properties"]


def test_nested_polygon_round_trips_through_decisions_and_commands() -> None:
    polygon = [
        {"x": 0.0, "y": 0.0},
        {"x": 4.0, "y": 0.0},
        {"x": 0.0, "y": 3.0},
    ]
    intent = ManeuverIntent("search_area", (ManeuverParameter("polygon", polygon),))
    decision = ManeuverControlDecision(
        "decision-polygon",
        "mission-tools",
        2,
        maneuver_id="search-polygon",
        physical_intent=intent,
    )
    command = ManeuverCommand(
        "command-polygon",
        "correlation-polygon",
        "mission-tools",
        2,
        "search-polygon",
        intent,
    )

    assert decision.to_dict()["physical_intent"]["parameters"]["polygon"] == polygon  # type: ignore[index]
    assert ManeuverControlDecision.from_json(decision.to_canonical_json()) == decision
    assert command.to_dict()["intent"]["parameters"]["polygon"] == polygon  # type: ignore[index]
    assert ManeuverCommand.from_json(command.to_canonical_json()) == command


@pytest.mark.parametrize(
    "polygon",
    [
        [{"x": 0, "y": 0}, {"x": 1, "y": 1}],
        [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": math.inf, "y": 1}],
        [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 2}],
        [{"x": 0, "y": 0}, {"x": 1, "y": 0}, [2, 1]],
    ],
)
def test_search_area_rejects_malformed_polygons(polygon: object) -> None:
    with pytest.raises(ValueError, match="polygon"):
        cast(Any, search_area).func(
            maneuver_id="search",
            polygon=polygon,
            reflection="Reject malformed geometry.",
            runtime=None,
        )


def test_maneuver_parameters_reject_non_json_values_and_negative_deadlines() -> None:
    with pytest.raises(ValueError, match="JSON-safe"):
        ManeuverParameter("polygon", {"points": {1, 2, 3}})
    with pytest.raises(ValueError, match="non-negative"):
        cast(Any, investigate).func(
            maneuver_id="investigate",
            entity_id="ship-1",
            deadline_time=-1,
            reflection="Reject an invalid absolute deadline.",
            runtime=None,
        )


def test_transition_tool_checks_exact_candidate_without_interpreting_context() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        request_id="heartbeat-1",
        correlation_id="correlation-1",
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        statechart_reference="accepted-statechart.json",
        fsm_context=journal.focused_context(status, None),
        environment_data={"mission_time_seconds": 9},
    )
    context = ManeuverToolContext(
        invocation, runner, _Dispatcher(), transition_intents=journal
    )
    assert ManeuverInvocation.from_dict(invocation.to_dict()) == invocation

    missing = json.loads(
        cast(Any, set_transition_target).func(
            target_state="Arbitrary Destination",
            rationale="This target differs in case from the live candidate.",
            runtime=_runtime(context),
        )
    )

    assert missing["status"] == "rejected"
    assert missing["current_state"] == "arbitrary origin"
    assert missing["candidates"][0] == {
        "target_state": "arbitrary destination",
        "condition": {
            "readiness": {"mission_clock": {"minimum": 10, "unit": "seconds"}}
        },
    }
    assert asyncio.run(runner.status()).active_state == "arbitrary origin"  # type: ignore[union-attr]

    selected = json.loads(
        cast(Any, set_transition_target).func(
            target_state="arbitrary destination",
            rationale="Select the exact live target.",
            runtime=_runtime(context),
        )
    )
    assert selected["transition_intent"]["condition"] == missing["candidates"][0][
        "condition"
    ]
    assert "x" not in invocation.to_dict()["fsm_context"]["current_state_context"]
    assert asyncio.run(runner.status()).active_state == "arbitrary origin"  # type: ignore[union-attr]
    intent_events = transport.next_event_sequence("transition-intents", plan.mission_id)
    retained = json.loads(
        cast(Any, set_transition_target).func(
            target_state="arbitrary destination",
            rationale="A repeated selection is idempotent.",
            runtime=_runtime(context),
        )
    )
    assert retained["status"] == "retained"
    assert retained["transition_intent"]["intent_id"] == selected[
        "transition_intent"
    ]["intent_id"]
    assert (
        transport.next_event_sequence("transition-intents", plan.mission_id)
        == intent_events
    )

    moved = json.loads(
        cast(Any, transition_fsm).func(
            current_state="arbitrary origin",
            next_state="arbitrary destination",
            assessment="satisfied_with_uncertainty",
            evidence="The available live evidence is sufficient.",
            uncertainty="The Mission clock is earlier than the expected time.",
            runtime=_runtime(context),
        )
    )
    assert moved["status"] == "transitioned"
    assert "event" not in moved
    assert context.execution_record.decisions[-1].transition_event == "any event text"
    assert moved["fsm_context"]["current_state"] == "arbitrary destination"
    assert moved["fsm_context"]["current_state_context"] == {
        "phase": "moving",
        "x": 4,
        "y": 8,
    }
    assert journal.latest(plan.mission_id).status == "consumed"  # type: ignore[union-attr]


def test_fake_environment_activates_ticks_and_overrides(tmp_path: Path) -> None:
    transport = FileTransport(tmp_path)
    environment = FakeEnvironment(transport, "mission-tools")
    first = ManeuverCommand(
        "command-1",
        "correlation",
        "mission-tools",
        2,
        "first",
        ManeuverIntent(
            "navigate", (ManeuverParameter("x", 1), ManeuverParameter("y", 2))
        ),
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
    environment.tick()
    assert environment.navigation_status == "completed"
    graph = environment.current_environment_data()["scene_graph"]
    assert graph["mission_time_seconds"] == 0.5  # type: ignore[index]


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
        "statechart.json",
        _focused(status),
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
            deadline_time=8.5,
            reflection="Current state context calls for movement.",
            runtime=_runtime(context),
        )
    )
    assert environment.current_maneuver["parameters"] == {  # type: ignore[index]
        "deadline_time": 8.5,
        "x": 100,
        "y": 200,
    }
    copied_context = ManeuverToolContext(invocation, runner, control)
    retained = json.loads(
        cast(Any, navigate).func(
            maneuver_id="not-in-normalized-plan",
            x=100,
            y=200,
            speed=20,
            deadline_time=8.5,
            reflection="A copied context must retain the submitted action.",
            runtime=_runtime(copied_context),
        )
    )
    assert retained["status"] == "retained_active_action"
    assert environment.current_maneuver["parameters"] == {  # type: ignore[index]
        "deadline_time": 8.5,
        "x": 100,
        "y": 200,
    }
    with pytest.raises(ValueError, match="retired observation parameters"):
        cast(Any, navigate).func(
            maneuver_id="retired-window",
            x=1,
            y=2,
            extra_parameters={"observation_start": 1},
            reflection="Retired sensing parameters must not pass through extras.",
            runtime=_runtime(context),
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


def test_concurrent_copied_contexts_submit_same_action_once() -> None:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-concurrent-action",
        "action-correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {"mission_time_seconds": 10},
    )

    class SlowAdapter:
        def __init__(self) -> None:
            self.calls = 0
            self.entered = Event()

        def submit(self, _: object) -> None:
            self.calls += 1
            self.entered.set()
            time.sleep(0.1)

    adapter = SlowAdapter()
    control = ManeuverControl(cast(Any, transport), cast(Any, adapter), object())
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def invoke(speed: float) -> None:
        context = ManeuverToolContext(invocation, runner, control)
        try:
            results.append(
                json.loads(
                    cast(Any, navigate).func(
                        maneuver_id="shared-action",
                        x=100,
                        y=200,
                        speed=speed,
                        reflection="Submit one shared physical action.",
                        runtime=_runtime(context),
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assert concurrent failures.
            errors.append(exc)

    first_thread = Thread(target=invoke, args=(10,))
    second_thread = Thread(target=invoke, args=(20,))
    first_thread.start()
    assert adapter.entered.wait(1)
    second_thread.start()
    first_thread.join(2)
    second_thread.join(2)

    assert errors == []
    assert adapter.calls == 1
    assert {result["status"] for result in results} == {
        "submitted",
        "retained_active_action",
    }


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


def test_duplicate_communication_while_running_returns_in_flight() -> None:
    port = TransportCommunicationPort(cast(Any, InProcessTransport()))
    started = Event()
    release = Event()
    completed: list[object] = []

    def handler(_: AgentMessage) -> dict[str, str]:
        started.set()
        assert release.wait(2)
        return {"status": "received"}

    port.register("hyper-agent", handler)
    message = AgentMessage(
        "stable-evaluation",
        "correlation-1",
        "mission-tools",
        2,
        "maneuver-control",
        "hyper-agent",
        "replan",
        {"message": "Evaluate."},
    )
    thread = Thread(target=lambda: completed.append(port.request(message)))
    thread.start()
    assert started.wait(2)

    duplicate = port.request(message)
    release.set()
    thread.join(2)

    assert duplicate.status == "already_in_flight"
    assert len(completed) == 1
    assert cast(Any, completed[0]).status == "completed"


def test_once_per_state_entry_hyper_evaluation_has_stable_identity() -> None:
    plan = _plan()
    reason = "Evaluate whether the current evidence changes the plan."
    evaluation = {
        "evaluation_id": "evidence-review",
        "kind": "replan",
        "reason": reason,
        "delivery_policy": "once_per_state_entry",
    }
    base = _chart(plan)

    def evaluation_chart(revision: int) -> Statechart:
        return Statechart(
            mission_id=base.mission_id,
            plan_revision=revision,
            mission_snapshot_id=f"mission-tools:snapshot:{revision}",
            planning_profile=base.planning_profile,
            entry_state=base.entry_state,
            terminal_states=base.terminal_states,
            states=base.states,
            state_context={
                state: {**dict(base.context_for(state)), "hyper_evaluation": evaluation}
                for state in base.states
            },
            transitions=base.transitions,
        )

    fsm_transport = InProcessTransport()
    journal = TransitionIntentJournal(fsm_transport)
    runner = FSMRunner(cast(Any, fsm_transport), store=InMemoryFSMStateStore())
    asyncio.run(runner.activate(evaluation_chart(2)))
    communication = TransportCommunicationPort(cast(Any, InProcessTransport()))
    seen: list[AgentMessage] = []
    communication.register(
        "hyper-agent",
        lambda message: seen.append(message) or {"disposition": "no_change"},
    )
    control = ManeuverControl(
        cast(Any, fsm_transport), cast(Any, object()), object()
    )

    def invoke(request_id: str) -> dict[str, object]:
        status = asyncio.run(runner.status())
        assert status is not None
        focused = journal.focused_context(status, None)
        control.update_live_fsm_context(focused)
        invocation = ManeuverInvocation(
            request_id,
            "correlation-evaluation",
            plan.mission_id,
            status.plan_revision,
            f"statechart-{status.plan_revision}.json",
            focused,
            {"mission_time_seconds": 0},
            available_recipients=("hyper-agent",),
        )
        context = ManeuverToolContext(
            invocation,
            runner,
            control,
            communication_port=communication,
        )
        return json.loads(
            cast(Any, communicate).func(
                recipient="hyper-agent",
                kind="replan",
                message=f"{reason} Additional caller detail from {request_id}.",
                reflection="The declared evaluation is due.",
                evaluation_id="evidence-review",
                delivery_policy="once_per_state_entry",
                runtime=_runtime(context),
            )
        )

    first = invoke("heartbeat-evaluation-1")
    repeated = invoke("heartbeat-evaluation-2")
    live = asyncio.run(runner.status())
    assert live is not None
    candidate = live.transition_candidates[0]
    asyncio.run(
        runner.apply(
            candidate,
            ManeuverDecision(
                "advance-evaluation-entry",
                plan.mission_id,
                transition_event=candidate.event,
                payload={"plan_revision": live.plan_revision},
            ),
        )
    )
    next_entry = invoke("heartbeat-evaluation-3")
    asyncio.run(runner.activate(evaluation_chart(3)))
    next_plan = invoke("heartbeat-evaluation-4")

    assert repeated == first
    assert len(seen) == 3
    assert all(message.payload["message"] == reason for message in seen)
    assert next_entry["command_id"] != first["command_id"]
    assert next_plan["command_id"] != next_entry["command_id"]


def test_communicate_builds_a_correlated_replan_request() -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-communication",
        "correlation-replan",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
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


def test_belief_tool_ingests_each_pending_event_once(
    tmp_path: Path,
) -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    perceptions = tuple(
        EventObservation(
            observation_id=f"event-observed:{index}",
            entity_id="ship-1",
            position=(0, 0, 0),
            observed_time=float(index),
            uncertainty_score=uncertainty,
            source_event_index=index,
            event_type="report",
            event_information={"index": index},
            event_time=float(index),
        )
        for index, uncertainty in ((1, 0.1), (2, 0.2))
    )
    invocation = ManeuverInvocation(
        "heartbeat-belief",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {"mission_time_seconds": 0},
        pending_perceptions=perceptions,
    )
    manager = BayesianBeliefManager(
        plan.mission_id,
        (BeliefKey("ship-1", "event-risk"),),
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
        cast(Any, ingest_perceptions).func(
            reflection="The current sensor association supports a risk update.",
            runtime=_runtime(context),
        )
    )

    assert result["status"] == "updated_complete"
    assert result["event_count"] == 2
    assert result["belief_revisions"] == [1, 2]
    assert service.load_current_snapshot() is not None
    assert (
        service.transport.next_event_sequence(
            service.observation_topic, plan.mission_id
        )
        == 2
    )
    persisted = service.transport.latest_event(
        service.observation_topic, plan.mission_id, event_kind="risk.observed"
    )
    assert persisted is not None
    assert persisted.payload["input_revision"] == 2
    assert persisted.payload["risk_type"] == "event-risk"
    assert tuple(cast(Any, persisted.payload["associations"])) == (
        {"entity_id": "ship-1", "weight": 1.0},
    )
    assert persisted.payload["likelihood_given_risk"] == 0.8
    assert persisted.payload["likelihood_given_safe"] == 0.2

    with pytest.raises(RuntimeError, match="unavailable after success"):
        cast(Any, ingest_perceptions).func(
            reflection="A repeated batch must be unavailable.",
            runtime=_runtime(context),
        )
    current = service.load_current_snapshot()
    assert current is not None and current.belief_revision == 2


def test_failed_perception_ingestion_remains_available_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    perception = EventObservation(
        observation_id="event-observed:retry",
        entity_id="ship-1",
        position=(0, 0, 0),
        observed_time=1,
        uncertainty_score=0.25,
        source_event_index=1,
        event_type="report",
        event_information={"decision": "left"},
        event_time=1,
    )
    invocation = ManeuverInvocation(
        "heartbeat-retry",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {"mission_time_seconds": 1},
        pending_perceptions=(perception,),
    )
    service = BayesianBeliefService(
        BayesianBeliefManager(
            plan.mission_id,
            (BeliefKey("ship-1", "event-risk"),),
            particle_count=128,
            seed=4,
        ),
        FileBayesianBeliefStore(tmp_path / "belief"),
        FileTransport(tmp_path / "transport"),
    )
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        belief_service=service,
    )
    original_handle = service.handle
    monkeypatch.setattr(
        service,
        "handle",
        lambda _event: (_ for _ in ()).throw(RuntimeError("ingestion failed")),
    )

    with pytest.raises(RuntimeError, match="ingestion failed"):
        cast(Any, ingest_perceptions).func(
            reflection="Attempt the pending batch.",
            runtime=_runtime(context),
        )

    assert context.perception_batch_ingested is False
    assert context.execution_record.executions == []
    assert invocation.pending_perceptions == (perception,)

    monkeypatch.setattr(service, "handle", original_handle)
    retried = json.loads(
        cast(Any, ingest_perceptions).func(
            reflection="Retry the retained pending batch.",
            runtime=_runtime(context),
        )
    )
    assert retried["belief_revisions"] == [1]
    persisted = service.transport.latest_event(
        service.observation_topic, plan.mission_id, event_kind="risk.observed"
    )
    assert persisted is not None
    assert persisted.payload["input_revision"] == 1

    with pytest.raises(RuntimeError, match="unavailable after success"):
        cast(Any, ingest_perceptions).func(
            reflection="A repeated batch must be unavailable.",
            runtime=_runtime(context),
        )
    current = service.load_current_snapshot()
    assert current is not None and current.belief_revision == 1
    assert (
        service.transport.next_event_sequence(
            service.observation_topic, plan.mission_id
        )
        == 1
    )


def test_todo_only_heartbeat_can_complete_with_no_change() -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-no-change",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {"mission_time_seconds": 0},
    )

    class Agent:
        todo_calls = 1

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
    assert context.execution_record.executions == []


def test_unchanged_target_and_suitable_active_action_submit_no_command() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Retain the target supported by the active navigation.",
        selected_at=1,
    )
    invocation = ManeuverInvocation(
        "heartbeat-continuity",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {
            "mission_time_seconds": 1,
            "current_maneuver": {
                "status": "active",
                "action": "navigate",
                "target_state": "arbitrary destination",
            },
        },
    )

    class Agent:
        def invoke(self, *_: object, **__: object) -> dict[str, object]:
            return {
                "structured_response": {
                    "mission_id": plan.mission_id,
                    "request_id": invocation.request_id,
                    "outcome": "no_change",
                    "summary": "The active action remains suitable.",
                }
            }

    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    completion = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert completion.outcome is ManeuverHeartbeatOutcome.NO_CHANGE
    assert context.execution_record.executions == []
