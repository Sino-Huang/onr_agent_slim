from pathlib import Path

import pytest

from onr.adapters.fast_downward import FastDownwardExecutor
from onr.application.symbolic_planning import SymbolicPlanning
from onr.contracts.planning import (
    ManeuverIntent,
    PlannerChoice,
    PlanningOutcome,
    SymbolicManeuver,
    SymbolicMissionSpec,
)


def test_real_symbolic_planning_emits_ordered_costed_actions_without_timing() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    fast_downward = repository_root / "modules" / "downward" / "fast-downward.py"
    downward = (
        repository_root
        / "modules"
        / "downward"
        / "builds"
        / "release"
        / "bin"
        / "downward"
    )
    missing = [path for path in (fast_downward, downward) if not path.is_file()]
    if missing:
        pytest.skip(
            "bundled Fast Downward driver or release build unavailable: "
            + ", ".join(map(str, missing))
        )

    mission_spec = SymbolicMissionSpec(
        mission_id="mission-real-symbolic-1",
        objective="Survey, inspect, and return",
        planner_choice=PlannerChoice(
            planning_profile="symbolic",
            planner_id="fast-downward",
        ),
        maneuvers=(
            SymbolicManeuver(
                maneuver_id="return-to-base",
                intent=ManeuverIntent(action="return-to-base"),
                dependencies=("inspect",),
                cost=1,
            ),
            SymbolicManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent(action="survey"),
                dependencies=(),
                cost=2,
            ),
            SymbolicManeuver(
                maneuver_id="inspect",
                intent=ManeuverIntent(action="inspect"),
                dependencies=("survey",),
                cost=3,
            ),
        ),
        source_authority="mission-control",
        domain_revision=1,
    )
    result = SymbolicPlanning(
        executor=FastDownwardExecutor(
            executable=fast_downward,
            timeout_seconds=10,
        )
    ).plan(
        mission_spec=mission_spec,
        plan_revision=5,
        mission_snapshot_id="snapshot-real-symbolic-1",
    )

    assert result.outcome is PlanningOutcome.SOLVED
    assert tuple(
        (step.step_index, step.maneuver_id, step.cost)
        for step in result.normalized_plan.symbolic_steps
    ) == (
        (0, "survey", 2),
        (1, "inspect", 3),
        (2, "return-to-base", 1),
    )
    document = result.normalized_plan.to_canonical_json()
    assert '"start"' not in document
    assert '"duration"' not in document
