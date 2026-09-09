"""Local real-model heartbeat and bounded-loop checks; no external service."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.application.communication import TransportCommunicationPort
from onr.application.context_coordination import ActivePlanRevision
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.contracts.fsm import Statechart, StatechartTransition
from onr.contracts.hyper_agent import HyperHeartbeatDecision
from onr.contracts.maneuver_control import ManeuverInvocation
from onr.runtime.composition import RuntimeComposition
from onr.runtime.config import load_runtime_config

pytestmark = pytest.mark.live
ROOT = Path(__file__).parents[1]


def test_live_waiting_fixed_view_preserves_action_and_intent(tmp_path: Path) -> None:
    config = load_runtime_config(ROOT / "conf/onr_agent_params.yaml", repo_root=ROOT)
    config = replace(
        config,
        debug=True,
        storage=replace(
            config.storage,
            root=tmp_path / "storage",
            planner_artifacts=tmp_path / "planner",
        ),
    )
    transport = InProcessTransport()
    runtime = RuntimeComposition(config, transport)
    mission = "local-waiting-view"
    runner = FSMRunner(transport, store=InMemoryFSMStateStore())
    location = {"x": 180, "y": -20, "z": -25}
    chart = Statechart(
        mission_id=mission,
        plan_revision=1,
        mission_snapshot_id=f"{mission}:snapshot:1",
        planning_profile="temporal",
        entry_state="observing",
        terminal_states=("done",),
        states=("observing", "done"),
        state_context={
            "observing": {
                "surveillance_mode": "fixed_view",
                "observation_window": {
                    "start": {"seconds": 100},
                    "duration": {"seconds": 20},
                },
                "desired_outcome": {
                    "kind": "observe_fixed_location",
                    "location": location,
                },
            },
            "done": {"desired_outcome": "observation interval complete"},
        },
        transitions=(
            StatechartTransition(
                "confirm",
                "observing",
                "done",
                {"readiness": {"not_before": {"seconds": 120}}},
            ),
        ),
    )
    status = asyncio.run(runner.activate(chart))
    communications = []
    communication = TransportCommunicationPort(transport)
    communication.register(
        "hyper-agent",
        lambda message: communications.append(message) or {"status": "received"},
    )
    model = runtime.create_chat_model(
        mission_id=mission, debug_scope="maneuver-control"
    )
    model.seed = 0
    control = runtime.create_maneuver_control(
        model=model,
        mission_id=mission,
        fsm_runner=runner,
        communication_port=communication,
        system_prompt=load_system_prompt(
            ROOT / "conf/system_prompt", "maneuver-control"
        ),
        skill_catalog=FilesystemRoleSkillCatalog(ROOT / "conf/skills"),
        backend_root=ROOT,
    )
    intent = control.transition_intents.select(
        status,
        "done",
        "Wait until the current evidence interval ends.",
        selected_at=100,
    )
    invocation = ManeuverInvocation(
        request_id="waiting-view-heartbeat",
        correlation_id=mission,
        mission_id=mission,
        plan_revision=1,
        statechart_reference="accepted-statechart.json",
        fsm_context=control.transition_intents.focused_context(status, intent),
        environment_data={
            "mission_time_seconds": 105,
            "controlled_vehicle": {
                "position": location,
                "max_velocity": 30,
                "fov_radius": 100,
                "flight_state": "flying",
                "speed_mps": 0,
            },
            "maneuver_lifecycle": {
                "action": "navigate",
                "maneuver_id": "established-view",
                "lifecycle": "completed",
                "phase": "navigate",
                "start_time": 90,
                "plan_revision": 1,
                "parameters": {**location, "deadline_time": 100},
                "progress": {"remaining_distance_m": 0},
            },
            "world_model_info": {
                "visible_ship_ids": [],
                "visible_ships": [],
                "event_report_checks": [],
            },
        },
        available_recipients=("hyper-agent",),
    )
    started = time.monotonic()
    completion = control.heartbeat(invocation)
    elapsed = time.monotonic() - started
    records = [
        json.loads(p.read_text()) for p in (tmp_path / "debug/llm").rglob("*.json")
    ]
    calls = [
        {
            "seconds": (
                datetime.fromisoformat(r["finished_at"])
                - datetime.fromisoformat(r["started_at"])
            ).total_seconds(),
            "tools": [
                t.get("function", {}).get("name") for t in r.get("tool_calls", [])
            ],
        }
        for r in records
    ]
    profile = {
        "seconds": elapsed,
        "llm_calls": len(records),
        "calls": calls,
        "todo_only_calls": sum(c["tools"] == ["write_todos"] for c in calls),
    }
    (tmp_path / "profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    print(json.dumps(profile))
    assert completion.summary
    assert not [
        d
        for d in control.last_execution_record.decisions
        if d.physical_intent is not None
    ]
    assert asyncio.run(runner.status()).active_state == "observing"
    assert control.transition_intents.current(asyncio.run(runner.status())) == intent
    assert not communications
    todos = [
        json.loads(t["function"]["arguments"])["todos"]
        for r in sorted(records, key=lambda r: r["started_at"])
        for t in r.get("tool_calls", [])
        if t["function"]["name"] == "write_todos"
    ]
    assert not todos, "A routine waiting heartbeat needs no model-driven bookkeeping"
    assert len(records) == 1
    assert calls[0]["tools"] == ["ManeuverHeartbeatResponse"]


def test_live_timed_closed_loop_freezes_world_and_finishes_short_window(
    tmp_path: Path,
) -> None:
    """Real LLM/tools/FSM/coordinator; deterministic environment, no AirSim."""
    from test_closed_loop_runtime import _revision, _runtime_parts

    def hyper(invocation):
        return HyperHeartbeatDecision(
            invocation.mission_id,
            invocation.plan_revision,
            "no_change",
            "Keep the bounded test plan",
            invocation.trigger_identities,
            (),
        )

    environment, coordinator, _, runner, _, maneuver, _ = _runtime_parts(
        tmp_path, hyper
    )
    config = load_runtime_config(ROOT / "conf/onr_agent_params.yaml", repo_root=ROOT)
    config = replace(
        config,
        debug=True,
        storage=replace(
            config.storage,
            root=tmp_path / "storage",
            planner_artifacts=tmp_path / "planner",
        ),
    )
    runtime = RuntimeComposition(config, maneuver.transport)
    model = runtime.create_chat_model(
        mission_id="mission-1", debug_scope="maneuver-control"
    )
    model.seed = 0
    control = runtime.create_maneuver_control(
        model=model,
        mission_id="mission-1",
        fsm_runner=runner,
        communication_port=maneuver.communication_port,
        system_prompt=load_system_prompt(
            ROOT / "conf/system_prompt", "maneuver-control"
        ),
        skill_catalog=FilesystemRoleSkillCatalog(ROOT / "conf/skills"),
        backend_root=ROOT,
    )
    maneuver.decision_provider = control.decision_provider
    base = _revision(1)
    chart = Statechart(
        mission_id="mission-1",
        plan_revision=1,
        mission_snapshot_id=base.planner_plan.mission_snapshot_id,
        planning_profile="temporal",
        entry_state="observing",
        states=("observing", "complete"),
        terminal_states=("complete",),
        state_context={
            "observing": {
                "surveillance_mode": "fixed_view",
                "observation_window": {
                    "start": {"seconds": 7},
                    "duration": {"seconds": 0.5},
                },
                "desired_outcome": {
                    "kind": "observe_fixed_location",
                    "location": {"x": 10, "y": 0, "z": -250},
                },
            },
            "complete": {"desired_outcome": "bounded observation interval complete"},
        },
        transitions=(
            StatechartTransition(
                "finish",
                "observing",
                "complete",
                {
                    "readiness": {
                        "not_before": {"seconds": 7.5},
                        "live_evidence": "Drone at the selected fixed viewpoint.",
                    },
                },
            ),
        ),
    )
    status = asyncio.run(runner.activate(chart))
    maneuver.transition_intents.select(
        status,
        "complete",
        "Observe at the selected viewpoint through t7.5",
        selected_at=0,
    )
    started = time.monotonic()
    result = coordinator(
        lambda *_: None,
        simulation_limit_seconds=12,
        maneuver_seconds=config.heartbeats.maneuver_seconds,
    ).run(
        ActivePlanRevision(
            base.planner_plan,
            base.planner_plan_reference,
            chart,
            "bounded-timed-chart.json",
        )
    )
    profile = {"wall_seconds": time.monotonic() - started, "result": result.to_dict()}
    (tmp_path / "profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    print(json.dumps(profile))
    assert result.terminal
    assert result.simulated_duration_seconds == 7.5
    assert result.physical_actions == ("navigate",)
    assert result.environment_triggered_maneuver_heartbeat_count == 1
    assert result.maneuver_heartbeat_count == 4  # initial, arrival, window start/end
    assert all(
        w.evidence_time_seconds == w.completion_time_seconds
        for w in result.inference_windows
    )
    assert environment.drone_position == (10.0, 0.0, -250.0)
