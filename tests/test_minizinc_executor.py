import json
import sys
from pathlib import Path

import pytest

from onr.adapters.minizinc import MiniZincExecutor
from onr.contracts.planning import (
    ManeuverParameter,
    PlanningOutcome,
    TemporalAssignment,
)

_SOLVED_PAYLOAD = {
    "assignments": [
        {
            "maneuver_id": "survey",
            "start": 0,
            "duration": 4,
            "parameters": {"x": 120, "y": -45},
        },
        {"maneuver_id": "return-to-base", "start": 4, "duration": 2},
    ]
}
_EXPECTED_ASSIGNMENTS = (
    TemporalAssignment(
        maneuver_id="survey",
        start=0,
        duration=4,
        parameters=(
            ManeuverParameter("x", 120),
            ManeuverParameter("y", -45),
        ),
    ),
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


def test_event_information_patrol_example_is_optimal_and_preserves_waypoints(
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
        / "event-information-patrol"
    )
    assets = {
        "model.mzn": (example / "model.mzn").read_bytes(),
        "data.dzn": (example / "data.dzn").read_bytes(),
    }
    executor = MiniZincExecutor(
        minizinc, tmp_path / "patrol-artifacts", timeout_seconds=30
    )

    assert executor.check(assets)
    result = executor.execute(assets)

    assert result.outcome is PlanningOutcome.SOLVED
    assert [
        (
            item.maneuver_id,
            item.start,
            item.duration,
            {parameter.name: parameter.value for parameter in item.parameters},
        )
        for item in result.assignments
    ] == [
        (
            "patrol-stop-1",
            209,
            2,
            {
                "move_duration": 144,
                "move_from_x": 46,
                "move_from_y": -86,
                "move_start": 65,
                "source_event_index": 37,
                "time_scale": 2,
                "wait_duration": 65,
                "wait_start": 0,
                "x": -484,
                "y": -1415,
            },
        ),
        (
            "patrol-stop-2",
            277,
            2,
            {
                "move_duration": 35,
                "move_from_x": -484,
                "move_from_y": -1415,
                "move_start": 242,
                "source_event_index": 63,
                "time_scale": 2,
                "wait_duration": 31,
                "wait_start": 211,
                "x": -626,
                "y": -1725,
            },
        ),
        (
            "patrol-stop-3",
            498,
            2,
            {
                "move_duration": 135,
                "move_from_x": -626,
                "move_from_y": -1725,
                "move_start": 363,
                "source_event_index": 113,
                "time_scale": 2,
                "wait_duration": 84,
                "wait_start": 279,
                "x": -1411,
                "y": -629,
            },
        ),
        (
            "patrol-stop-4",
            598,
            2,
            {
                "move_duration": 36,
                "move_from_x": -1411,
                "move_from_y": -629,
                "move_start": 562,
                "source_event_index": 230,
                "time_scale": 2,
                "wait_duration": 62,
                "wait_start": 500,
                "x": -1619,
                "y": -347,
            },
        ),
    ]
