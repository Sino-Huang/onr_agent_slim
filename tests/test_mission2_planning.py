from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from onr.application.mission2_planning import (
    MISSION2_LOCATION_MATCH_TOLERANCE_M,
    Mission2ReplanGate,
    _active_mission2_assignment,
    collision_observation_candidates,
    joint_observation_priority,
    main,
    mission2_advisory_candidate,
    mission2_candidate_score,
    mission2_trigger_identity,
    write_minizinc_problem,
)
from onr.contracts.fsm import FSMStatus


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


def monitoring_status() -> FSMStatus:
    return FSMStatus(
        mission_id="mission-2",
        plan_revision=1,
        statechart_revision=1,
        active_state="collision-monitoring-active",
    )


def assignment_status(
    env,
    *,
    candidate=None,
    pair=None,
    target=None,
    mode=None,
    run_id=None,
    start_s=None,
    end_s=None,
    location=None,
    plan_revision=1,
) -> FSMStatus:
    candidate = candidate or collision_observation_candidates(env)[0]
    location = location or {"x": candidate.x, "y": candidate.y, "z": candidate.z}
    context = {
        "candidate_id": candidate.candidate_id,
        "surveillance_mode": mode or candidate.mode,
        "target_entity_id": candidate.target_entity_id if target is None else target,
        "risk_pair": list(candidate.ship_ids if pair is None else pair),
        "prediction_run_id": run_id or candidate.prediction_run_id,
        "prediction_sequence": candidate.prediction_sequence,
        "observation_window": {
            "start_s": candidate.start_s if start_s is None else start_s,
            "end_s": candidate.end_s if end_s is None else end_s,
        },
        "desired_outcome": {"location": location},
        "planner_item": candidate.to_dict(),
    }
    return FSMStatus(
        mission_id="mission-2",
        plan_revision=plan_revision,
        statechart_revision=plan_revision,
        active_state="risk-observation-1",
        active_state_context=context,
    )


def add_pair(env, pair=(3, 4), *, contact=20.0, probability=None, offset=0.0):
    prediction = env["world_model_info"]["perception_predictions"]
    prediction["active_pairs"].append(
        {
            "ship_ids": list(pair),
            "probability": probability,
            "predicted_contact_at_s": contact,
        }
    )
    for ship in pair:
        prediction["trajectories"][str(ship)] = {
            "ready": True,
            "sampled_at_s": 5,
            "points": [
                {
                    "time_s": 5 + t,
                    "position": {"x": 85 + offset + t, "y": 0, "z": None},
                }
                for t in range(31)
            ],
        }


def set_lifecycle(env, status, *, action="navigate", lifecycle="active", revision=None):
    assignment = _active_mission2_assignment(status)
    assert assignment is not None
    parameters = (
        {"entity_id": assignment.target_entity_id}
        if action == "pursue"
        else {"x": assignment.location[0], "y": assignment.location[1], "z": -25}
    )
    env["maneuver_lifecycle"] = {
        "action": action,
        "lifecycle": lifecycle,
        "plan_revision": status.plan_revision if revision is None else revision,
        "parameters": parameters,
    }


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


def test_ten_metre_entry_is_urgency_not_an_actual_contact_deadline():
    env = environment()
    env["world_model_info"]["perception_predictions"]["active_pairs"][0]["predicted_contact_at_s"] = 6
    candidates = collision_observation_candidates(env)
    assert candidates
    assert candidates[0].start_s > 6


def test_gate_coalesces_duplicates_and_detects_changed_cleared_and_stale_risks():
    gate = Mission2ReplanGate()
    env = environment()
    status = monitoring_status()
    first = gate.assess(env, status)
    assert first.trigger and first.reason == "new_serviceable_risk"
    assert first.risk_revision.revision == 1
    duplicate = gate.assess(copy.deepcopy(env), status)
    assert duplicate.reason == "new_serviceable_risk"
    assert not duplicate.risk_revision.material_changed
    prediction = env["world_model_info"]["perception_predictions"]
    prediction["sequence"] += 1
    assert not gate.assess(env, status).risk_revision.material_changed
    candidate_ids = tuple(sorted(c.candidate_id for c in collision_observation_candidates(env)))
    env["controlled_vehicle"]["position"]["x"] = 200
    assert tuple(sorted(c.candidate_id for c in collision_observation_candidates(env))) == candidate_ids
    assert not gate.assess(env, status).risk_revision.material_changed
    env["controlled_vehicle"]["position"]["x"] = 0
    prediction["alerts"] = [{"event_id": "warning-1", "ship_ids": [1, 2]}]
    warning = gate.assess(env, status)
    assert warning.trigger and warning.reason == "new_warning"
    assert mission2_trigger_identity(warning) == "mission2-gate:new_warning:risk-run:2"
    assert not gate.assess(env, status).risk_revision.material_changed
    prediction["active_pairs"] = []
    cleared = gate.assess(env, status)
    assert cleared.risk_revision.material_causes == ("pair_removed", "candidate_feasibility_changed")
    assert not cleared.trigger and cleared.reason == "no_serviceable_risk"
    assert not gate.assess(env, status).risk_revision.material_changed
    env["mission_time_seconds"] = 11
    stale = gate.assess(env, status)
    assert stale.risk_revision.material_causes == ("forecast_state_changed",)
    assert not stale.trigger and stale.reason == "no_serviceable_risk"
    assert not gate.assess(env, status).risk_revision.material_changed


def test_gate_coalesces_contact_time_floating_point_jitter() -> None:
    gate = Mission2ReplanGate()
    env = environment()
    status = monitoring_status()
    assert gate.assess(env, status).risk_revision.material_changed

    refreshed = copy.deepcopy(env)
    refreshed["mission_time_seconds"] = 5.5
    prediction = refreshed["world_model_info"]["perception_predictions"]
    prediction["sequence"] = 2
    prediction["valid_until_s"] = 10.5
    prediction["active_pairs"][0]["predicted_contact_at_s"] = 20.000000000001

    assert not gate.assess(refreshed, status).risk_revision.material_changed
    prediction["active_pairs"][0]["predicted_contact_at_s"] = 20.49
    assert not gate.assess(refreshed, status).risk_revision.material_changed
    prediction["active_pairs"][0]["predicted_contact_at_s"] = 20.5
    changed = gate.assess(refreshed, status).risk_revision
    assert changed.material_causes == ("contact_time_moved",)


def test_contact_threshold_accumulates_against_last_material_baseline():
    gate = Mission2ReplanGate()
    env = environment()
    status = monitoring_status()
    gate.assess(env, status)
    pair = env["world_model_info"]["perception_predictions"]["active_pairs"][0]
    for contact in (20.3, 20.49):
        pair["predicted_contact_at_s"] = contact
        assert not gate.assess(env, status).risk_revision.material_changed
    pair["predicted_contact_at_s"] = 20.5
    assert gate.assess(env, status).risk_revision.material_causes == ("contact_time_moved",)


def test_cpa_and_probability_material_thresholds():
    status = monitoring_status()
    env = environment(probability=.50)
    pair = env["world_model_info"]["perception_predictions"]["active_pairs"][0]
    pair["position"] = {"x": 0.0, "y": 0.0}
    gate = Mission2ReplanGate()
    gate.assess(env, status)
    pair["position"]["x"] = 24.99
    assert not gate.assess(env, status).risk_revision.material_changed
    pair["position"]["x"] = 25.0
    assert gate.assess(env, status).risk_revision.material_causes == ("cpa_moved",)

    env = environment(probability=.50)
    pair = env["world_model_info"]["perception_predictions"]["active_pairs"][0]
    gate = Mission2ReplanGate()
    gate.assess(env, status)
    pair["probability"] = .59
    assert not gate.assess(env, status).risk_revision.material_changed
    pair["probability"] = .60
    assert gate.assess(env, status).risk_revision.material_causes == ("probability_changed",)

    env = environment(probability=None)
    gate = Mission2ReplanGate()
    gate.assess(env, status)
    env["world_model_info"]["perception_predictions"]["active_pairs"][0]["probability"] = .01
    assert gate.assess(env, status).risk_revision.material_causes == ("probability_changed",)


def test_pair_add_remove_each_create_one_revision_and_duplicate_does_not():
    gate = Mission2ReplanGate()
    env = environment()
    status = monitoring_status()
    assert gate.assess(env, status).risk_revision.revision == 1
    add_pair(env)
    added = gate.assess(env, status).risk_revision
    assert "pair_added" in added.material_causes and added.revision == 2
    assert not gate.assess(copy.deepcopy(env), status).risk_revision.material_changed
    env["world_model_info"]["perception_predictions"]["active_pairs"] = env[
        "world_model_info"
    ]["perception_predictions"]["active_pairs"][:1]
    removed = gate.assess(env, status).risk_revision
    assert "pair_removed" in removed.material_causes and removed.revision == 3
    assert not gate.assess(env, status).risk_revision.material_changed


def test_fresh_alert_is_unconditional_and_deduplicated_per_run():
    gate = Mission2ReplanGate()
    env = environment()
    status = monitoring_status()
    gate.assess(env, status)
    prediction = env["world_model_info"]["perception_predictions"]
    prediction["alerts"] = [{"event_id": "near-1", "ship_ids": [1, 2]}]
    assert gate.assess(env, status).reason == "new_warning"
    repeated = gate.assess(env, status)
    assert repeated.reason != "new_warning" and not repeated.risk_revision.material_changed
    prediction["alerts"].append({"event_id": "collision-1", "ship_ids": [1, 2]})
    escalation = gate.assess(env, status)
    assert escalation.trigger and escalation.reason == "new_warning"
    assert escalation.risk_revision.fresh_alert_ids == ("collision-1",)


def test_ready_empty_forecast_and_stale_monitoring_do_not_trigger_hyper():
    gate = Mission2ReplanGate()
    env = environment()
    env["world_model_info"]["perception_predictions"]["active_pairs"] = []
    ready = gate.assess(env, monitoring_status())
    assert ready.risk_revision.material_changed and not ready.trigger
    env["mission_time_seconds"] = 11
    stale = gate.assess(env, monitoring_status())
    assert stale.risk_revision.forecast_state == "stale"
    assert not stale.trigger and stale.reason == "no_serviceable_risk"


def test_stale_forecast_with_active_assignment_changes_authority():
    env = environment()
    status = assignment_status(env, end_s=20)
    env["mission_time_seconds"] = 11
    decision = Mission2ReplanGate().assess(env, status)
    assert decision.trigger and decision.reason == "forecast_authority_changed"


def test_fixed_view_coverage_and_pair_target_identity():
    env = environment()
    status = assignment_status(env)
    set_lifecycle(env, status)
    covered = Mission2ReplanGate().assess(env, status)
    assert not covered.trigger and covered.reason == "current_plan_covered"

    wrong_pair = assignment_status(env, pair=(2, 3), target=1)
    set_lifecycle(env, wrong_pair)
    decision = Mission2ReplanGate().assess(env, wrong_pair)
    assert decision.trigger and decision.reason == "planned_risk_cleared"


def test_active_exact_target_pursuit_is_covered():
    env = environment()
    env["world_model_info"]["visible_ship_ids"] = [1]
    candidate = next(c for c in collision_observation_candidates(env) if c.target_entity_id == 1)
    status = assignment_status(env, candidate=candidate)
    set_lifecycle(env, status, action="pursue")
    decision = Mission2ReplanGate().assess(env, status)
    assert not decision.trigger and decision.reason == "current_plan_covered"


def test_older_plan_lifecycle_cannot_prove_pursuit_coverage():
    env = environment()
    env["world_model_info"]["visible_ship_ids"] = [1]
    candidate = next(c for c in collision_observation_candidates(env) if c.target_entity_id == 1)
    status = assignment_status(env, candidate=candidate)
    set_lifecycle(env, status, action="pursue", revision=0)
    env["controlled_vehicle"]["max_velocity"] = 200
    env["controlled_vehicle"]["fov_radius"] = 10
    trajectory = env["world_model_info"]["perception_predictions"]["trajectories"]["1"]
    for point in trajectory["points"]:
        point["position"]["x"] += 150
    decision = Mission2ReplanGate().assess(env, status)
    assert decision.trigger and decision.reason == "coverage_lost"


@pytest.mark.parametrize("speed,covered", [(10.0, True), (9.0, False)])
def test_arrival_slack_boundary(speed, covered):
    env = environment()
    status = assignment_status(env)
    assignment = _active_mission2_assignment(status)
    env["mission_time_seconds"] = 9.0
    env["world_model_info"]["perception_predictions"]["valid_until_s"] = 12
    env["controlled_vehicle"]["position"]["x"] = assignment.location[0] - 15
    env["controlled_vehicle"]["max_velocity"] = speed
    set_lifecycle(env, status)
    decision = Mission2ReplanGate().assess(env, status)
    assert (decision.reason == "current_plan_covered") is covered
    assert decision.reason == ("current_plan_covered" if covered else "coverage_lost")


def test_candidate_location_tolerance_then_requires_trajectory_fov_proof():
    env = environment()
    status = assignment_status(env)
    assignment = _active_mission2_assignment(status)
    set_lifecycle(env, status)
    assert MISSION2_LOCATION_MATCH_TOLERANCE_M == 25.0
    for trajectory in env["world_model_info"]["perception_predictions"]["trajectories"].values():
        for point in trajectory["points"]:
            point["position"]["x"] += 24.9
    assert Mission2ReplanGate().assess(env, status).reason == "current_plan_covered"

    for trajectory in env["world_model_info"]["perception_predictions"]["trajectories"].values():
        for point in trajectory["points"]:
            point["position"]["x"] += 1.0
    env["controlled_vehicle"]["fov_radius"] = 10
    env["controlled_vehicle"]["max_velocity"] = 200
    env["controlled_vehicle"]["position"]["x"] = assignment.location[0]
    assert Mission2ReplanGate().assess(env, status).reason == "coverage_lost"


def test_planned_pair_clear_triggers_but_unrelated_pair_removal_does_not():
    env = environment()
    add_pair(env)
    status = assignment_status(env)
    set_lifecycle(env, status)
    gate = Mission2ReplanGate()
    gate.assess(env, status)
    prediction = env["world_model_info"]["perception_predictions"]
    prediction["active_pairs"] = prediction["active_pairs"][:1]
    unrelated = gate.assess(env, status)
    assert not unrelated.trigger and unrelated.reason == "current_plan_covered"

    add_pair(env)
    gate.assess(env, status)
    prediction["active_pairs"] = prediction["active_pairs"][1:]
    cleared = gate.assess(env, status)
    assert cleared.trigger and cleared.reason == "planned_risk_cleared"


@pytest.mark.parametrize(
    "probability,reason", [(.59, "current_plan_preferred"), (.60, "higher_priority_risk")]
)
def test_competing_risk_improvement_threshold(probability, reason):
    env = environment(probability=.50)
    for trajectory in env["world_model_info"]["perception_predictions"]["trajectories"].values():
        for point in trajectory["points"]:
            point["position"]["x"] -= 5 if point is trajectory["points"][0] else 0
    add_pair(env, probability=probability)
    status = assignment_status(env)
    set_lifecycle(env, status)
    decision = Mission2ReplanGate().assess(env, status)
    assert decision.reason == reason
    assert decision.trigger is (reason == "higher_priority_risk")


def test_observation_window_and_mission_end_never_trigger_replan():
    env = environment()
    status = assignment_status(env)
    env["mission_time_seconds"] = _active_mission2_assignment(status).observation_end_s
    env["world_model_info"]["perception_predictions"]["valid_until_s"] = 100
    window = Mission2ReplanGate().assess(env, status)
    assert not window.trigger and window.reason == "observation_window_complete"

    env = environment()
    env["mission_time_seconds"] = 100
    prediction = env["world_model_info"]["perception_predictions"]
    prediction["valid_until_s"] = 100
    prediction["alerts"] = [{"event_id": "at-end", "ship_ids": [1, 2]}]
    ended = Mission2ReplanGate().assess(env, monitoring_status())
    assert not ended.trigger and ended.reason == "mission_complete"


def test_prediction_run_reset_clears_baseline_and_alert_dedup():
    gate = Mission2ReplanGate()
    env = environment()
    prediction = env["world_model_info"]["perception_predictions"]
    prediction["alerts"] = [{"event_id": "warning-1", "ship_ids": [1, 2]}]
    assert gate.assess(env, monitoring_status()).reason == "new_warning"
    prediction["run_id"] = "risk-run-2"
    prediction["active_pairs"] = []
    prediction["alerts"] = []
    reset = gate.assess(env, monitoring_status())
    assert reset.risk_revision.revision == 1
    assert reset.risk_revision.material_causes == ("prediction_run_changed",)
    assert not reset.trigger
    prediction["alerts"] = [{"event_id": "warning-1", "ship_ids": [1, 2]}]
    assert gate.assess(env, monitoring_status()).reason == "new_warning"


def test_mission1_only_does_not_enable_collision_gate():
    decision = Mission2ReplanGate().assess({"world_model_info": {}}, monitoring_status())
    assert not decision.trigger and decision.reason == "not_mission2"
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


def test_candidate_scoring_matches_minizinc_writer_values(tmp_path):
    env = environment(probability=.85)
    candidates = collision_observation_candidates(env)
    report = {
        "mission_time_seconds": env["mission_time_seconds"],
        "valid_until_s": env["world_model_info"]["perception_predictions"]["valid_until_s"],
        "mission_end_time_s": env["world_model_info"]["mission_end_time_s"],
        "candidates": [candidate.to_dict() for candidate in candidates],
    }
    model = tmp_path / "model.mzn"
    data = tmp_path / "data.dzn"
    write_minizinc_problem(report, model, data)
    values = {
        line.split(" = ", 1)[0]: json.loads(line.split(" = ", 1)[1][:-1])
        for line in data.read_text().splitlines()
        if line.startswith(("candidate_weight", "travel_millis"))
    }
    now = env["mission_time_seconds"]
    expected_travel = [round(1000 * candidate.travel_seconds) for candidate in candidates]
    expected_weight = [
        (mission2_candidate_score(candidate, now) + travel) // 100000
        for candidate, travel in zip(candidates, expected_travel)
    ]
    assert values == {
        "candidate_weight": expected_weight,
        "travel_millis": expected_travel,
    }
    assert mission2_advisory_candidate(candidates, now) == min(
        candidates,
        key=lambda candidate: (
            -mission2_candidate_score(candidate, now),
            candidate.travel_seconds,
            candidate.candidate_id,
        ),
    )


@pytest.mark.parametrize("ready", [True, False])
def test_candidate_cli_writes_verified_minizinc_problem(tmp_path,ready,capsys):
    env=environment()
    if not ready:
        env["world_model_info"]["perception_predictions"]["status"]="not_ready"
    source=tmp_path / "environment.json";source.write_text(json.dumps(env))
    output=tmp_path / "candidates.json";model=tmp_path / "model.mzn";data=tmp_path / "data.dzn"
    assert main([str(source),"--output",str(output),"--model",str(model),"--data",str(data)])==0
    capsys.readouterr()
    executable=Path(__file__).parents[1] / "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc"
    result=subprocess.run([str(executable),"--solver","coin-bc",str(model),str(data)],
        text=True,capture_output=True,timeout=30,check=False)
    assert result.returncode==0,result.stderr
    solved=json.loads(result.stdout.splitlines()[0])
    assert bool(solved["selected_indices"]) is ready


def test_code_owned_statechart_keeps_monitoring_until_recording_end(tmp_path,capsys):
    source=tmp_path / "environment.json";source.write_text(json.dumps(environment()))
    candidates=tmp_path / "mission2-candidates.json"
    assert main([str(source),"--output",str(candidates)])==0
    capsys.readouterr()
    plan=tmp_path / "minizinc.plan"
    plan.write_text(json.dumps({"type":"solution","output":{
        "default":json.dumps({"selected_indices":[1],"monitor_until_s":10})}})+"\n")
    chart=tmp_path / "statechart.json"
    helper=Path(__file__).parents[1] / "conf/skills/hyper/creating-statechart-files/examples/mission2-collision/prepare_statechart.py"
    result=subprocess.run([str(helper),str(plan),str(candidates),str(chart)],
        text=True,capture_output=True,timeout=10,check=False)
    assert result.returncode==0,result.stderr
    value=json.loads(chart.read_text())
    assert value["entry_state"]=="risk-observation-1"
    assert "scenario-recording-end" in value["states"]
    terminal=value["transitions"][-1]
    assert terminal["context"]["readiness"]["mission_time_at_or_after"]["seconds"]==100
