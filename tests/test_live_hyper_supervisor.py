from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from onr.adapters.system_prompts import load_system_prompt
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.hyper_agent import HyperHeartbeatInvocation
from onr.runtime.composition import RuntimeComposition

pytestmark = pytest.mark.live
_REPO_ROOT = Path(__file__).parents[1]


def test_stable_evidence_produces_live_no_change_decision(tmp_path: Path) -> None:
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
    mission_id = f"live-hyper-supervisor-{uuid4().hex}"
    supervisor = runtime.create_hyper_supervisor(
        system_prompt=load_system_prompt(
            _REPO_ROOT / "conf/system_prompt", "hyper-supervisor"
        ),
        mission_id=mission_id,
        backend_root=tmp_path,
    )
    invocation = HyperHeartbeatInvocation(
        mission_id=mission_id,
        plan_revision=1,
        trigger_identities=("periodic:10",),
        mission_snapshot=MissionSnapshot(
            mission_id,
            4,
            "2026-08-23T00:00:00+10:00",
            plan_revision=1,
            plan_reference="planner-plan.json",
            source_revisions={"plan": 1, "environment_data": 1},
            source_health={"plan": "healthy", "environment_data": "healthy"},
            source_freshness={"plan": True, "environment_data": True},
        ),
        planner_plan_reference="planner-plan.json",
        statechart_reference="statechart.json",
        fsm_status=FSMStatus(
            mission_id=mission_id,
            plan_revision=1,
            statechart_revision=1,
            active_state="surveying",
        ),
        environment_data={"scene_graph": {"mission_time_seconds": 10}},
    )

    decision = supervisor.heartbeat(invocation)

    assert decision.disposition == "no_change"
    assert decision.trigger_identities == ("periodic:10",)
