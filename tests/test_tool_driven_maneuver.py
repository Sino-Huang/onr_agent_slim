from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.operational_log import InProcessOperationalLog
from onr.agents.maneuver_control import (
    DeepAgentsHeartbeatProvider,
    ManeuverHeartbeatOrderingError,
    _derived_pursuit_facts,
    _model_visible_invocation,
)
from onr.agents.maneuver_tools import (
    MANEUVER_OPERATIONAL_TOOLS,
    ManeuverHeartbeatExecutionRecord,
    ManeuverToolContext,
    communicate,
    ingest_perceptions,
    investigate,
    land,
    navigate,
    pursue,
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
from onr.contracts.environment import EntityObservation, EventObservation
from onr.contracts.fsm import ManeuverDecision, Statechart, StatechartTransition
from onr.contracts.maneuver_control import (
    ManeuverCommand,
    ManeuverControlDecision,
    ManeuverHeartbeatCompletion,
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
from onr.contracts.transport import TransportEvent
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


def _assess_first_chart(plan: NormalizedPlan) -> Statechart:
    return Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="patrol-awaiting-first-assignment",
        terminal_states=("assignment-1-complete",),
        states=(
            "patrol-awaiting-first-assignment",
            "assignment-1-in-progress",
            "assignment-1-complete",
        ),
        state_context={
            "patrol-awaiting-first-assignment": {"phase": "waiting"},
            "assignment-1-in-progress": {
                "phase": "navigate",
                "maneuver_id": "patrol-action-185",
                "target": {"x": 306, "y": -17},
            },
            "assignment-1-complete": {"phase": "complete"},
        },
        transitions=(
            StatechartTransition(
                event="assignment-1-may-begin",
                source="patrol-awaiting-first-assignment",
                target="assignment-1-in-progress",
                context={"not_before": 0.0},
            ),
            StatechartTransition(
                event="assignment-1-outcome-achieved",
                source="assignment-1-in-progress",
                target="assignment-1-complete",
                context={
                    "not_before": 57.0,
                    "expected_observation_count": 4,
                },
            ),
        ),
    )


def _branching_chart(plan: NormalizedPlan) -> Statechart:
    return Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="origin",
        terminal_states=("destination-a", "destination-b"),
        states=("origin", "destination-a", "destination-b"),
        state_context={state: {} for state in ("origin", "destination-a", "destination-b")},
        transitions=(
            StatechartTransition(
                event="choose-a",
                source="origin",
                target="destination-a",
                context={"path": "a"},
            ),
            StatechartTransition(
                event="choose-b",
                source="origin",
                target="destination-b",
                context={"path": "b"},
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
        "arrival_direction",
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


def test_heartbeat_completion_has_only_code_owned_identity_and_summary() -> None:
    completion = ManeuverHeartbeatCompletion(
        mission_id="mission-tools",
        request_id="heartbeat-contract",
        summary="Completed one decision cycle.",
    )

    assert completion.to_dict() == {
        "mission_id": "mission-tools",
        "request_id": "heartbeat-contract",
        "summary": "Completed one decision cycle.",
    }
    assert ManeuverHeartbeatCompletion.from_json(
        completion.to_canonical_json()
    ) == completion
    assert not hasattr(completion, "outcome")
    with pytest.raises(ValueError, match="invalid fields"):
        ManeuverHeartbeatCompletion.from_dict(
            {**completion.to_dict(), "outcome": "completed"}
        )


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


def test_pursuit_state_context_dispatches_real_pursue_with_numeric_ship_id() -> None:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    chart = Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="pursuing",
        terminal_states=("pursuing",),
        states=("pursuing",),
        state_context={
            "pursuing": {
                "candidate_id": "candidate-7",
                "surveillance_mode": "pursue_ship",
                "target_entity_id": 7,
                "target_report_ids": ["report-7"],
            }
        },
        transitions=(),
    )
    status = asyncio.run(runner.activate(chart))
    invocation = ManeuverInvocation(
        "heartbeat-pursue",
        "correlation-pursue",
        plan.mission_id,
        plan.plan_revision,
        "pursuit-statechart.json",
        _focused(status),
        {"mission_time_seconds": 0},
    )

    class Dispatcher:
        command: ManeuverCommand | None = None

        def dispatch_physical(
            self,
            invocation: ManeuverInvocation,
            decision: ManeuverControlDecision,
            *,
            sequence: int,
        ) -> tuple[ManeuverCommand, bool]:
            self.command = ManeuverCommand(
                f"command-{sequence}",
                invocation.correlation_id,
                invocation.mission_id,
                invocation.plan_revision,
                decision.maneuver_id or "pursuit",
                decision.physical_intent,
            )
            return self.command, True

    dispatcher = Dispatcher()
    context = ManeuverToolContext(invocation, runner, dispatcher)
    result = json.loads(
        cast(Any, pursue).func(
            maneuver_id="pursue-candidate-7",
            entity_id=invocation.fsm_context.current_state_context["target_entity_id"],
            reflection="Maintain visibility through the evidence window.",
            runtime=_runtime(context),
        )
    )

    assert result["action"] == "pursue"
    assert dispatcher.command is not None
    entity_id = dispatcher.command.to_dict()["intent"]["parameters"]["entity_id"]
    assert entity_id == 7
    assert isinstance(entity_id, int)


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


@pytest.mark.parametrize("direction", [-1, 4, 1.0, 1.5, True, "east"])
def test_navigation_tool_schema_rejects_non_discrete_arrival_direction(direction):
    schema = cast(Any, navigate).tool_call_schema
    with pytest.raises(ValueError):
        schema.model_validate({"maneuver_id": "view", "x": 0, "y": 0,
                               "reflection": "Face the selected view", "arrival_direction": direction})


@pytest.mark.parametrize("direction", range(4))
def test_navigation_tool_dispatches_selected_arrival_direction(direction):
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    chart = Statechart(mission_id=plan.mission_id, plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id, planning_profile="temporal",
        entry_state="view", states=("view",), terminal_states=("view",), transitions=(),
        state_context={"view": {"surveillance_mode": "fixed_view",
            "planner_item": {"parameters": {"x": 0, "y": 0, "arrival_direction": direction}}}})
    status = asyncio.run(runner.activate(chart))
    invocation = ManeuverInvocation("turn-heartbeat", "turn-correlation", plan.mission_id,
        plan.plan_revision, "statechart.json", _focused(status), {"mission_time_seconds": 0})

    class Dispatcher:
        command = None

        def dispatch_physical(self, invocation, decision, *, sequence):
            self.command = ManeuverCommand(f"command-{sequence}", invocation.correlation_id,
                invocation.mission_id, invocation.plan_revision, decision.maneuver_id,
                decision.physical_intent)
            return self.command, True

    dispatcher = Dispatcher()
    context = ManeuverToolContext(invocation, runner, dispatcher)
    parameters = invocation.fsm_context.current_state_context["planner_item"]["parameters"]
    result = json.loads(cast(Any, navigate).func(maneuver_id="face-view", x=parameters["x"], y=parameters["y"],
        arrival_direction=parameters["arrival_direction"], reflection="Preserve the selected discrete viewing direction",
        runtime=_runtime(context)))
    assert result["action"] == "navigate"
    assert dispatcher.command.to_dict()["intent"]["parameters"]["arrival_direction"] == direction


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
        environment_data={"mission_time_seconds": 10},
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


def test_transition_tool_rejects_future_exact_time_bound() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Select the exact live target.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        request_id="future-bound-heartbeat",
        correlation_id="future-bound-correlation",
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        statechart_reference="accepted-statechart.json",
        fsm_context=journal.focused_context(status, intent),
        environment_data={"mission_time_seconds": 9},
    )
    context = ManeuverToolContext(
        invocation, runner, _Dispatcher(), transition_intents=journal
    )

    result = json.loads(
        cast(Any, transition_fsm).func(
            current_state=status.active_state,
            next_state="arbitrary destination",
            assessment="satisfied_with_uncertainty",
            evidence="The time bound is still future.",
            uncertainty="Mission time has not reached the bound.",
            runtime=_runtime(context),
        )
    )

    assert result == {
        "status": "rejected",
        "reason": "the selected Transition Intent time bound is still future",
        "mission_time_seconds": 9.0,
        "not_before_seconds": 10.0,
        "current_state": status.active_state,
        "candidates": [
            {
                "target_state": item.target,
                "condition": item.to_dict()["transition_context"],
            }
            for item in status.transition_candidates
        ],
    }


def test_transition_tool_requires_exact_unconfirmed_report_ids() -> None:
    plan = _plan()
    chart = _chart(plan)
    chart = replace(
        chart,
        transitions=(
            replace(
                chart.transitions[0],
                context={
                    "readiness": {
                        "not_before": {"seconds": 10},
                        "sensed_evidence": {
                            "report_check_ledger": "world_model_info.event_report_checks",
                            "report_ids": ["confirmed-report", "missing-report"],
                        },
                    }
                },
            ),
        ),
    )
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(chart))
    intent = journal.select(
        status, "arbitrary destination", "Assess exact evidence.", selected_at=0
    )
    invocation = ManeuverInvocation(
        request_id="missing-evidence-heartbeat",
        correlation_id="missing-evidence-correlation",
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        statechart_reference="accepted-statechart.json",
        fsm_context=journal.focused_context(status, intent),
        environment_data={
            "mission_time_seconds": 10,
            "world_model_info": {
                "event_report_checks": [
                    {"report_id": "confirmed-report", "outcome": "clean"}
                ]
            },
        },
    )
    context = ManeuverToolContext(
        invocation, runner, _Dispatcher(), transition_intents=journal
    )

    result = json.loads(
        cast(Any, transition_fsm).func(
            current_state="arbitrary origin",
            next_state="arbitrary destination",
            assessment="satisfied_with_uncertainty",
            evidence="One required report is confirmed.",
            uncertainty="One required report remains unconfirmed.",
            runtime=_runtime(context),
        )
    )

    assert result["status"] == "rejected"
    assert result["reason"] == (
        "uncertainty must name every unconfirmed required report ID"
    )
    assert result["omitted_report_ids"] == ["missing-report"]
    assert asyncio.run(runner.status()).active_state == "arbitrary origin"


def test_assess_first_heartbeat_persists_new_state_intent_for_fresh_evidence(
    tmp_path: Path,
) -> None:
    plan = _plan()
    transport = FileTransport(tmp_path / "transport")
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_assess_first_chart(plan)))
    environment = FakeEnvironment(transport, plan.mission_id)
    operational_log = InProcessOperationalLog()

    class Agent:
        def __init__(self) -> None:
            self.invocation_count = 0

        def invoke(
            self,
            state: dict[str, object],
            *,
            context: ManeuverToolContext,
            **_: object,
        ) -> dict[str, object]:
            self.invocation_count += 1
            now = context.invocation.environment_data["mission_time_seconds"]
            if now == 0:
                runtime = _runtime(context)
                cast(Any, set_transition_target).func(
                    target_state="assignment-1-in-progress",
                    rationale="Bootstrap the first current-state target.",
                    runtime=runtime,
                )
                cast(Any, transition_fsm).func(
                    current_state="patrol-awaiting-first-assignment",
                    next_state="assignment-1-in-progress",
                    assessment="satisfied",
                    evidence="The initial assignment is available at Mission time zero.",
                    uncertainty="None.",
                    runtime=runtime,
                )
                cast(Any, set_transition_target).func(
                    target_state="assignment-1-complete",
                    rationale="Track the outcome condition for the active assignment.",
                    runtime=runtime,
                )
                cast(Any, navigate).func(
                    maneuver_id="patrol-action-185",
                    x=306,
                    y=-17,
                    speed=1,
                    deadline_time=16.5,
                    reflection="Navigate using the newly active state's outcome facts.",
                    runtime=runtime,
                )
            else:
                assert now == 5
                intent = context.invocation.fsm_context.transition_intent
                assert intent is not None
                assert intent.target_state == "assignment-1-complete"
                assert intent.condition["not_before"] == 57.0
            return {
                **state,
                "structured_response": {
                    "summary": "Applied one assess-first decision cycle.",
                },
            }

    agent = Agent()
    control = ManeuverControl(
        cast(Any, transport),
        DeepAgentsHeartbeatProvider(agent),
        fsm_runner=runner,
        transition_intents=journal,
        operational_log=operational_log,
    )

    first = ManeuverInvocation(
        request_id="heartbeat-assess-first-0",
        correlation_id="correlation-assess-first",
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        statechart_reference="assess-first-statechart.json",
        fsm_context=journal.focused_context(status, None),
        environment_data={"mission_time_seconds": 0},
    )
    first_completion = control.heartbeat(first)
    assert isinstance(first_completion, ManeuverHeartbeatCompletion)
    first_record = control.last_execution_record
    assert isinstance(first_record, ManeuverHeartbeatExecutionRecord)
    assert first_record.initial_intent_id is None
    assert first_record.successful_transition_count == 1
    assert [item.name for item in first_record.executions] == [
        "set_transition_target",
        "transition_fsm",
        "set_transition_target",
        "navigate",
    ]
    assert environment.run_once() is not None
    assert environment.current_maneuver["maneuver_id"] == "patrol-action-185"  # type: ignore[index]

    for _ in range(10):
        environment.tick()
    live = asyncio.run(runner.status())
    assert live is not None
    persisted = journal.current(live)
    assert persisted is not None
    second = ManeuverInvocation(
        request_id="heartbeat-assess-first-5",
        correlation_id="correlation-assess-first",
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        statechart_reference="assess-first-statechart.json",
        fsm_context=journal.focused_context(live, persisted),
        environment_data={"mission_time_seconds": 5},
    )

    second_completion = control.heartbeat(second)
    assert isinstance(second_completion, ManeuverHeartbeatCompletion)
    second_record = control.last_execution_record
    assert isinstance(second_record, ManeuverHeartbeatExecutionRecord)
    assert second_record.initial_intent_id == persisted.intent_id
    assert second_record.successful_transition_count == 0
    assert second_record.executions == []
    assert agent.invocation_count == 2
    assert environment.current_maneuver["command_id"] == "maneuver:heartbeat-assess-first-0:4"  # type: ignore[index]
    heartbeat_records = [
        item
        for item in operational_log.replay(plan.mission_id)
        if item.event_kind == "heartbeat"
    ]
    assert [item.outcome for item in heartbeat_records] == ["completed", "completed"]
    assert [
        item.details["successful_transition_count"] for item in heartbeat_records
    ] == [1, 0]


def test_second_successful_transition_is_rejected_with_new_intent_retained() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_assess_first_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-one-snapshot",
        "correlation-one-snapshot",
        plan.mission_id,
        plan.plan_revision,
        "assess-first-statechart.json",
        journal.focused_context(status, None),
        {"mission_time_seconds": 0},
    )
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    runtime = _runtime(context)

    cast(Any, set_transition_target).func(
        target_state="assignment-1-in-progress",
        rationale="Bootstrap the initial target.",
        runtime=runtime,
    )
    first = json.loads(
        cast(Any, transition_fsm).func(
            current_state="patrol-awaiting-first-assignment",
            next_state="assignment-1-in-progress",
            assessment="satisfied",
            evidence="The initial condition is satisfied.",
            uncertainty="None.",
            runtime=runtime,
        )
    )
    selected = json.loads(
        cast(Any, set_transition_target).func(
            target_state="assignment-1-complete",
            rationale="Select the new state's target for later assessment.",
            runtime=runtime,
        )
    )
    second = json.loads(
        cast(Any, transition_fsm).func(
            current_state="assignment-1-in-progress",
            next_state="assignment-1-complete",
            assessment="satisfied",
            evidence="Reused evidence from the same injected snapshot.",
            uncertainty="None.",
            runtime=runtime,
        )
    )

    assert first["status"] == "transitioned"
    assert second["status"] == "rejected"
    assert "one successful FSM transition" in second["reason"]
    assert context.execution_record.successful_transition_count == 1
    live = asyncio.run(runner.status())
    assert live is not None and live.active_state == "assignment-1-in-progress"
    retained = journal.current(live)
    assert retained is not None
    assert retained.intent_id == selected["transition_intent"]["intent_id"]


def test_retargeted_injected_intent_cannot_transition_until_fresh_heartbeat() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_branching_chart(plan)))
    original = journal.select(
        status,
        "destination-a",
        "Initially prefer route A.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        "heartbeat-retarget",
        "correlation-retarget",
        plan.mission_id,
        plan.plan_revision,
        "branching-statechart.json",
        journal.focused_context(status, original),
        {"mission_time_seconds": 5},
    )
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    runtime = _runtime(context)

    replacement_result = json.loads(
        cast(Any, set_transition_target).func(
            target_state="destination-b",
            rationale="Fresh evidence makes route A unsuitable.",
            runtime=runtime,
        )
    )
    premature = json.loads(
        cast(Any, transition_fsm).func(
            current_state="origin",
            next_state="destination-b",
            assessment="satisfied",
            evidence="Evidence used to reject route A.",
            uncertainty="None.",
            runtime=runtime,
        )
    )

    replacement = journal.latest(plan.mission_id)
    assert replacement is not None
    assert replacement.intent_id == replacement_result["transition_intent"]["intent_id"]
    assert premature["status"] == "rejected"
    assert "fresh Maneuver heartbeat" in premature["reason"]
    assert context.execution_record.successful_transition_count == 0
    live = asyncio.run(runner.status())
    assert live is not None and live.active_state == "origin"

    fresh_invocation = ManeuverInvocation(
        "heartbeat-retarget-fresh",
        "correlation-retarget",
        plan.mission_id,
        plan.plan_revision,
        "branching-statechart.json",
        journal.focused_context(live, replacement),
        {"mission_time_seconds": 10},
    )
    fresh_context = ManeuverToolContext(
        fresh_invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    transitioned = json.loads(
        cast(Any, transition_fsm).func(
            current_state="origin",
            next_state="destination-b",
            assessment="satisfied",
            evidence="Fresh heartbeat evidence satisfies route B.",
            uncertainty="None.",
            runtime=_runtime(fresh_context),
        )
    )
    assert transitioned["status"] == "transitioned"
    assert fresh_context.execution_record.initial_intent_id == replacement.intent_id
    assert fresh_context.execution_record.successful_transition_count == 1


def test_physical_action_is_rejected_when_live_candidates_have_no_intent() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_branching_chart(plan)))
    invocation = ManeuverInvocation(
        "heartbeat-physical-without-intent",
        "correlation-physical-without-intent",
        plan.mission_id,
        plan.plan_revision,
        "branching-statechart.json",
        journal.focused_context(status, None),
        {"mission_time_seconds": 0},
    )
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )

    result = json.loads(
        cast(Any, navigate).func(
            maneuver_id="premature-navigation",
            x=1,
            y=2,
            reflection="This action must wait for target selection.",
            runtime=_runtime(context),
        )
    )

    assert result["status"] == "rejected"
    assert "valid Transition Intent" in result["reason"]
    assert [item.name for item in context.execution_record.executions] == ["navigate"]
    assert context.execution_record.executions[0].successful is False


def test_missing_post_transition_intent_resumes_same_episode_once() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_assess_first_chart(plan)))
    initial = journal.select(
        status,
        "assignment-1-in-progress",
        "Select the injected target before the heartbeat.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        "heartbeat-correct-selection",
        "correlation-correct-selection",
        plan.mission_id,
        plan.plan_revision,
        "assess-first-statechart.json",
        journal.focused_context(status, initial),
        {"mission_time_seconds": 5},
    )
    todo_state = [{"content": "one heartbeat-local cycle", "status": "in_progress"}]

    class Agent:
        def __init__(self) -> None:
            self.states: list[dict[str, object]] = []

        def invoke(
            self,
            state: dict[str, object],
            *,
            context: ManeuverToolContext,
            **_: object,
        ) -> dict[str, object]:
            self.states.append(state)
            runtime = _runtime(context)
            if len(self.states) == 1:
                transitioned = json.loads(
                    cast(Any, transition_fsm).func(
                        current_state="patrol-awaiting-first-assignment",
                        next_state="assignment-1-in-progress",
                        assessment="satisfied",
                        evidence="The injected intent condition is satisfied.",
                        uncertainty="None.",
                        runtime=runtime,
                    )
                )
                assert transitioned["status"] == "transitioned"
                return {
                    "messages": [
                        *cast(list[object], state["messages"]),
                        HumanMessage(content="preserved-first-response"),
                    ],
                    "todos": todo_state,
                    "structured_response": {"summary": "Premature completion."},
                }

            assert state["todos"] is todo_state
            assert "structured_response" not in state
            messages = cast(list[HumanMessage], state["messages"])
            assert messages[-2].content == "preserved-first-response"
            correction = json.loads(cast(str, messages[-1].content))
            assert correction["fsm_context"]["current_state"] == (
                "assignment-1-in-progress"
            )
            assert correction["fsm_context"]["transition_intent"] is None
            assert correction["prohibition"] == (
                "Do not call transition_fsm again in this heartbeat."
            )
            cast(Any, set_transition_target).func(
                target_state="assignment-1-complete",
                rationale="Select the new state's target without assessing it.",
                runtime=runtime,
            )
            return {
                **state,
                "structured_response": {"summary": "Selected the deferred target."},
            }

    agent = Agent()
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    completion = DeepAgentsHeartbeatProvider(agent).heartbeat(invocation, context)

    assert completion.summary == "Selected the deferred target."
    assert len(agent.states) == 2
    assert context.execution_record.successful_transition_count == 1
    assert [item.name for item in context.execution_record.executions] == [
        "transition_fsm",
        "set_transition_target",
    ]
    live = asyncio.run(runner.status())
    assert live is not None and live.active_state == "assignment-1-in-progress"
    assert journal.current(live) is not None
    assert invocation.environment_data["mission_time_seconds"] == 5


def test_terminal_transition_completion_requires_no_new_intent() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Select the terminal target.",
        selected_at=10,
    )
    invocation = ManeuverInvocation(
        "heartbeat-terminal",
        "correlation-terminal",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 10},
    )

    class Agent:
        calls = 0

        def invoke(
            self,
            state: dict[str, object],
            *,
            context: ManeuverToolContext,
            **_: object,
        ) -> dict[str, object]:
            self.calls += 1
            result = json.loads(
                cast(Any, transition_fsm).func(
                    current_state="arbitrary origin",
                    next_state="arbitrary destination",
                    assessment="satisfied",
                    evidence="The terminal condition is satisfied.",
                    uncertainty="None.",
                    runtime=_runtime(context),
                )
            )
            assert result["status"] == "transitioned"
            return {
                **state,
                "structured_response": {"summary": "Reached the terminal state."},
            }

    agent = Agent()
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    completion = DeepAgentsHeartbeatProvider(agent).heartbeat(invocation, context)

    assert completion.summary == "Reached the terminal state."
    assert agent.calls == 1
    live = asyncio.run(runner.status())
    assert live is not None and live.transition_candidates == ()
    assert journal.current(live) is None


def test_failed_post_transition_selection_correction_raises_ordering_error() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_assess_first_chart(plan)))
    intent = journal.select(
        status,
        "assignment-1-in-progress",
        "Select the injected target.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        "heartbeat-failed-correction",
        "correlation-failed-correction",
        plan.mission_id,
        plan.plan_revision,
        "assess-first-statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 0},
    )

    class Agent:
        calls = 0

        def invoke(
            self,
            state: dict[str, object],
            *,
            context: ManeuverToolContext,
            **_: object,
        ) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                cast(Any, transition_fsm).func(
                    current_state="patrol-awaiting-first-assignment",
                    next_state="assignment-1-in-progress",
                    assessment="satisfied",
                    evidence="The injected target is satisfied.",
                    uncertainty="None.",
                    runtime=_runtime(context),
                )
            return {
                **state,
                "structured_response": {"summary": "Still missing a selection."},
            }

    agent = Agent()
    operational_log = InProcessOperationalLog()
    control = ManeuverControl(
        cast(Any, transport),
        DeepAgentsHeartbeatProvider(agent),
        operational_log=operational_log,
        fsm_runner=runner,
        transition_intents=journal,
    )

    with pytest.raises(
        ManeuverHeartbeatOrderingError,
        match="correction did not select",
    ):
        control.heartbeat(invocation)

    assert agent.calls == 2
    record = control.last_execution_record
    assert isinstance(record, ManeuverHeartbeatExecutionRecord)
    assert record.successful_transition_count == 1
    heartbeat_record = operational_log.replay(plan.mission_id)[-1]
    assert heartbeat_record.event_kind == "heartbeat"
    assert heartbeat_record.outcome == "failed"
    assert heartbeat_record.details["error_type"] == (
        "ManeuverHeartbeatOrderingError"
    )


def test_provider_failure_emits_durable_failed_heartbeat_record() -> None:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    operational_log = InProcessOperationalLog()
    invocation = ManeuverInvocation(
        "heartbeat-provider-failure",
        "correlation-provider-failure",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {"mission_time_seconds": 0},
    )

    class Provider:
        def heartbeat(self, *_: object) -> ManeuverHeartbeatCompletion:
            raise RuntimeError("provider unavailable")

    control = ManeuverControl(
        cast(Any, transport),
        Provider(),
        operational_log=operational_log,
        fsm_runner=runner,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        control.heartbeat(invocation)

    records = operational_log.replay(plan.mission_id)
    assert len(records) == 1
    assert records[0].source == "maneuver-control"
    assert records[0].event_kind == "heartbeat"
    assert records[0].outcome == "failed"
    assert records[0].details["operation"] == "maneuver_heartbeat"
    assert records[0].details["request_id"] == invocation.request_id
    assert records[0].details["error_type"] == "RuntimeError"
    assert isinstance(control.last_execution_record, ManeuverHeartbeatExecutionRecord)


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


def test_fake_environment_survives_concurrent_context_sequence_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FileTransport(tmp_path / "transport")
    environment = FakeEnvironment(transport, "mission-tools")
    command = ManeuverCommand(
        "command-concurrent-publication",
        "correlation",
        "mission-tools",
        2,
        "concurrent-publication-navigation",
        ManeuverIntent(
            "navigate",
            (ManeuverParameter("x", 1), ManeuverParameter("y", 1)),
        ),
    )
    original_publish = transport.publish_event
    inserted = False
    competing_sequence: int | None = None

    def publish_with_competitor(topic: str, event: object) -> object:
        nonlocal competing_sequence, inserted
        if (
            not inserted
            and topic == environment.context_topic
            and isinstance(event, TransportEvent)
            and event.payload.get("source") == "environment_data"
        ):
            inserted = True
            competing_sequence = event.sequence
            original_publish(
                topic,
                TransportEvent(
                    schema_version=1,
                    event_id="belief.updated:mission-tools:concurrent",
                    mission_id="mission-tools",
                    sequence=event.sequence,
                    event_kind="belief.updated",
                    payload={"belief_revision": 99},
                ),
            )
        return original_publish(topic, cast(Any, event))

    monkeypatch.setattr(transport, "publish_event", publish_with_competitor)

    result = environment.submit(command)

    assert inserted is True
    assert result.command == command
    latest = transport.latest_event(
        environment.context_topic,
        environment.mission_id,
        event_kind="source-fact",
    )
    assert latest is not None
    assert latest.payload["source"] == "environment_data"
    assert competing_sequence is not None
    assert latest.sequence == competing_sequence + 1


def test_physical_tools_submit_and_override_without_application_gate(
    tmp_path: Path,
) -> None:
    plan = _plan()
    transport = FileTransport(tmp_path / "transport")
    environment = FakeEnvironment(transport, plan.mission_id)
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Select the target before choosing a physical action.",
        selected_at=10,
    )
    invocation = ManeuverInvocation(
        "heartbeat-actions",
        "action-correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 10},
    )
    control = ManeuverControl(
        cast(Any, transport),
        object(),
        fsm_runner=runner,
        transition_intents=journal,
    )
    context = ManeuverToolContext(
        invocation, runner, control, transition_intents=journal
    )

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
    assert environment.run_once() is not None
    assert environment.current_maneuver["parameters"] == {  # type: ignore[index]
        "deadline_time": 8.5,
        "x": 100,
        "y": 200,
    }
    copied_context = ManeuverToolContext(
        invocation, runner, control, transition_intents=journal
    )
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
    assert retained["status"] == "already_queued"
    assert environment.current_maneuver["parameters"] == {  # type: ignore[index]
        "deadline_time": 8.5,
        "x": 100,
        "y": 200,
    }
    rejected = json.loads(
        cast(Any, navigate).func(
            maneuver_id="retired-window",
            x=1,
            y=2,
            extra_parameters={"observation_start": 1},
            reflection="Retired sensing parameters must not pass through extras.",
            runtime=_runtime(context),
        )
    )
    assert rejected["status"] == "rejected"
    assert "retired observation parameters" in rejected["reason"]
    second = json.loads(
        cast(Any, land).func(
            maneuver_id="emergency-landing",
            x=10,
            y=20,
            reflection="Emergency evidence warrants immediate override.",
            runtime=_runtime(context),
        )
    )
    assert environment.run_once() is not None

    assert first["status"] == second["status"] == "queued"
    assert environment.navigation_status == "active"
    assert environment.current_maneuver["maneuver_id"] == "emergency-landing"  # type: ignore[index]
    assert environment.last_override_feedback is not None
    assert (
        cast(Any, environment.last_override_feedback.payload["payload"])["reason"]
        == "overridden"
    )


def test_concurrent_copied_contexts_submit_same_action_once() -> None:
    plan = _plan()
    entered = Event()

    class SlowTransport(InProcessTransport):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def send_command(self, command):  # type: ignore[no-untyped-def]
            self.calls += 1
            entered.set()
            time.sleep(0.1)
            return super().send_command(command)

    transport = SlowTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Select the shared action's transition target.",
        selected_at=10,
    )
    invocation = ManeuverInvocation(
        "heartbeat-concurrent-action",
        "action-correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 10},
    )

    control = ManeuverControl(cast(Any, transport), object())
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def invoke(speed: float) -> None:
        context = ManeuverToolContext(
            invocation, runner, control, transition_intents=journal
        )
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
    assert entered.wait(1)
    second_thread.start()
    first_thread.join(2)
    second_thread.join(2)

    assert errors == []
    assert transport.calls == 1
    assert {result["status"] for result in results} == {
        "queued",
        "already_queued",
    }


def test_parallel_physical_calls_share_one_heartbeat_status_gate() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    base_runner = FSMRunner(
        cast(Any, transport), store=InMemoryFSMStateStore()
    )
    status = asyncio.run(base_runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Select the shared parallel action target.",
        selected_at=10,
    )
    invocation = ManeuverInvocation(
        "heartbeat-parallel-action",
        "parallel-action-correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 10},
    )

    class ContendedRunner:
        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.entered = Event()

        async def status(self) -> object:
            async with self.lock:
                self.entered.set()
                await asyncio.sleep(0.05)
                return status

        async def apply(self, *_: object) -> object:
            raise AssertionError("FSM transition was not expected")

    runner = ContendedRunner()
    control = ManeuverControl(cast(Any, transport), object())
    context = ManeuverToolContext(
        invocation,
        runner,
        control,
        transition_intents=journal,
    )
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(
                json.loads(
                    cast(Any, navigate).func(
                        maneuver_id="parallel-action",
                        x=100,
                        y=200,
                        reflection="Submit one deduplicated parallel action.",
                        runtime=_runtime(context),
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assert thread failures.
            errors.append(exc)

    first = Thread(target=invoke, daemon=True)
    second = Thread(target=invoke, daemon=True)
    first.start()
    assert runner.entered.wait(1)
    second.start()
    first.join(1)
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    queued = transport.state.commands[("maneuver-adapter", plan.mission_id)]
    assert len(queued) == 1
    assert {result["status"] for result in results} == {
        "queued",
        "already_queued",
    }


@pytest.mark.parametrize("physical_tool", [pursue, investigate])
def test_numeric_physical_entity_ids_dispatch_unchanged(physical_tool: object) -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Select the entity action target.",
        selected_at=10,
    )
    invocation = ManeuverInvocation(
        "heartbeat-numeric-entity",
        "numeric-entity-correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {
            "schema_version": 2,
            "mission_time_seconds": 10,
            "controlled_vehicle": {"position": {"x": 0, "y": 0, "z": 0}},
            "maneuver_lifecycle": None,
            "world_model_info": {"visible_ship_ids": [7]},
        },
    )
    control = ManeuverControl(cast(Any, transport), object())
    context = ManeuverToolContext(
        invocation,
        runner,
        control,
        transition_intents=journal,
    )

    result = json.loads(
        cast(Any, physical_tool).func(
            maneuver_id="numeric-entity-action",
            entity_id=7,
            reflection="Use the exact physical vessel identity.",
            runtime=_runtime(context),
        )
    )

    assert result["status"] == "queued"
    queued = transport.state.commands[("maneuver-adapter", plan.mission_id)][0][1]
    command = ManeuverCommand.from_command(queued, "maneuver")
    assert command.intent.to_dict()["parameters"]["entity_id"] == 7


def test_parallel_target_selection_and_physical_action_share_fsm_gate() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    base_runner = FSMRunner(
        cast(Any, transport), store=InMemoryFSMStateStore()
    )
    status = asyncio.run(base_runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Select the target before the parallel tool turn.",
        selected_at=10,
    )
    invocation = ManeuverInvocation(
        "heartbeat-parallel-selection-action",
        "parallel-selection-action-correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 10},
    )

    class ContendedRunner:
        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.entered = Event()

        async def status(self) -> object:
            async with self.lock:
                self.entered.set()
                await asyncio.sleep(0.05)
                return status

        async def apply(self, *_: object) -> object:
            raise AssertionError("FSM transition was not expected")

    runner = ContendedRunner()
    control = ManeuverControl(cast(Any, transport), object())
    context = ManeuverToolContext(
        invocation,
        runner,
        control,
        transition_intents=journal,
    )
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def select() -> None:
        try:
            results.append(
                json.loads(
                    cast(Any, set_transition_target).func(
                        target_state="arbitrary destination",
                        rationale="Retain the injected target.",
                        runtime=_runtime(context),
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assert thread failures.
            errors.append(exc)

    def act() -> None:
        try:
            results.append(
                json.loads(
                    cast(Any, navigate).func(
                        maneuver_id="parallel-selection-action",
                        x=100,
                        y=200,
                        reflection="Act only after target selection is visible.",
                        runtime=_runtime(context),
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assert thread failures.
            errors.append(exc)

    selection = Thread(target=select, daemon=True)
    physical = Thread(target=act, daemon=True)
    selection.start()
    assert runner.entered.wait(1)
    physical.start()
    selection.join(1)
    physical.join(1)

    assert not selection.is_alive()
    assert not physical.is_alive()
    assert errors == []
    assert {result["status"] for result in results} == {
        "retained",
        "queued",
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


def test_completed_stable_communication_returns_outcome_before_payload_comparison() -> None:
    transport = InProcessTransport()
    port = TransportCommunicationPort(cast(Any, transport))
    seen: list[AgentMessage] = []
    port.register(
        "hyper-agent",
        lambda message: seen.append(message) or {"status": "queued"},
    )
    first = AgentMessage(
        "stable-evaluation",
        "correlation-1",
        "mission-tools",
        2,
        "maneuver-control",
        "hyper-agent",
        "replan",
        {"message": "Canonical reason.", "source_revisions": {"environment": 1}},
    )
    later_snapshot = AgentMessage(
        "stable-evaluation",
        "correlation-1",
        "mission-tools",
        2,
        "maneuver-control",
        "hyper-agent",
        "replan",
        {"message": "Canonical reason.", "source_revisions": {"environment": 2}},
    )

    original = port.request(first)
    repeated = port.request(later_snapshot)

    assert repeated == original
    assert len(seen) == 1


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
        cast(Any, fsm_transport), object()
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


@pytest.mark.parametrize("entity_count", [0, 2])
def test_belief_tool_rejects_non_event_batches_without_mutation(
    tmp_path: Path, entity_count: int
) -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    perceptions = tuple(
        EntityObservation(f"sighting:{index}", "ship-1", (1, 2, 0), index, 0.0)
        for index in range(entity_count)
    )
    invocation = ManeuverInvocation(
        "heartbeat-entity-only",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {"mission_time_seconds": 20},
        pending_perceptions=perceptions,
    )
    service = BayesianBeliefService(
        BayesianBeliefManager(
            plan.mission_id,
            (BeliefKey("ship-1", "event-risk"),),
            particle_count=128,
            seed=3,
        ),
        FileBayesianBeliefStore(tmp_path),
        InProcessTransport(),
    )
    context = ManeuverToolContext(
        invocation, runner, _Dispatcher(), belief_service=service
    )
    before = service.load_current_snapshot()
    result = json.loads(
        cast(Any, ingest_perceptions).func(
            reflection="Process the current sightings.", runtime=_runtime(context)
        )
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "no_pending_event_observations"
    assert context.perception_batch_ingested is False
    assert context.execution_record.executions[-1].successful is False
    assert invocation.pending_perceptions == perceptions
    assert service.load_current_snapshot() == before
    assert (
        service.transport.next_event_sequence(
            service.observation_topic, plan.mission_id
        )
        == 0
    )


@pytest.mark.parametrize("include_entity", [False, True])
def test_belief_tool_ingests_each_pending_event_once(
    tmp_path: Path, include_entity: bool
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
        pending_perceptions=(
            (EntityObservation("sighting", "ship-1", (0, 0, 0), 0, 0.0),)
            + perceptions
            if include_entity else perceptions
        ),
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


def test_model_visible_invocation_bounds_entities_and_retains_events() -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    old = EntityObservation("old", "ship-1", (0, 0, 0), 1, 0.1)
    latest = EntityObservation("latest", "ship-1", (1, 2, 0), 2, 0.2)
    other = EntityObservation("other", "ship-2", (3, 4, 0), 1.5, 0.3)
    event = EventObservation(
        observation_id="event-observed",
        entity_id="ship-1",
        position=(1, 2, 0),
        observed_time=2,
        uncertainty_score=0.1,
        source_event_index=1,
        event_type="report",
        event_information={"decision": "left"},
        event_time=2,
    )
    invocation = ManeuverInvocation(
        "bounded-entity-heartbeat",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {"mission_time_seconds": 2},
        pending_perceptions=(old, event, other, latest),
    )

    visible = _model_visible_invocation(invocation)

    assert visible["pending_perceptions"] == [event.to_dict()]
    assert visible["pending_entity_perceptions"] == {
        "observation_count": 3,
        "latest_by_entity": [latest.to_dict(), other.to_dict()],
    }
    assert invocation.pending_perceptions == (old, event, other, latest)


def test_model_visible_invocation_projects_world_history_to_current_references() -> None:
    plan = _plan()
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    event = EventObservation(
        observation_id="event-observed",
        entity_id="ship-1",
        position=(1, 2, 0),
        observed_time=2,
        uncertainty_score=0.1,
        source_event_index=1,
        event_type="report",
        event_information={"report_id": "report-current"},
        event_time=2,
    )
    current_report = {"report_id": "report-current", "entity_id": "ship-1"}
    other_report = {"report_id": "report-other", "entity_id": "ship-2"}
    invocation = ManeuverInvocation(
        "bounded-world-heartbeat",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {
            "mission_time_seconds": 2,
            "maneuver_lifecycle": {"parameters": {"entity_id": "ship-1"}},
            "world_model_info": {
                "visible_ship_ids": ["ship-1", "ship-2"],
                "event_report_checks": [current_report, other_report],
                "ship_event_reports": {
                    "ship-1": [current_report],
                    "ship-2": [other_report],
                },
                "public_position_fixes": [current_report, other_report],
                "detected_issues": {
                    "ship-1": [{"issue_id": "current"}],
                    "ship-2": [{"issue_id": "other"}],
                },
            },
        },
        pending_perceptions=(event,),
    )

    visible = _model_visible_invocation(invocation)
    info = cast(dict[str, object], visible["environment_data"])["world_model_info"]

    assert isinstance(info, dict)
    assert info["visible_ship_ids"] == ["ship-1", "ship-2"]
    assert info["event_report_checks"] == [current_report]
    assert info["ship_event_reports"] == {"ship-1": [current_report]}
    assert info["public_position_fixes"] == [current_report]
    assert info["detected_issues"] == {"ship-1": [{"issue_id": "current"}]}
    assert visible["pending_event_perception_count"] == 1
    assert info["history_projection"] == {
        "scope": "current_fsm_and_pending_events",
        "event_report_checks": {"available": 2, "shown": 1},
        "ship_event_reports": {"available": 2, "shown": 1},
        "public_position_fixes": {"available": 2, "shown": 1},
        "detected_issue_entities": {"available": 2, "shown": 1},
    }
    original_info = invocation.to_dict()["environment_data"]["world_model_info"]
    assert original_info["ship_event_reports"] == {
        "ship-1": [current_report],
        "ship-2": [other_report],
    }


def test_derived_pursuit_facts_expose_passed_bound_and_stale_fix() -> None:
    plan = _plan()
    chart = _chart(plan)
    chart = replace(
        chart,
        state_context={
            **chart.state_context,
            chart.entry_state: {
                "surveillance_mode": "pursue_ship",
                "target_entity_id": 23,
                "observation_window": {
                    "start": {"seconds": 52},
                    "duration": {"seconds": 30},
                },
            },
        },
    )
    runner = FSMRunner(cast(Any, InProcessTransport()), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(chart))
    invocation = ManeuverInvocation(
        "pursuit-facts-heartbeat",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        _focused(status),
        {
            "mission_time_seconds": 55,
            "maneuver_lifecycle": {
                "action": "pursue",
                "lifecycle": "active",
                "phase": "search",
                "start_time": 46,
                "parameters": {"entity_id": 23},
            },
            "world_model_info": {
                "visible_ship_ids": [],
                "gps_interval_seconds": 30,
                "next_gps_update_time_s": 60,
                "public_position_fixes": [
                    {"entity_id": 23, "sampled_at_s": 30, "source": "gps"}
                ],
            },
        },
    )

    assert _derived_pursuit_facts(invocation) == {
        "target_entity_id": 23,
        "matching_active_pursuit": True,
        "active_acquisition_navigation": False,
        "target_visible": False,
        "pursuit_phase": "search",
        "mission_time_seconds": 55.0,
        "attempt_start_seconds": 46.0,
        "first_required_observation_seconds": 52.0,
        "first_gps_after_attempt_seconds": 60.0,
        "acquisition_bound_seconds": 52.0,
        "seconds_since_acquisition_bound": 3.0,
        "newest_target_fix": {
            "entity_id": 23,
            "sampled_at_s": 30,
            "source": "gps",
        },
        "newest_fix_after_attempt": False,
    }


def test_reporting_events_are_pre_ingested_once_before_model_assessment() -> None:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    journal = TransitionIntentJournal(transport)
    intent = journal.select(
        status, "arbitrary destination", "Retain pending intent.", selected_at=0
    )
    event = EventObservation(
        observation_id="event-pre-ingested",
        entity_id="ship-1",
        position=(1, 2, 0),
        observed_time=2,
        uncertainty_score=0.1,
        source_event_index=1,
        event_type="report",
        event_information={"decision": "left"},
        event_time=2,
    )
    invocation = ManeuverInvocation(
        "pre-ingestion-heartbeat",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 2},
        pending_perceptions=(event,),
    )

    class ReportingService:
        belief_kind = "reporting_reliability"

        def handle(self, _: object) -> None:
            return None

    class Agent:
        def invoke(self, state: dict[str, Any], **_: object) -> dict[str, object]:
            payload = json.loads(state["messages"][0].content)
            assert payload["pending_perceptions"] == []
            assert payload["pending_event_perception_count"] == 0
            assert payload["pre_ingested_event_perceptions"]["status"] == (
                "ignored_actual_event_observations"
            )
            assert payload["pre_ingested_event_perceptions"]["event_count"] == 1
            return {"structured_response": {"summary": "Event batch recorded."}}

    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        belief_service=ReportingService(),
        transition_intents=journal,
    )

    DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert context.perception_batch_ingested is True
    assert [item.name for item in context.execution_record.executions] == [
        "ingest_perceptions"
    ]
    assert context.execution_record.executions[0].successful is True
    assert invocation.pending_perceptions == (event,)


@pytest.mark.parametrize(
    ("phase", "visible", "mission_time", "remaining", "status_text"),
    (
        ("pursuit", False, 55, 27, "tracking phase"),
        ("search", True, 55, 27, "currently visible"),
        ("search", False, 50, 32, "within its acquisition search bound"),
    ),
)
def test_future_visible_pursuit_fast_path_ingests_and_skips_model(
    phase: str,
    visible: bool,
    mission_time: int,
    remaining: int,
    status_text: str,
) -> None:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    chart = Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile=plan.planner_choice.planning_profile,
        entry_state="observing",
        states=("observing", "done"),
        terminal_states=("done",),
        state_context={
            "observing": {
                "surveillance_mode": "pursue_ship",
                "target_entity_id": 23,
                "observation_window": {
                    "start": {"seconds": 52},
                    "duration": {"seconds": 30},
                },
            },
            "done": {},
        },
        transitions=(
            StatechartTransition(
                "finish",
                "observing",
                "done",
                {"readiness": {"not_before": {"seconds": 82}}},
            ),
        ),
    )
    status = asyncio.run(runner.activate(chart))
    journal = TransitionIntentJournal(transport)
    intent = journal.select(status, "done", "Wait for time gate.", selected_at=0)
    event = EventObservation(
        observation_id="event-fast-path",
        entity_id=23,
        position=(1, 2, 0),
        observed_time=mission_time,
        uncertainty_score=0.1,
        source_event_index=1,
        event_type="report",
        event_information={"decision": "left"},
        event_time=mission_time,
    )
    invocation = ManeuverInvocation(
        "fast-path-heartbeat",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {
            "mission_time_seconds": mission_time,
            "maneuver_lifecycle": {
                "action": "pursue",
                "lifecycle": "active",
                "phase": phase,
                "start_time": 46,
                "parameters": {"entity_id": 23},
            },
            "world_model_info": {"visible_ship_ids": [23] if visible else []},
        },
        pending_perceptions=(event,),
    )

    class ReportingService:
        belief_kind = "reporting_reliability"

        def handle(self, _: object) -> None:
            return None

    class Agent:
        def invoke(self, *_: object, **__: object) -> object:
            raise AssertionError("routine future tracking must not invoke the model")

    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        belief_service=ReportingService(),
        transition_intents=journal,
    )

    result = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert f"{remaining} seconds" in result.summary
    assert status_text in result.summary
    assert "no physical command was submitted" in result.summary
    assert context.perception_batch_ingested is True
    assert [item.name for item in context.execution_record.executions] == [
        "ingest_perceptions"
    ]
    assert asyncio.run(runner.status()).active_state == "observing"


def _bounded_search_invocation(
    *, mission_time: float, fixes: list[dict[str, object]]
) -> tuple[
    ManeuverInvocation,
    FSMRunner,
    TransitionIntentJournal,
    InProcessTransport,
]:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    chart = Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="observing",
        states=("observing", "done"),
        terminal_states=("done",),
        state_context={
            "observing": {
                "surveillance_mode": "pursue_ship",
                "target_entity_id": 23,
                "observation_window": {
                    "start": {"seconds": 52},
                    "duration": {"seconds": 30},
                },
            },
            "done": {},
        },
        transitions=(
            StatechartTransition(
                "finish",
                "observing",
                "done",
                {"readiness": {"not_before": {"seconds": 82}}},
            ),
        ),
    )
    status = asyncio.run(runner.activate(chart))
    journal = TransitionIntentJournal(transport)
    intent = journal.select(status, "done", "Wait for time gate.", selected_at=0)
    invocation = ManeuverInvocation(
        f"bounded-search-{mission_time:g}",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {
            "mission_time_seconds": mission_time,
            "controlled_vehicle": {"position": {"x": 0, "y": 0, "z": -25}},
            "maneuver_lifecycle": {
                "action": "pursue",
                "lifecycle": "active",
                "phase": "search",
                "start_time": 46,
                "parameters": {"entity_id": 23},
            },
            "world_model_info": {
                "visible_ship_ids": [],
                "gps_interval_seconds": 30,
                "next_gps_update_time_s": 90,
                "public_position_fixes": fixes,
            },
        },
        available_recipients=("hyper-agent",),
    )
    return invocation, runner, journal, transport


def test_missed_acquisition_fast_path_notifies_once_without_model() -> None:
    invocation, runner, journal, transport = _bounded_search_invocation(
        mission_time=55,
        fixes=[
            {
                "entity_id": 23,
                "sampled_at_s": 30,
                "source": "gps",
                "position": {"x": 10, "y": 20},
            }
        ],
    )
    communication = TransportCommunicationPort(cast(Any, InProcessTransport()))
    seen: list[AgentMessage] = []
    communication.register(
        "hyper-agent",
        lambda message: seen.append(message) or {"disposition": "no_change"},
    )

    class Agent:
        def invoke(self, *_: object, **__: object) -> object:
            raise AssertionError("exact missed acquisition must not invoke the model")

    provider = DeepAgentsHeartbeatProvider(Agent())
    for _ in range(2):
        context = ManeuverToolContext(
            invocation,
            runner,
            ManeuverControl(cast(Any, transport), object()),
            communication_port=communication,
            transition_intents=journal,
        )
        result = provider.heartbeat(invocation, context)
        assert "missed acquisition" in result.summary

    assert len(seen) == 1
    assert seen[0].kind == "replan"


def test_new_gps_recovery_fast_path_navigates_and_reports_without_model() -> None:
    invocation, runner, journal, transport = _bounded_search_invocation(
        mission_time=61,
        fixes=[
            {
                "entity_id": 23,
                "sampled_at_s": 60,
                "source": "gps",
                "position": {"x": 150, "y": 70},
            }
        ],
    )
    communication = TransportCommunicationPort(cast(Any, InProcessTransport()))
    seen: list[AgentMessage] = []
    communication.register(
        "hyper-agent",
        lambda message: seen.append(message) or {"disposition": "no_change"},
    )

    class Agent:
        def invoke(self, *_: object, **__: object) -> object:
            raise AssertionError("exact GPS recovery must not invoke the model")

    control = ManeuverControl(cast(Any, transport), object())
    context = ManeuverToolContext(
        invocation,
        runner,
        control,
        communication_port=communication,
        transition_intents=journal,
    )
    result = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert "Submitted recovery navigation" in result.summary
    assert [item.name for item in context.execution_record.executions] == [
        "navigate",
        "communicate",
    ]
    queued = transport.state.commands[("maneuver-adapter", invocation.mission_id)][0][1]
    command = ManeuverCommand.from_command(queued, "maneuver")
    assert command.action == "navigate"
    assert command.intent.to_dict()["parameters"] == {"x": 150, "y": 70, "z": -25}
    assert len(seen) == 1
    assert seen[0].kind == "report"


def test_completed_acquisition_navigation_resumes_pursuit_without_model() -> None:
    base, runner, journal, transport = _bounded_search_invocation(
        mission_time=61,
        fixes=[],
    )
    environment = dict(base.environment_data)
    environment["maneuver_lifecycle"] = {
        "action": "navigate",
        "lifecycle": "completed",
        "parameters": {"x": 150, "y": 70, "z": -25},
    }
    invocation = replace(base, environment_data=environment)

    class Agent:
        def invoke(self, *_: object, **__: object) -> object:
            raise AssertionError("navigation arrival must not invoke the model")

    control = ManeuverControl(cast(Any, transport), object())
    context = ManeuverToolContext(
        invocation,
        runner,
        control,
        transition_intents=journal,
    )
    result = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert "Submitted pursuit of target 23" in result.summary
    assert [item.name for item in context.execution_record.executions] == ["pursue"]
    queued = transport.state.commands[("maneuver-adapter", base.mission_id)][0][1]
    command = ManeuverCommand.from_command(queued, "maneuver")
    assert command.action == "pursue"
    assert command.intent.to_dict()["parameters"] == {"entity_id": 23}


def test_future_fixed_view_report_wake_skips_model() -> None:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    chart = Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="observing",
        states=("observing", "done"),
        terminal_states=("done",),
        state_context={
            "observing": {
                "surveillance_mode": "fixed_view",
                "desired_outcome": {"location": {"x": 100, "y": 20}},
            },
            "done": {},
        },
        transitions=(
            StatechartTransition(
                "finish",
                "observing",
                "done",
                {"readiness": {"not_before": {"seconds": 82}}},
            ),
        ),
    )
    status = asyncio.run(runner.activate(chart))
    journal = TransitionIntentJournal(transport)
    intent = journal.select(status, "done", "Wait for time gate.", selected_at=0)
    invocation = ManeuverInvocation(
        "fixed-view-report-wake",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {
            "mission_time_seconds": 75,
            "maneuver_lifecycle": {
                "action": "navigate",
                "lifecycle": "completed",
                "parameters": {"x": 100, "y": 20, "z": -25},
            },
            "world_model_info": {"event_report_checks": []},
        },
    )

    class Agent:
        def invoke(self, *_: object, **__: object) -> object:
            raise AssertionError("future fixed-view gate must not invoke the model")

    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    result = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert "7 seconds in the future" in result.summary
    assert context.execution_record.executions == []


def test_time_ready_mission1_entry_fast_path_transitions_and_dispatches() -> None:
    plan = _plan()
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    chart = Statechart(
        mission_id=plan.mission_id,
        plan_revision=plan.plan_revision,
        mission_snapshot_id=plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="waiting",
        states=("waiting", "assignment", "done"),
        terminal_states=("done",),
        state_context={
            "waiting": {"desired_outcome": "begin"},
            "assignment": {
                "candidate_id": "fixed-1",
                "surveillance_mode": "fixed_view",
                "desired_outcome": {
                    "location": {"x": 100, "y": 20},
                    "arrival_deadline": {"seconds": 8},
                },
                "planner_item": {"parameters": {"arrival_direction": 2}},
            },
            "done": {},
        },
        transitions=(
            StatechartTransition(
                "begin",
                "waiting",
                "assignment",
                {"readiness": {"mission_time_at_or_after": {"seconds": 0}}},
            ),
            StatechartTransition(
                "finish",
                "assignment",
                "done",
                {"readiness": {"not_before": {"seconds": 10}}},
            ),
        ),
    )
    status = asyncio.run(runner.activate(chart))
    journal = TransitionIntentJournal(transport)
    invocation = ManeuverInvocation(
        "ready-entry",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, None),
        {
            "mission_time_seconds": 0,
            "controlled_vehicle": {"position": {"x": 0, "y": 0, "z": -25}},
            "world_model_info": {"visible_ship_ids": []},
        },
    )

    class Agent:
        def invoke(self, *_: object, **__: object) -> object:
            raise AssertionError("exact Mission 1 entry must not invoke the model")

    control = ManeuverControl(cast(Any, transport), object())
    context = ManeuverToolContext(
        invocation,
        runner,
        control,
        transition_intents=journal,
    )
    result = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    live = asyncio.run(runner.status())
    assert live.active_state == "assignment"
    assert journal.current(live).target_state == "done"
    assert "submitted fixed-view navigation" in result.summary
    assert [item.name for item in context.execution_record.executions] == [
        "set_transition_target",
        "transition_fsm",
        "set_transition_target",
        "navigate",
    ]
    queued = transport.state.commands[("maneuver-adapter", plan.mission_id)][0][1]
    command = ManeuverCommand.from_command(queued, "maneuver")
    assert command.intent.to_dict()["parameters"] == {
        "arrival_direction": 2,
        "deadline_time": 8.0,
        "x": 100,
        "y": 20,
        "z": -25,
    }


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


def test_todo_only_heartbeat_retains_intent_and_returns_typed_completion() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Retain this unsatisfied intent.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        "heartbeat-no-change",
        "correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 0},
    )

    class Agent:
        todo_calls = 1

        def invoke(self, state: dict[str, Any], **_: object) -> dict[str, object]:
            assert len(state["messages"]) == 1  # Unknown condition schema is unchanged.
            return {
                "structured_response": {
                    "summary": "Current evidence warrants no effect.",
                }
            }

    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    completion = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)
    assert completion.mission_id == invocation.mission_id
    assert completion.request_id == invocation.request_id
    assert context.execution_record.executions == []


@pytest.mark.parametrize("now", [10.0, 10.5, 11.0])
@pytest.mark.parametrize(
    ("ledger_ids", "matched_ids", "missing_ids"),
    [
        ((), [], ["required-a", "required-b"]),
        (("required-a", "required-a"), ["required-a"], ["required-b"]),
        (("required-a", "required-b"), ["required-a", "required-b"], []),
    ],
)
def test_heartbeat_adds_exact_read_only_transition_facts(
    now: float,
    ledger_ids: tuple[str, ...],
    matched_ids: list[str],
    missing_ids: list[str],
) -> None:
    plan = _plan()
    chart = _chart(plan)
    chart = replace(
        chart,
        state_context={
            chart.entry_state: {
                "observation_window": {
                    "start": {"seconds": 10.0},
                    "duration": {"seconds": 0.5},
                }
            },
            "arbitrary destination": {},
        },
        transitions=(
            replace(
                chart.transitions[0],
                context={
                    "readiness": {
                        "not_before": {"seconds": 10.5},
                        "sensed_evidence": {
                            "report_check_ledger": "world_model_info.event_report_checks",
                            "report_ids": ["required-a", "required-b"],
                        },
                    }
                },
            ),
        ),
    )
    transport = InProcessTransport()
    runner = FSMRunner(transport, store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(chart))
    journal = TransitionIntentJournal(transport)
    intent = journal.select(
        status, "arbitrary destination", "Assess this intent.", selected_at=0
    )
    invocation = ManeuverInvocation(
        "fact-heartbeat",
        "fact-correlation",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {
            "mission_time_seconds": now,
            "world_model_info": {
                "event_report_checks": [
                    {"report_id": "unrelated", "outcome": "clean"},
                    {"report_id": None, "outcome": "omitted"},
                    *[
                        {"report_id": report_id, "outcome": "altered"}
                        for report_id in ledger_ids
                    ],
                ]
            },
        },
    )
    original = invocation.to_dict()
    model_visible = _model_visible_invocation(invocation)

    class Agent:
        def invoke(self, state: dict[str, Any], **_: object) -> dict[str, object]:
            messages = state["messages"]
            assert len(messages) == 2
            assert json.loads(messages[0].content) == model_visible
            facts = json.loads(messages[1].content)["derived_transition_facts"]
            assert facts["intent_id"] == intent.intent_id
            assert facts["source_state"] == chart.entry_state
            assert facts["target_state"] == "arbitrary destination"
            assert facts["mission_time_seconds"] == now
            assert facts["seconds_until_not_before"] == 10.5 - now
            assert facts["seconds_until_window_end"] == 10.5 - now
            assert facts["report_check_comparison"] == {
                "required_count": 2,
                "matched_count": len(matched_ids),
                "matched_report_ids": matched_ids,
                "matched_outcomes": {report_id: "altered" for report_id in matched_ids},
                "unconfirmed_count": len(missing_ids),
                "unconfirmed_report_ids": missing_ids,
            }
            assert "assessment" not in facts
            return {
                "structured_response": {
                    "summary": "Facts received; assessment remains agent-owned."
                }
            }

    context = ManeuverToolContext(
        invocation, runner, _Dispatcher(), transition_intents=journal
    )
    DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)
    assert invocation.to_dict() == original
    assert ManeuverInvocation.from_dict(original) == invocation
    assert context.execution_record.executions == []
    assert asyncio.run(runner.status()).active_state == chart.entry_state


def test_prose_only_heartbeat_resumes_same_episode_for_structured_summary() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Retain this unsatisfied intent.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        "heartbeat-prose-summary",
        "correlation-prose-summary",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 0},
    )
    todo_state = [{"content": "one heartbeat-local cycle", "status": "completed"}]

    class Agent:
        def __init__(self) -> None:
            self.states: list[dict[str, object]] = []

        def invoke(self, state: dict[str, object], **_: object) -> dict[str, object]:
            self.states.append(state)
            if len(self.states) == 1:
                return {
                    "messages": [
                        *cast(list[object], state["messages"]),
                        AIMessage(content="Prose-only heartbeat completion."),
                    ],
                    "todos": todo_state,
                }

            assert state["todos"] is todo_state
            messages = cast(list[HumanMessage], state["messages"])
            assert messages[-2].content == "Prose-only heartbeat completion."
            correction = json.loads(cast(str, messages[-1].content))
            assert "ManeuverHeartbeatResponse" in correction["completion_correction"]
            assert correction["prohibition"] == (
                "Do not call other tools or repeat mission effects, skill reads, "
                "or todo updates. Preserve the successful work already recorded."
            )
            assert correction["errors"] == [
                {
                    "code": "malformed_structured_output",
                    "expected": "valid structured output",
                    "path": "$",
                }
            ]
            return {
                **state,
                "structured_response": {"summary": "Retained the current intent."},
            }

    agent = Agent()
    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    completion = DeepAgentsHeartbeatProvider(agent).heartbeat(invocation, context)

    assert completion.summary == "Retained the current intent."
    assert len(agent.states) == 2
    assert context.execution_record.executions == []


def test_rejected_tool_heartbeat_returns_normal_typed_completion() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_branching_chart(plan)))
    intent = journal.select(
        status,
        "destination-a",
        "Retain the valid injected intent.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        "heartbeat-rejected-tool",
        "correlation-rejected-tool",
        plan.mission_id,
        plan.plan_revision,
        "branching-statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 0},
    )

    class Agent:
        def invoke(
            self,
            state: dict[str, object],
            *,
            context: ManeuverToolContext,
            **_: object,
        ) -> dict[str, object]:
            rejected = json.loads(
                cast(Any, set_transition_target).func(
                    target_state="unknown-destination",
                    rationale="Exercise a rejected tool call.",
                    runtime=_runtime(context),
                )
            )
            assert rejected["status"] == "rejected"
            return {
                **state,
                "structured_response": {"summary": "Retained the valid intent."},
            }

    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        transition_intents=journal,
    )
    completion = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert completion.summary == "Retained the valid intent."
    assert len(context.execution_record.executions) == 1
    assert context.execution_record.executions[0].successful is False


def test_rejected_stale_evaluation_communication_can_complete_heartbeat() -> None:
    plan = _plan()
    transport = InProcessTransport()
    journal = TransitionIntentJournal(transport)
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    status = asyncio.run(runner.activate(_chart(plan)))
    intent = journal.select(
        status,
        "arbitrary destination",
        "Retain the valid target while rejecting stale communication.",
        selected_at=0,
    )
    invocation = ManeuverInvocation(
        "heartbeat-rejected-communication",
        "correlation-rejected-communication",
        plan.mission_id,
        plan.plan_revision,
        "statechart.json",
        journal.focused_context(status, intent),
        {"mission_time_seconds": 0},
        available_recipients=("hyper-agent",),
    )
    communication = TransportCommunicationPort(cast(Any, InProcessTransport()))
    communication.register(
        "hyper-agent", lambda _: {"disposition": "no_change"}
    )

    class Agent:
        def invoke(
            self,
            state: dict[str, object],
            *,
            context: ManeuverToolContext,
            **_: object,
        ) -> dict[str, object]:
            rejected = json.loads(
                cast(Any, communicate).func(
                    recipient="hyper-agent",
                    kind="replan",
                    message="This evaluation belongs to an earlier state.",
                    reflection="Reject stale state-entry communication.",
                    evaluation_id="stale-evaluation",
                    delivery_policy="once_per_state_entry",
                    runtime=_runtime(context),
                )
            )
            assert rejected["status"] == "rejected"
            return {
                **state,
                "structured_response": {
                    "summary": "Retained the current intent after rejection."
                },
            }

    context = ManeuverToolContext(
        invocation,
        runner,
        _Dispatcher(),
        communication_port=communication,
        transition_intents=journal,
    )
    completion = DeepAgentsHeartbeatProvider(Agent()).heartbeat(invocation, context)

    assert completion.summary == "Retained the current intent after rejection."
    assert len(context.execution_record.executions) == 1
    assert context.execution_record.executions[0].name == "communicate"
    assert context.execution_record.executions[0].successful is False


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

    assert completion.mission_id == invocation.mission_id
    assert completion.request_id == invocation.request_id
    assert context.execution_record.executions == []
