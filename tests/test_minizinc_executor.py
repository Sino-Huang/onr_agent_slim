import json
import sys
from pathlib import Path

import pytest

from onr.adapters.minizinc import MiniZincExecutor
from onr.contracts.planning import PlanningOutcome

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
                    "output": {"default": "planner-native text"},
                    "sections": ["default"],
                },
                {"type": "status", "status": "OPTIMAL_SOLUTION"},
            ),
            0.5,
            PlanningOutcome.SOLVED,
            (),
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
            PlanningOutcome.SOLVED,
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
        "satisfied",
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
    expected_assignments: tuple[object, ...],
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


def test_minizinc_executor_returns_exact_failed_process_diagnostic(
    tmp_path: Path,
) -> None:
    script = (
        "import json,sys;"
        "print(json.dumps({'type':'error','location':{'filename':sys.argv[-1]},"
        "'message':'instance mismatch'}));"
        "print('planner stderr', file=sys.stderr);"
        "raise SystemExit(7)"
    )
    result = MiniZincExecutor(
        executable=Path(sys.executable),
        artifact_root=tmp_path / "artifacts",
        arguments=("-c", script),
    ).execute(_PLANNER_ASSETS)

    assert result.outcome is PlanningOutcome.ERROR
    assert result.return_code == 7
    assert result.stderr == "planner stderr\n"
    assert result.evidence is not None
    assert str(result.evidence.artifact_directory / "data.dzn") in result.stdout
    assert '"message": "instance mismatch"' in result.stdout
    assert result.evidence.stdout_path.read_text(encoding="utf-8") == result.stdout
    assert result.evidence.stderr_path.read_text(encoding="utf-8") == result.stderr


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

    accepted = executor.check(
        {
            "model.mzn": (example / "model.mzn").read_bytes(),
            "data.dzn": (example / "data.dzn").read_bytes(),
        }
    )
    rejected = executor.check(
        {
            "model.mzn": b"this is not MiniZinc;",
            "data.dzn": b"horizon = 3;",
        }
    )
    assert accepted.accepted is True
    assert accepted.return_code == 0
    assert rejected.accepted is False
    assert rejected.return_code == 1
    assert "syntax error" in rejected.error_message
    assert rejected.error_message == rejected.stderr.strip()


def test_event_information_patrol_example_chooses_stops_schedule_and_locations(
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

    assert executor.check(assets).accepted is True
    result = executor.execute(assets)

    assert result.outcome is PlanningOutcome.SOLVED
    assert result.assignments == ()
    stream = [json.loads(line) for line in result.stdout.splitlines()]
    solution = next(item for item in stream if item["type"] == "solution")
    assignments = json.loads(solution["output"]["default"])["assignments"]
    assert [item["maneuver_id"] for item in assignments] == [
        "patrol-stop-1",
        "patrol-stop-2",
    ]
    assert [item["start"] for item in assignments] == [10, 40]
    assert [item["duration"] for item in assignments] == [3, 6]
    assert [item["parameters"]["source_event_index"] for item in assignments] == [
        1,
        4,
    ]
    assert [
        (item["parameters"]["x"], item["parameters"]["y"]) for item in assignments
    ] == [(90, 0), (260, 80)]
    model_text = assets["model.mzn"].decode()
    data_text = assets["data.dzn"].decode()
    assert "array[STOP_SLOTS] of var bool: used" in model_text
    assert "information_gain * (event_count + 1) - used_stop_count" in model_text
    assert "global_cardinality" not in model_text
    assert "arg_sort" not in model_text
    assert "wait_start" not in model_text
    assert "stop_count" not in data_text
    assert "dwell_ticks" not in data_text
    assert "maneuver_id =" not in data_text
    assert "initialize_event_data_materialization" in data_text
    assert "materialize_event_information_data" in data_text
