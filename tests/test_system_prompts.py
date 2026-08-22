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
        "Hand off execution.",
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
    assert "belief marginals" in prompt
    assert "tool `reflection` arguments" in prompt
    assert "Statechart/FSM is the execution semantics" in prompt
    for capability in (
        "mission-parsing",
        "planner-selection",
        "`record_planning_intent`",
        "creating-minizinc-problem-files",
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


def test_maneuver_prompt_uses_semantic_fsm_and_environment_before_transition() -> None:
    prompt = load_system_prompt(
        Path(__file__).parents[1] / "conf/system_prompt",
        "maneuver-control",
    )

    assert "ManeuverInvocation" in prompt
    assert "current environment data" in prompt
    assert "semantic state context" in prompt
    assert "environment_time_at_or_after" in prompt
    assert "ManeuverHeartbeatCompletion" in prompt
