from __future__ import annotations

import asyncio

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.contracts.fsm import (
    ManeuverDecision,
    Statechart,
    StatechartCondition,
    StatechartTransition,
)


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
                conditions=(StatechartCondition(65, 2),),
                requires_decision=True,
            ),
            StatechartTransition(
                event="complete-patrol",
                source="moving-to-patrol-stop-1",
                target="patrol-complete",
                conditions=(StatechartCondition(211, 2),),
                requires_decision=True,
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


def test_runner_exposes_conditions_before_they_are_satisfied_and_requires_decision() -> None:
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
    assert status.transition_candidates[0].conditions == (
        StatechartCondition(65, 2),
    )
    assert status.transition_candidates[0].target_state_context["phase"] == "moving"

    unchanged = asyncio.run(runner.apply(status.transition_candidates[0]))
    assert unchanged.active_state == chart.entry_state

    advanced = asyncio.run(
        runner.apply(
            status.transition_candidates[0],
            maneuver_decision=ManeuverDecision(
                decision_id="decision-1",
                mission_id=chart.mission_id,
                transition_event="start-moving-to-patrol-stop-1",
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
                    conditions=(StatechartCondition(move_start, 2),),
                    requires_decision=True,
                ),
                StatechartTransition(
                    event=f"arrive-at-patrol-stop-{number}",
                    source=moving,
                    target=at_stop,
                    conditions=(StatechartCondition(arrive, 2),),
                    requires_decision=True,
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
            conditions=(StatechartCondition(600, 2),),
            requires_decision=True,
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
