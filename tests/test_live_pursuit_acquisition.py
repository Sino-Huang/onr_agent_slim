"""Real-model decisions at the public pursuit-acquisition boundary.

Fixtures supply observations; physical movement is covered by runtime replay.
The GPS case tests a supplied fix, not availability of a live GPS publisher.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
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


@pytest.mark.parametrize(
    "phase, expected",
    [
        ("unseen", "navigate"),
        ("arrived", "pursue"),
        ("tracking", None),
        ("new-gps", "navigate"),
        ("no-gps", None),
        ("stale-gps", None),
    ],
)
def test_live_pursuit_acquisition_and_recovery(
    tmp_path: Path, phase: str, expected: str | None
) -> None:
    config = load_runtime_config(ROOT / "conf/onr_agent_params.yaml", repo_root=ROOT)
    config = replace(
        config,
        storage=replace(
            config.storage,
            root=tmp_path / "storage",
            planner_artifacts=tmp_path / "planner",
        ),
    )
    transport = InProcessTransport()
    runtime = RuntimeComposition(config, transport)
    mission = f"acquisition-{phase}"
    runner = FSMRunner(transport, store=InMemoryFSMStateStore())
    window = {"start": {"seconds": 52}, "duration": {"seconds": 30}}
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
                "surveillance_mode": "pursue_ship",
                "target_entity_id": 23,
                "target_report_ids": ["report-first", "report-last"],
                "observation_window": window,
                "desired_outcome": {
                    "kind": "maintain_moving_entity_visibility",
                    "entity_id": 23,
                    "physical_action": "pursue",
                    "evidence_window": window,
                    "acquisition_rendezvous": {
                        "location": {"x": 180, "y": -20},
                        "arrival_deadline": {"seconds": 52},
                        "position_source": "planner_public_report",
                    },
                },
            },
            "done": {"desired_outcome": "report evidence complete"},
        },
        transitions=(
            StatechartTransition(
                "confirm",
                "observing",
                "done",
                {
                    "readiness": {
                        "not_before": {"seconds": 82},
                        "sensed_evidence": {
                            "report_ids": ["report-first", "report-last"],
                            "report_check_ledger": "world_model_info.event_report_checks",
                        },
                    }
                },
            ),
        ),
    )
    status = asyncio.run(runner.activate(chart))
    communication = TransportCommunicationPort(transport)
    messages = []
    communication.register(
        "hyper-agent",
        lambda message: messages.append(message) or {"status": "received"},
    )
    control = runtime.create_maneuver_control(
        mission_id=mission,
        fsm_runner=runner,
        communication_port=communication,
        system_prompt=load_system_prompt(
            ROOT / "conf/system_prompt", "maneuver-control"
        ),
        skill_catalog=FilesystemRoleSkillCatalog(ROOT / "conf/skills"),
        backend_root=ROOT,
    )
    now = (
        40
        if phase == "unseen"
        else 46
        if phase == "arrived"
        else 61
        if phase == "new-gps"
        else 55
    )
    position = {"x": 40 if phase == "unseen" else 180, "y": -20, "z": -25}
    visible = phase == "tracking"
    active = (
        None
        if phase == "unseen"
        else {
            "action": "navigate" if phase == "arrived" else "pursue",
            "maneuver_id": "acquire-report-first" if phase == "arrived" else "track-23",
            "lifecycle": "completed" if phase == "arrived" else "active",
            "phase": "navigate"
            if phase == "arrived"
            else "pursuit"
            if visible
            else "search",
            "start_time": 40 if phase == "arrived" else 46,
            "plan_revision": 1,
            "parameters": {"x": 180, "y": -20, "z": -25, "deadline_time": 52}
            if phase == "arrived"
            else {"entity_id": 23},
            "progress": {"remaining_distance_m": 0}
            if phase == "arrived"
            else {"entity_id": 23},
        }
    )
    world = {
        "visible_ship_ids": [23] if visible else [],
        "visible_ships": [
            {
                "ship_id": 23,
                "observed_at_time_s": now,
                "ned_pose": {"north_m": 185, "east_m": -20, "down_m": -2.5},
            }
        ]
        if visible
        else [],
        "event_report_checks": [],
    }
    if phase in {"new-gps", "stale-gps"}:
        world["gps_interval_seconds"] = 30.0
        world["next_gps_update_time_s"] = 90.0 if phase == "new-gps" else 60.0
        world["public_position_fixes"] = [
            {
                "entity_id": 23,
                "source": "gps",
                "sampled_at_s": 60 if phase == "new-gps" else 30,
                "position": {"x": 150, "y": 60, "z": -2.5},
            }
        ]
    invocation = ManeuverInvocation(
        request_id=f"heartbeat-{phase}",
        correlation_id=mission,
        mission_id=mission,
        plan_revision=1,
        statechart_reference="accepted-statechart.json",
        fsm_context=control.transition_intents.focused_context(status, None),
        environment_data={
            "mission_time_seconds": now,
            "controlled_vehicle": {
                "position": position,
                "max_velocity": 30,
                "fov_radius": 100,
                "flight_state": "flying",
                "speed_mps": 0,
            },
            "maneuver_lifecycle": active,
            "world_model_info": world,
        },
        available_recipients=("hyper-agent",),
    )
    control.heartbeat(invocation)
    physical = [
        d.physical_intent
        for d in control.last_execution_record.decisions
        if d.physical_intent is not None
    ]
    assert [p.action for p in physical] == ([] if expected is None else [expected])
    assert asyncio.run(runner.status()).active_state == "observing"
    if expected:
        parameters = {p.name: p.value for p in physical[0].parameters}
        if expected == "pursue":
            assert (
                parameters["entity_id"] == 23 and type(parameters["entity_id"]) is int
            )
        else:
            assert (parameters["x"], parameters["y"], parameters["z"]) == (
                (150, 60, -25) if phase == "new-gps" else (180, -20, -25)
            )
            if phase == "unseen":
                assert parameters["deadline_time"] == 52
    if phase in {"new-gps", "no-gps", "stale-gps"}:
        assert messages, "A missed report must be escalated, not counted as observed"
