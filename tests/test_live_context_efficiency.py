"""Local real-model heartbeat checks; no environment service or Mission loop."""

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
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.contracts.fsm import Statechart, StatechartTransition
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
    assert todos and all(t["status"] == "completed" for t in todos[-1])
