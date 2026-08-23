from __future__ import annotations

import asyncio

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.contracts.fsm import (
    ManeuverDecision,
    Statechart,
    StatechartTransition,
)
from onr.contracts.hyper_agent import MissionInput
from onr.demo.maneuver_patrol import create_demo_patrol


def _chart() -> Statechart:
    return Statechart(
        mission_id="mission-semantic",
        plan_revision=1,
        mission_snapshot_id="mission-semantic:snapshot:1",
        planning_profile="temporal",
        entry_state="at-initial-location",
        terminal_states=("patrol-complete",),
        states=(
            "at-initial-location",
            "moving-to-patrol-stop-1",
            "patrol-complete",
        ),
        state_context={
            "at-initial-location": {
                "phase": "stationary",
                "x": 46,
                "y": -86,
            },
            "moving-to-patrol-stop-1": {
                "phase": "moving",
                "target_x": -484,
                "target_y": -1415,
            },
            "patrol-complete": {"phase": "complete"},
        },
        transitions=(
            StatechartTransition(
                event="start-moving-to-patrol-stop-1",
                source="at-initial-location",
                target="moving-to-patrol-stop-1",
                context={"not_before": {"tick": 65, "scale": 2}},
            ),
            StatechartTransition(
                event="complete-patrol",
                source="moving-to-patrol-stop-1",
                target="patrol-complete",
                context={"not_before": {"tick": 211, "scale": 2}},
            ),
        ),
    )


def test_semantic_statechart_round_trips_and_rejects_unreachable_states() -> None:
    chart = _chart()

    assert Statechart.from_json(chart.to_canonical_json()) == chart
    assert chart.context_for("moving-to-patrol-stop-1")["target_x"] == -484

    with pytest.raises(ValueError, match="reachable"):
        Statechart(
            mission_id=chart.mission_id,
            plan_revision=chart.plan_revision,
            mission_snapshot_id=chart.mission_snapshot_id,
            planning_profile=chart.planning_profile,
            entry_state=chart.entry_state,
            terminal_states=chart.terminal_states,
            states=chart.states + ("orphan",),
            state_context={**chart.state_context, "orphan": {}},
            transitions=chart.transitions,
        )


def test_python_statemachine_factory_builds_and_advances_semantic_chart() -> None:
    machine = PythonStateMachineFactory().build(_chart())

    assert machine.current_state == "at-initial-location"
    assert machine.allowed_events == ("start-moving-to-patrol-stop-1",)
    machine.send("start-moving-to-patrol-stop-1")
    assert machine.current_state == "moving-to-patrol-stop-1"
    assert machine.allowed_events == ("complete-patrol",)


def test_runner_exposes_flexible_context_and_requires_decision() -> None:
    chart = _chart()
    runner = FSMRunner(
        InProcessTransport(),
        store=InMemoryFSMStateStore(),
        machine_factory=PythonStateMachineFactory(),
    )

    status = asyncio.run(runner.activate(chart))

    assert status.active_state == chart.entry_state
    assert status.active_state_context["phase"] == "stationary"
    assert status.enabled_events == ("start-moving-to-patrol-stop-1",)
    assert status.transition_candidates[0].transition_context == {
        "not_before": {"tick": 65, "scale": 2}
    }
    assert status.transition_candidates[0].target_state_context["phase"] == "moving"

    advanced = asyncio.run(
        runner.apply(
            status.transition_candidates[0],
            ManeuverDecision(
                decision_id="decision-1",
                mission_id=chart.mission_id,
                transition_event="start-moving-to-patrol-stop-1",
                payload={"plan_revision": chart.plan_revision},
            ),
        )
    )
    assert advanced.active_state == "moving-to-patrol-stop-1"


def test_event_patrol_semantic_topology_instantiates_all_four_stops() -> None:
    stops = (
        (1, 65, 209, 46, -86, -484, -1415, 37),
        (2, 242, 277, -484, -1415, -626, -1725, 63),
        (3, 363, 498, -626, -1725, -1411, -629, 113),
        (4, 562, 598, -1411, -629, -1619, -347, 230),
    )
    states = ["at-initial-location"]
    contexts: dict[str, dict[str, object]] = {
        "at-initial-location": {"phase": "stationary", "x": 46, "y": -86}
    }
    transitions = []
    source = "at-initial-location"
    for number, move_start, arrive, from_x, from_y, x, y, event_index in stops:
        moving = f"moving-to-patrol-stop-{number}"
        at_stop = f"at-patrol-stop-{number}"
        states.extend((moving, at_stop))
        contexts[moving] = {
            "phase": "moving",
            "from_x": from_x,
            "from_y": from_y,
            "target_x": x,
            "target_y": y,
        }
        contexts[at_stop] = {
            "phase": "observing",
            "x": x,
            "y": y,
            "source_event_index": event_index,
        }
        transitions.extend(
            (
                StatechartTransition(
                    event=f"start-moving-to-patrol-stop-{number}",
                    source=source,
                    target=moving,
                    context={"time": {"tick": move_start, "scale": 2}},
                ),
                StatechartTransition(
                    event=f"arrive-at-patrol-stop-{number}",
                    source=moving,
                    target=at_stop,
                    context={"time": {"tick": arrive, "scale": 2}},
                ),
            )
        )
        source = at_stop
    states.append("patrol-complete")
    contexts["patrol-complete"] = {"phase": "complete"}
    transitions.append(
        StatechartTransition(
            event="complete-patrol",
            source=source,
            target="patrol-complete",
            context={"time": {"tick": 600, "scale": 2}},
        )
    )
    chart = Statechart(
        mission_id="mission-patrol",
        plan_revision=1,
        mission_snapshot_id="mission-patrol:snapshot:1",
        planning_profile="temporal",
        entry_state="at-initial-location",
        terminal_states=("patrol-complete",),
        states=tuple(states),
        state_context=contexts,
        transitions=tuple(transitions),
    )

    machine = PythonStateMachineFactory().build(chart)
    for transition in chart.transitions:
        assert machine.allowed_events == (transition.event,)
        machine.send(transition.event)

    assert machine.current_state == "patrol-complete"
    assert machine.allowed_events == ()


def test_demo_patrol_exposes_absolute_navigation_deadlines() -> None:
    artifacts = create_demo_patrol(
        MissionInput("mission-demo-deadlines", "Patrol four stops.", "test")
    )

    assert [
        artifacts.statechart.state_context[f"moving-to-patrol-stop-{number}"][
            "deadline_time"
        ]
        for number in range(1, 5)
    ] == [5, 10, 15, 20]
