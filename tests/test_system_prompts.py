from __future__ import annotations

from pathlib import Path

import pytest

from onr.adapters.system_prompts import load_system_prompt


def test_load_system_prompt_reads_role_file(tmp_path: Path) -> None:
    prompt_path = tmp_path / "hyper-agent" / "SYSTEM.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Use the configured tools.", encoding="utf-8")

    assert load_system_prompt(tmp_path, "hyper-agent") == "Use the configured tools."


@pytest.mark.parametrize(
    "role", ["", " ", ".", "..", "../hyper-agent", "nested/hyper-agent", "/tmp"]
)
def test_load_system_prompt_rejects_invalid_role_components(
    tmp_path: Path, role: str
) -> None:
    with pytest.raises(ValueError, match="one valid path component"):
        load_system_prompt(tmp_path, role)


def test_load_system_prompt_rejects_missing_unreadable_and_blank_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="cannot be read"):
        load_system_prompt(tmp_path, "missing")

    unreadable = tmp_path / "unreadable" / "SYSTEM.md"
    unreadable.mkdir(parents=True)
    with pytest.raises(ValueError, match="cannot be read"):
        load_system_prompt(tmp_path, "unreadable")

    blank = tmp_path / "blank" / "SYSTEM.md"
    blank.parent.mkdir()
    blank.write_text(" \n\t", encoding="utf-8")
    with pytest.raises(ValueError, match="is blank"):
        load_system_prompt(tmp_path, "blank")


def test_hyper_prompt_matches_dual_planner_fsm_only_workflow() -> None:
    prompt = load_system_prompt(
        Path(__file__).parents[1] / "conf/system_prompt",
        "hyper-agent",
    )

    stages = (
        "Parse Mission Intent.",
        "Select and record the planner.",
        "Generate planner files.",
        "Submit and statically verify the files.",
        "Execute the planner.",
        "Generate the Statechart.",
        "Validate and repair the Statechart.",
        "Return accepted execution artifacts.",
    )
    assert [prompt.index(stage) for stage in stages] == sorted(
        prompt.index(stage) for stage in stages
    )
    assert "todo list with exactly these eight items in this order" in prompt
    assert "`execution_ready`" in prompt
    assert "planner-native plan" in prompt
    assert "Fast Downward" in prompt
    assert "VAL" in prompt
    assert "same submitted files" in prompt
    assert "`write_todos`" in prompt
    assert "environment, belief, and" in prompt
    assert "Never copy or transcribe the belief document" in prompt
    assert "compact DZN inspector" in prompt
    assert "read the generated DZN into model context" in prompt
    assert "checked-in preparation and inspection helpers" in prompt
    assert "absolute virtual paths for file tools" in prompt
    assert "repository-relative shell paths" in prompt
    assert "jq 'keys'" in prompt
    assert "jq '.static_info | length'" in prompt
    assert "code-owned candidate/DAG generator" in prompt
    assert "tool `reflection` arguments" in prompt
    assert "Statechart/FSM is the execution semantics" in prompt
    assert "world_model_info" in prompt
    assert "current-FoV" in prompt
    assert "Numeric\nvessel IDs are canonical" in prompt
    assert "complete future public report schedule" in prompt
    assert "event_report_checks" in prompt
    for supervisory_instruction in (
        "HyperHeartbeatInvocation",
        "HyperHeartbeatDecisionCandidate",
        "`no_change`",
        "`replan`",
        "`decline`",
    ):
        assert supervisory_instruction not in prompt
    for capability in (
        "mission-parsing",
        "planner-selection",
        "`record_planning_intent`",
        "creating-minizinc-problem-files",
        "`execute`",
        "creating-pddl-problem-files",
        "`write_file`",
        "`submit_planner_attempt`",
        "`planner_executor`",
        "`submit_statechart_draft`",
        "`HyperWorkflowResultCandidate`",
    ):
        assert capability in prompt
    for removed in (
        "load_planning_context",
        "PlannerChoiceRecord",
        "sha256",
        "digest",
        "authoring",
    ):
        assert removed not in prompt


def test_hyper_supervisor_prompt_is_heartbeat_only() -> None:
    prompt = load_system_prompt(
        Path(__file__).parents[1] / "conf/system_prompt",
        "hyper-supervisor",
    )

    assert "You are the Hyper Agent" in prompt
    assert "HyperHeartbeatInvocation" in prompt
    assert "HyperHeartbeatDecisionCandidate" in prompt
    for disposition in ("`no_change`", "`replan`", "`decline`"):
        assert disposition in prompt
    assert "world_model_info" in prompt
    assert "current-FoV" in prompt
    assert "Positive integer vessel IDs" in prompt
    for planning_instruction in (
        "todo list with exactly these eight items",
        "Generate planner files",
        "record_planning_intent",
        "write_file",
        "submit_planner_attempt",
        "planner_executor",
        "submit_statechart_draft",
        "HyperWorkflowResultCandidate",
    ):
        assert planning_instruction not in prompt


def test_maneuver_prompt_enforces_assess_first_single_snapshot_ordering() -> None:
    prompt = load_system_prompt(
        Path(__file__).parents[1] / "conf/system_prompt",
        "maneuver-control",
    )

    assert "ManeuverInvocation" in prompt
    assert "current\nenvironment data" in prompt
    assert "Future target-state\noperational context" in prompt
    assert "set_transition_target" in prompt
    assert "satisfied_with_uncertainty" in prompt
    assert "write_todos" in prompt
    assert "at\nmost one FSM transition" in prompt
    assert "assess the injected Transition Intent before considering another" in prompt
    assert "Do not assess or transition against that new target" in prompt
    assert "submitting no physical command" in prompt
    assert "Python supplies\n   the authoritative Mission and request identities" in prompt
    assert "ManeuverHeartbeatCompletion" in prompt
    assert "do not submit a hold" in prompt
    assert "Terminal lifecycle alone does not require replacement" in prompt
    assert "world_model_info" in prompt
    assert "current-FoV" in prompt
    assert "detected_issues" in prompt
    assert "positive integers" in prompt
    assert "Sensor-gated actual Events" in prompt
    assert "no_change Maneuver heartbeat requires no tool executions" not in prompt
