from __future__ import annotations

import copy
import json

import pytest

from onr.application.mission2_planning import (
    Mission2ReplanGate, collision_observation_candidates, joint_observation_priority, main,
)


def environment(*, source="simulated", probability=None):
    return {"mission_time_seconds": 5.0,
        "controlled_vehicle": {"position": {"x": 0, "y": 0, "z": -25}, "max_velocity": 20, "fov_radius": 100},
        "world_model_info": {"mission_mode": "mission2", "mission_end_time_s": 100,
            "visible_ship_ids": [], "perception_predictions": {
                "schema_version": 1, "run_id": "risk-run", "sequence": 1, "source": source,
                "status": "ready", "valid_until_s": 10,
                "active_pairs": [{"ship_ids": [1, 2], "probability": probability, "predicted_contact_at_s": 20}],
                "alerts": [], "trajectories": {str(i): {"ready": True, "sampled_at_s": 5,
                    "points": [{"time_s": 5+t, "position": {"x": 80+i*5+t, "y": 0, "z": None}}
                               for t in range(31)]} for i in (1, 2)}}}}


def test_live_and_standalone_use_same_observation_candidates():
    simulated = collision_observation_candidates(environment())
    live = collision_observation_candidates(environment(source="sukai", probability=.85))
    assert simulated and len(simulated) == len(live)
    assert simulated[0].candidate_id == live[0].candidate_id
    assert simulated[0].probability is None and live[0].probability == .85
    assert simulated[0].mode == "fixed_view"
    assert simulated[0].start_s >= 5 + simulated[0].travel_seconds
    assert simulated[0].end_s <= 20
    assert simulated[0].z == -25


def test_visibility_permits_pursuit_but_never_comes_from_pair_membership():
    env = environment()
    assert all(c.mode == "fixed_view" for c in collision_observation_candidates(env))
    env["world_model_info"]["visible_ship_ids"] = [1]
    candidates = collision_observation_candidates(env)
    assert next(c for c in candidates if c.target_entity_id == 1).mode == "pursue_ship"
    assert next(c for c in candidates if c.target_entity_id == 2).mode == "fixed_view"


@pytest.mark.parametrize("state", ["not_ready", "unavailable", "stale"])
def test_unready_and_expired_forecasts_are_not_current_candidates(state):
    env = environment()
    env["world_model_info"]["perception_predictions"]["status"] = state
    assert not collision_observation_candidates(env)
    env["world_model_info"]["perception_predictions"]["status"] = "ready"
    env["mission_time_seconds"] = 11
    assert not collision_observation_candidates(env)


def test_unreachable_risk_does_not_create_a_teleport_plan():
    env = environment()
    env["controlled_vehicle"]["max_velocity"] = .01
    assert not collision_observation_candidates(env)


def test_active_pair_is_not_expired_by_its_original_ten_metre_crossing():
    env = environment()
    env["world_model_info"]["perception_predictions"]["active_pairs"][0]["predicted_contact_at_s"] = 4
    candidates = collision_observation_candidates(env)
    assert candidates, "the producer still marks this ongoing risk active"
    assert candidates[0].predicted_contact_at_s == 4


def test_gate_coalesces_duplicates_and_detects_changed_cleared_and_stale_risks():
    gate = Mission2ReplanGate()
    env = environment()
    assert "risk_changed" in gate.assess(env)
    assert gate.assess(copy.deepcopy(env)) is None
    prediction = env["world_model_info"]["perception_predictions"]
    prediction["sequence"] += 1
    assert gate.assess(env) is None
    prediction["alerts"] = [{"event_id": "warning-1"}]
    assert "new_warning" in gate.assess(env)
    assert gate.assess(env) is None
    prediction["active_pairs"] = []
    assert "risk_changed" in gate.assess(env)
    assert gate.assess(env) is None
    env["mission_time_seconds"] = 11
    assert "risk_changed" in gate.assess(env)
    assert gate.assess(env) is None


def test_mission1_only_does_not_enable_collision_gate():
    assert Mission2ReplanGate().assess({"world_model_info": {}}) is None
    assert collision_observation_candidates({"world_model_info": {}}) == ()


def test_joint_priority_is_explicit_and_balanced_does_not_starve():
    args = dict(mission1_available=True, mission2_available=True)
    assert joint_observation_priority(configured_priority="mission1", last_served=None, **args) == "mission1"
    assert joint_observation_priority(configured_priority="mission2", last_served=None, **args) == "mission2"
    first = joint_observation_priority(configured_priority="balanced", last_served=None, **args)
    second = joint_observation_priority(configured_priority="balanced", last_served=first, **args)
    assert {first, second} == {"mission1", "mission2"}


def test_candidate_cli_needs_no_reports_or_bayesian_state(tmp_path, capsys):
    source = tmp_path / "environment.json"
    source.write_text(json.dumps(environment()))
    output = tmp_path / "candidates.json"
    assert main([str(source), "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["candidate_count"] == 2
    assert report["candidates"][0]["probability"] is None
    assert json.loads(capsys.readouterr().out)["prediction_source"] == "simulated"
