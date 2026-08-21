from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from onr.contracts.hyper_agent import MissionInput
from onr.runtime import RuntimeComposition
from onr.runtime.maneuver_cli import run_maneuver_demo

pytestmark = pytest.mark.live
_REPO_ROOT = Path(__file__).parents[1]


def test_live_maneuver_agent_drives_verified_ten_state_patrol() -> None:
    mission = MissionInput(
        mission_id=f"live-maneuver-{uuid4().hex}",
        mission_text="Execute the accepted four-stop Maneuver patrol.",
        source_authority="live-maneuver-test",
    )
    runtime = RuntimeComposition.create(
        repo_root=_REPO_ROOT,
        config_path=_REPO_ROOT / "conf/onr_agent_params.yaml",
    )
    runtime.verify_llm_reachability()

    result = run_maneuver_demo(
        runtime,
        mission,
        repo_root=_REPO_ROOT,
        artifact_root=(
            _REPO_ROOT / "var/planner-artifacts/maneuver-live" / mission.mission_id
        ),
    )

    assert result.final_state == "patrol-complete"
    assert result.heartbeat_count == 10
    assert result.transition_count == 9
    assert result.physical_actions == (
        "navigate",
        "navigate",
        "navigate",
        "navigate",
        "land",
    )
    assert result.belief_revision == 1
    assert result.hyper_message_count == 1
    assert result.override_confirmed is True
    assert result.agent_log_directory.is_dir()
    assert result.llm_log_directory.is_dir()
