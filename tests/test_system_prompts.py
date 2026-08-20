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


def test_hyper_prompt_matches_the_current_minizinc_workflow() -> None:
    prompt = load_system_prompt(
        Path(__file__).parents[1] / "conf/system_prompt",
        "hyper-agent",
    )

    stages = (
        "Parse Mission Intent into PlanningIntent.",
        "Decide and record the MiniZinc planner inside PlanningIntent.",
        "Load the current snapshot-authorized operational evidence.",
        "Generate and persist MiniZinc problem files.",
        "Run MiniZinc and repair rejected translations.",
    )
    assert [prompt.index(stage) for stage in stages] == sorted(
        prompt.index(stage) for stage in stages
    )
    assert "Call `write_todos` immediately" in prompt
    assert "Never batch several completions" in prompt
    assert "`outcome: plan_ready`" in prompt
    assert "verified NormalizedPlan" in prompt
    assert "`environment_data.static_info`" in prompt
    assert "`environment_data.scene_graph`" in prompt
    assert "`belief_snapshot`" in prompt
    for capability in (
        "mission-parsing",
        "planner-selection",
        "`record_planning_intent`",
        "creating-minizinc-problem-files",
        "`load_planning_context`",
        "`persist_planner_assets`",
        "`planner_executor`",
        "`HyperWorkflowResultCandidate`",
    ):
        assert capability in prompt


def test_maneuver_prompt_uses_generated_plan_before_transition() -> None:
    prompt = load_system_prompt(
        Path(__file__).parents[1] / "conf/system_prompt",
        "maneuver-control",
    )

    assert "invocation overlay" in prompt
    assert "`normalized_plan`" in prompt
    assert "select that exact maneuver" in prompt
    assert (
        "Do not advance a maneuver before authoritative completion feedback" in prompt
    )
