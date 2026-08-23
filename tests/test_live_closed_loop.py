from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from onr.contracts.hyper_agent import MissionInput
from onr.ports.transport import Subscription
from onr.runtime.cli import run_closed_loop_demo
from onr.runtime.composition import RuntimeComposition

pytestmark = pytest.mark.live
_REPO_ROOT = Path(__file__).parents[1]


def test_complete_report_runs_real_periodic_and_lifecycle_closed_loop(
    tmp_path: Path,
) -> None:
    values = yaml.safe_load(
        (_REPO_ROOT / "conf/onr_agent_params.yaml").read_text(encoding="utf-8")
    )
    values["transport"]["root"] = str(tmp_path / "transport")
    values["storage"]["root"] = str(tmp_path / "storage")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    runtime = RuntimeComposition.create(
        repo_root=_REPO_ROOT,
        config_path=config_path,
    )
    runtime.verify_llm_reachability()
    mission = MissionInput(
        mission_id=f"live-closed-loop-{uuid4().hex}",
        mission_text=(
            "Please patrol the environment and confirm that all the events "
            "mentioned in the event report are accounted for."
        ),
        source_authority="live-closed-loop-test",
    )

    result = run_closed_loop_demo(
        runtime,
        mission,
        repo_root=_REPO_ROOT,
        planner_artifacts=tmp_path / "planner-artifacts",
        recursion_limit=120,
        simulation_limit_seconds=360,
    )

    assert result.terminal
    assert result.final_fsm_state
    assert result.tick_count > 0
    assert result.maneuver_heartbeat_count > 0
    assert result.environment_triggered_maneuver_heartbeat_count > 0
    assert result.hyper_heartbeat_count > 0
    assert result.physical_actions
    assert result.feedback_count >= len(result.physical_actions)
    assert result.perception_count > 0
    assert result.belief_revisions[0] == 1
    assert result.belief_revisions[-1] > 20
    assert result.belief_revisions == tuple(range(1, result.belief_revisions[-1] + 1))
    assert any(item.disposition == "no_change" for item in result.hyper_outcomes)
    assert any(item.request_identities for item in result.hyper_outcomes)
    assert result.plan_revisions[0] == 1

    context_subscription = Subscription(
        runtime.config.services.context_coordination,
        mission.mission_id,
        "planning-evidence",
    )
    belief_subscription = Subscription(
        "bayesian-belief-manager",
        mission.mission_id,
        "belief-observations",
    )
    assert runtime.transport.get_cursor(context_subscription)["sequence"] == (
        runtime.transport.next_event_sequence("planning-evidence", mission.mission_id)
        - 1
    )
    assert runtime.transport.get_cursor(belief_subscription)["sequence"] == 19
    assert (tmp_path / "planner-artifacts/revision-001").is_dir()
    assert tuple((tmp_path / "storage").rglob("*.json"))
