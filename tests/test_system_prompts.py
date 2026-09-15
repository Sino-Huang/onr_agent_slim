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
    assert "code-owned workflow gate" in prompt
    assert "write_todos" not in prompt
    assert "`execution_ready`" in prompt
    assert "planner-native plan" in prompt
    assert "Fast Downward" in prompt
    assert "VAL" in prompt
    assert "same submitted files" in prompt
    assert "environment, belief, and" in prompt
    assert "Never copy or transcribe the belief document" in prompt
    assert "compact DZN inspector" in prompt
    assert "read the generated DZN into model context" in prompt
    assert "checked-in preparation and inspection helpers" in prompt
    assert "absolute virtual paths for file tools" in prompt
    assert "repository-relative shell paths" in prompt
    assert "jq 'keys'" in prompt
    assert "exact event" in prompt
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
    assert "same stale\nrendezvous" in prompt
    assert "recorded\n`no_change` decision still completes" in prompt
    assert "at most 150 words of private" in prompt
    assert "newer GPS fix can support Maneuver's bounded" in prompt
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
    prompt = " ".join(
        load_system_prompt(
            Path(__file__).parents[1] / "conf/system_prompt",
            "maneuver-control",
        ).split()
    )

    assert "ManeuverInvocation" in prompt
    assert "Current FSM, environment, active-action, and Hyper outcome facts" in prompt
    assert "A successful transition reveals the next state's context" in prompt
    assert "set_transition_target" in prompt
    assert "satisfied_with_uncertainty" in prompt
    assert "write_todos" not in prompt
    assert "at most one FSM transition" in prompt
    assert "assess the injected intent first" in prompt
    assert "Do not assess that new target now" in prompt
    assert "Preserve a suitable nonterminal active action" in prompt
    assert "Python supplies the heartbeat's Mission/request identities" in prompt
    assert "ManeuverHeartbeatResponse" in prompt
    assert "Do not submit hold/repeat navigation" in prompt
    assert "world_model_info" in prompt
    assert "FoV evidence" in prompt
    assert "detected_issues" in prompt
    assert "positive integers" in prompt
    assert "pending Event Observation" in prompt
    assert "at most 200 words of private deliberation" in prompt
    assert "no_change Maneuver heartbeat requires no tool executions" not in prompt


def test_maneuver_prompt_bounds_waiting_for_unconfirmed_reports() -> None:
    prompt = " ".join(
        load_system_prompt(
            Path(__file__).parents[1] / "conf/system_prompt", "maneuver-control"
        ).split()
    )
    assert "any exact time/window bound is future, retain it" in prompt
    assert "required coverage was achieved" in prompt
    assert "name the missing IDs and visibility limits" in prompt
    assert "Wait beyond a window only" in prompt
    assert "mandatory verification" in prompt
    assert "bounded publication delay" in prompt
    assert "derived_transition_facts" in prompt
    assert "It is not an assessment" in prompt
    assert "wall time and sensing uncertainty cannot satisfy it" in prompt
