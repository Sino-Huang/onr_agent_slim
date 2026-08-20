import json
import sys
from pathlib import Path

import pytest

from onr.adapters.minizinc import MiniZincExecutor
from onr.contracts.planning import PlanningOutcome, TemporalAssignment

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
_PLANNER_ASSETS = {"model.mzn": b"model", "data.dzn": b"data"}


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
            _emit(
                {
                    "type": "solution",
                    "output": {"default": json.dumps(_SOLVED_PAYLOAD)},
                    "sections": ["default"],
                },
                {"type": "status", "status": "SATISFIED"},
            ),
            0.5,
            PlanningOutcome.INCOMPLETE,
            (),
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
    ids=(
        "optimal",
        "satisfied-not-optimal",
        "unsolvable",
        "incomplete",
        "timeout",
        "error",
    ),
)
def test_minizinc_executor_maps_json_stream_to_public_results(
    script: str,
    timeout_seconds: float,
    expected_outcome: PlanningOutcome,
    expected_assignments: tuple[TemporalAssignment, ...],
    tmp_path: Path,
) -> None:
    executor = MiniZincExecutor(
        executable=Path(sys.executable),
        artifact_root=tmp_path / "artifacts",
        arguments=("-c", script),
        timeout_seconds=timeout_seconds,
    )

    result = executor.execute(_PLANNER_ASSETS)

    assert result.outcome is expected_outcome
    assert result.assignments == expected_assignments
    assert result.evidence is not None
    assert (
        result.evidence.artifact_directory.parent == (tmp_path / "artifacts").resolve()
    )
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
        'print(\'{"type": "status", "status": "UNSATISFIABLE"}\')'
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


def test_minizinc_executor_static_check_uses_real_model_and_data_parser(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    minizinc = (
        repository_root
        / "modules"
        / "MiniZincIDE-2.9.7-bundle-linux-x86_64"
        / "bin"
        / "minizinc"
    )
    example = (
        repository_root
        / "conf"
        / "skills"
        / "hyper"
        / "creating-minizinc-problem-files"
        / "examples"
        / "risk-weighted-fov"
    )
    executor = MiniZincExecutor(minizinc, tmp_path / "artifacts")

    assert executor.check(
        {
            "model.mzn": (example / "model.mzn").read_bytes(),
            "data.dzn": (example / "data.dzn").read_bytes(),
        }
    )
    assert not executor.check(
        {
            "model.mzn": b"this is not MiniZinc;",
            "data.dzn": b"horizon = 3;",
        }
    )
