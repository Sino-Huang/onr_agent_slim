import json
import sys
from pathlib import Path

import pytest

from onr.adapters.minizinc import MiniZincExecutor
from onr.application.minizinc import translate_minizinc
from onr.contracts.planning import (
    ManeuverIntent,
    MissionSpec,
    PlannerChoice,
    PlanningOutcome,
    TemporalAssignment,
    TemporalManeuver,
)


_SOLVED_PAYLOAD = {
    "assignments": [
        {"maneuver_id": "survey", "start": 0, "duration": 4},
        {"maneuver_id": "return-to-base", "start": 4, "duration": 2},
    ]
}
_EXPECTED_ASSIGNMENTS = (
    TemporalAssignment(maneuver_id="survey", start=0, duration=4),
    TemporalAssignment(maneuver_id="return-to-base", start=4, duration=2),
)


def _emit(*events: object) -> str:
    statements = [f"print({json.dumps(json.dumps(event))})" for event in events]
    return ";".join(statements)


@pytest.mark.parametrize(
    ("script", "timeout_seconds", "expected_outcome", "expected_assignments"),
    (
        (
            _emit(
                {
                    "type": "solution",
                    "output": {"default": json.dumps(_SOLVED_PAYLOAD)},
                    "sections": ["default"],
                },
                {"type": "status", "status": "OPTIMAL_SOLUTION"},
            ),
            0.5,
            PlanningOutcome.SOLVED,
            _EXPECTED_ASSIGNMENTS,
        ),
        (
            _emit({"type": "status", "status": "UNSATISFIABLE"}),
            0.5,
            PlanningOutcome.UNSOLVABLE,
            (),
        ),
        (
            _emit(
                {
                    "type": "solution",
                    "output": {"default": json.dumps(_SOLVED_PAYLOAD)},
                    "sections": ["default"],
                }
            ),
            0.5,
            PlanningOutcome.INCOMPLETE,
            (),
        ),
        (
            "import time;time.sleep(1)",
            0.05,
            PlanningOutcome.TIMEOUT,
            (),
        ),
        (
            "print('not-json');raise SystemExit(2)",
            0.5,
            PlanningOutcome.ERROR,
            (),
        ),
    ),
    ids=("solved", "unsolvable", "incomplete", "timeout", "error"),
)
def test_minizinc_executor_maps_json_stream_to_public_results(
    script: str,
    timeout_seconds: float,
    expected_outcome: PlanningOutcome,
    expected_assignments: tuple[TemporalAssignment, ...],
    tmp_path: Path,
) -> None:
    planner_choice = PlannerChoice(
        planning_profile="temporal",
        planner_id="minizinc",
    )
    mission_spec = MissionSpec(
        mission_id="mission-1",
        objective="Survey the operating area and return",
        planner_choice=planner_choice,
        maneuvers=(
            TemporalManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent(action="survey"),
                dependencies=(),
                duration=4,
            ),
            TemporalManeuver(
                maneuver_id="return-to-base",
                intent=ManeuverIntent(action="return-to-base"),
                dependencies=("survey",),
                duration=2,
            ),
        ),
        horizon=10,
        source_authority="mission-control",
    )
    executor = MiniZincExecutor(
        executable=Path(sys.executable),
        artifact_root=tmp_path / "artifacts",
        arguments=("-c", script),
        timeout_seconds=timeout_seconds,
    )

    result = executor.execute(translate_minizinc(mission_spec))

    assert result.outcome is expected_outcome
    assert result.assignments == expected_assignments
    assert result.evidence is not None
    assert result.evidence.artifact_directory.parent == (tmp_path / "artifacts").resolve()
    assert {path.name for path in result.evidence.artifact_paths} == {
        "model.mzn",
        "data.dzn",
    }
    assert result.evidence.stdout_path.exists()
    assert result.evidence.stderr_path.exists()


def test_minizinc_executor_persists_relative_solver_artifacts_in_run_directory(
    tmp_path: Path,
) -> None:
    script = (
        "from pathlib import Path;"
        "Path('relative-solver-artifact.txt').write_text('artifact', encoding='utf-8');"
        "print('{\"type\": \"status\", \"status\": \"UNSATISFIABLE\"}')"
    )
    result = MiniZincExecutor(
        executable=Path(sys.executable),
        artifact_root=tmp_path / "artifacts",
        arguments=("-c", script),
    ).execute({"model.mzn": b"model", "data.dzn": b"data"})

    assert result.outcome is PlanningOutcome.UNSOLVABLE
    assert result.evidence is not None
    artifact = result.evidence.artifact_directory / "relative-solver-artifact.txt"
    assert artifact.read_text(encoding="utf-8") == "artifact"
    assert artifact in result.evidence.artifact_paths
