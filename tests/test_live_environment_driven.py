from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from onr.contracts.hyper_agent import MissionInput
from onr.runtime.cli import run_closed_loop_demo
from onr.runtime.composition import RuntimeComposition

pytestmark = pytest.mark.live
_REPO_ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "update_ownership", ["environment_driven", "coordinator_driven"]
)
def test_update_profile_respects_ownership_during_live_inference(
    tmp_path: Path, update_ownership: str
) -> None:
    environment_values = yaml.safe_load(
        (_REPO_ROOT / "conf/environment_params.yaml").read_text(encoding="utf-8")
    )
    environment_values["updates"] = {
        "ownership": update_ownership,
        "cadence_seconds": 0.5,
    }
    environment_values["fake"]["artifact_root"] = str(tmp_path / "environment")
    environment_profile = tmp_path / "environment.yaml"
    environment_profile.write_text(
        yaml.safe_dump(environment_values, sort_keys=False), encoding="utf-8"
    )

    runtime_values = yaml.safe_load(
        (_REPO_ROOT / "conf/onr_agent_params.yaml").read_text(encoding="utf-8")
    )
    runtime_values["environment_profile"] = str(environment_profile)
    runtime_values["transport"]["root"] = str(tmp_path / "transport")
    runtime_values["storage"]["root"] = str(tmp_path / "storage")
    runtime_values["storage"]["planner_artifacts"] = str(tmp_path / "planner-artifacts")
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(
        yaml.safe_dump(runtime_values, sort_keys=False), encoding="utf-8"
    )
    runtime = RuntimeComposition.create(
        repo_root=_REPO_ROOT,
        config_path=runtime_path,
    )
    runtime.verify_llm_reachability()
    mission = MissionInput(
        mission_id=f"live-{update_ownership}-{uuid4().hex}",
        mission_text=(
            "Patrol the environment and begin accounting for the events in the "
            "event report."
        ),
        source_authority=f"live-{update_ownership}-test",
    )

    result = run_closed_loop_demo(
        runtime,
        mission,
        repo_root=_REPO_ROOT,
        planner_artifacts=tmp_path / "planner-artifacts",
        recursion_limit=120,
        simulation_limit_seconds=15,
    )

    assert result.simulated_duration_seconds == 15
    assert result.inference_windows
    if update_ownership == "environment_driven":
        assert result.maximum_update_batch > 1
        assert result.coalesced_update_count >= 2
        assert any(
            window.completion_time_seconds > window.evidence_time_seconds
            for window in result.inference_windows
        )
        assert all(
            window.completion_time_seconds >= window.evidence_time_seconds
            for window in result.inference_windows
        )
    else:
        assert result.maximum_update_batch == 1
        assert result.coalesced_update_count == 0
        assert all(
            window.completion_time_seconds == window.evidence_time_seconds
            for window in result.inference_windows
        )

    command_directory = (
        tmp_path / "transport" / "commands" / "maneuver-adapter" / mission.mission_id
    )
    command_envelopes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in command_directory.glob("*.json")
    ]
    commands = [item for item in command_envelopes if item.get("kind") == "command"]
    assert commands
    assert len(tuple((tmp_path / "transport" / "receipts").glob("*.json"))) == len(
        commands
    )
    assert not tuple((tmp_path / "transport" / "identity").glob("outcome-*.json"))

    feedback_directory = (
        tmp_path
        / "transport"
        / "topics"
        / "maneuver-feedback"
        / "missions"
        / mission.mission_id
    )
    feedback_ids = {
        json.loads(path.read_text(encoding="utf-8"))["event_id"]
        for path in feedback_directory.glob("*.json")
    }
    assert len(feedback_ids) == result.feedback_count
