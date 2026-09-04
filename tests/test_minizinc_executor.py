import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from onr.adapters.file_transport import FileTransport
from onr.adapters.minizinc import MiniZincExecutor
from onr.contracts.planning import PlanningOutcome
from onr.demo.fake_environment import FakeEnvironment
from onr.ports.transport import Subscription

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
            _emit(
                {
                    "type": "solution",
                    "output": {
                        "default": (
                            '{"assignments":[{"surveillance_mode":fixed_view"}]}'
                        )
                    },
                    "sections": ["default"],
                },
                {"type": "status", "status": "OPTIMAL_SOLUTION"},
            ),
            0.5,
            PlanningOutcome.ERROR,
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
        "malformed-json-native-plan",
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

    result = executor.execute(_PLANNER_ASSETS, "gecode")

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
    assert result.evidence.minizinc_solver == "gecode"


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
    ).execute({"model.mzn": b"model", "data.dzn": b"data"}, "highs")

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
    ).execute(_PLANNER_ASSETS, "coin-bc")

    assert result.outcome is PlanningOutcome.ERROR
    assert result.return_code == 7
    assert result.stderr == "planner stderr\n"
    assert result.evidence is not None
    assert str(result.evidence.artifact_directory / "data.dzn") in result.stdout
    assert '"message": "instance mismatch"' in result.stdout
    assert result.evidence.stdout_path.read_text(encoding="utf-8") == result.stdout
    assert result.evidence.stderr_path.read_text(encoding="utf-8") == result.stderr


def test_minizinc_executor_passes_only_the_validated_solver_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import onr.adapters.minizinc as module

    observed: list[str] = []
    observed_environment: dict[str, str] = {}

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(arguments)
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        observed_environment.update(cast(dict[str, str], environment))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout='{"type":"status","status":"UNSATISFIABLE"}\n',
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    executor = MiniZincExecutor(Path("/opt/minizinc"), tmp_path / "artifacts")

    result = executor.execute(_PLANNER_ASSETS, "highs")

    assert result.outcome is PlanningOutcome.UNSOLVABLE
    assert observed[:4] == [
        "/opt/minizinc",
        "--solver",
        "highs",
        "--json-stream",
    ]
    assert observed.count("--solver") == 1
    assert result.evidence is not None
    assert observed_environment["TMPDIR"] == str(result.evidence.artifact_directory)
    with pytest.raises(ValueError, match="unsupported MiniZinc solver"):
        executor.execute(_PLANNER_ASSETS, cast(Any, "highs --output-to-file plan"))


def test_minizinc_executor_static_check_uses_real_model_and_data_parser(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    minizinc = (
        repository_root
        / "modules"
        / "MiniZincIDE-2.10.1-appimage"
        / "usr"
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
        / "MiniZincIDE-2.10.1-appimage"
        / "usr"
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
    result = executor.execute(assets, "coin-bc")

    assert result.outcome is PlanningOutcome.SOLVED
    assert result.assignments == ()
    stream = [json.loads(line) for line in result.stdout.splitlines()]
    solution = next(item for item in stream if item["type"] == "solution")
    native = json.loads(solution["output"]["default"])
    assignments = native["assignments"]
    assert native["combined_score"] == 1_728_914
    assert native["maneuver_count"] == len(assignments) == 2
    assert assignments[0]["surveillance_mode"] == "fixed_view"
    assert assignments[0]["entity_id"] is None
    assert assignments[0]["parameters"]["report_ids"] == ["report-checked"]
    assert assignments[1]["surveillance_mode"] == "pursue_ship"
    assert assignments[1]["entity_id"] == 7
    assert assignments[1]["parameters"]["report_ids"] == [
        "report-future-a",
        "report-future-b",
    ]
    assert assignments[1]["parameters"]["target_posterior_risk"] == 132_918
    assert assignments[1]["parameters"]["public_report_rate"] == 222_222
    assert assignments[1]["observation_window"] == {
        "start": 24,
        "duration": 5,
        "time_scale": 2,
    }
    model_text = assets["model.mzn"].decode()
    data_text = assets["data.dzn"].decode()
    assert 'include "network_flow.mzn"' not in model_text
    assert "network_flow_cost" not in model_text
    assert "outgoing_start[node]..outgoing_start[node + 1] - 1" in model_text
    assert "incoming_start[node]..incoming_start[node + 1] - 1" in model_text
    assert "flow[incoming_edge[position]]" in model_text
    assert "array[CANDIDATES] of var 0.0..1.0: selected" in model_text
    assert "candidate_combined_score[candidate] * selected[candidate]" in model_text
    assert "candidate_recall[candidate]" in model_text
    assert "+ candidate_estimation[candidate]" in model_text
    assert "+ candidate_omission[candidate]" in model_text
    assert "candidate_score" not in data_text
    assert "candidate_count = 4;" in data_text
    assert "arc_count = 7;" in data_text
    assert result.evidence is not None
    assert result.evidence.minizinc_solver == "coin-bc"
    assert '"status": "OPTIMAL_SOLUTION"' in result.stdout


def _dzn_int(text: str, name: str) -> int:
    match = re.search(rf"^{name} = (-?\d+);$", text, re.MULTILINE)
    assert match is not None
    return int(match.group(1))


def _dzn_array(text: str, name: str) -> list[int]:
    match = re.search(rf"^{name} = \[([^\n]*)\];$", text, re.MULTILINE)
    assert match is not None
    return [int(value) for value in match.group(1).split(", ")]


def test_event_information_generator_manifest_and_dzn_structure(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    example = (
        repository_root
        / "conf/skills/hyper/creating-minizinc-problem-files/examples"
        / "event-information-patrol"
    )
    mission_id = "mission:demo"
    transport = FileTransport(
        tmp_path / "transport",
        (
            FakeEnvironment.subscription_for(mission_id),
            Subscription("scene-reader", mission_id, "environment-data"),
            Subscription("context-coordination", mission_id, "normalized-plans"),
        ),
    )
    environment = (
        FakeEnvironment(transport, mission_id, output_root=tmp_path / "environment")
        .heartbeat()
        .environment_file
    )
    generated = tmp_path / "data.dzn"

    completed = subprocess.run(
        [
            sys.executable,
            str(example / "generate_data.py"),
            str(environment),
            str(generated),
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    manifest = json.loads(completed.stdout)
    assert set(manifest) == {
        "candidates",
        "candidate_counts_by_mode",
        "pursuit_ship_ids",
        "pursuit_risk_rate_inputs",
        "arcs",
        "advisory_score",
        "advisory_modes",
        "advisory_maneuvers",
        "advisory_duration_s",
        "component_score_consistent",
        "advisory_utility",
        "covered_report_count",
        "covered_report_ids",
    }
    assert manifest["candidates"] > 0
    assert manifest["covered_report_count"] == len(manifest["covered_report_ids"])
    assert len(manifest["covered_report_ids"]) == len(
        set(manifest["covered_report_ids"])
    )
    data = generated.read_text(encoding="utf-8")
    action_count = _dzn_int(data, "candidate_count")
    arc_count = _dzn_int(data, "arc_count")
    node_count = _dzn_int(data, "node_count")
    source = _dzn_int(data, "source_node")
    sink = _dzn_int(data, "sink_node")
    action_arrays = [
        _dzn_array(data, name)
        for name in (
            "candidate_x",
            "candidate_y",
            "candidate_start",
            "candidate_duration",
            "candidate_recall",
            "candidate_estimation",
            "candidate_omission",
            "candidate_target_risk",
            "candidate_omission_probability",
            "candidate_public_report_rate",
            "candidate_report_span",
        )
    ]
    arc_from = _dzn_array(data, "arc_from")
    arc_to = _dzn_array(data, "arc_to")
    outgoing_start = _dzn_array(data, "outgoing_start")
    incoming_start = _dzn_array(data, "incoming_start")
    incoming_edge = _dzn_array(data, "incoming_edge")
    assert all(len(values) == action_count for values in action_arrays)
    assert len(arc_from) == len(arc_to) == arc_count
    assert len(outgoing_start) == len(incoming_start) == node_count + 1
    assert outgoing_start[0] == incoming_start[0] == 1
    assert outgoing_start[-1] == incoming_start[-1] == arc_count + 1
    assert sorted(incoming_edge) == list(range(1, arc_count + 1))
    for node in range(1, node_count + 1):
        assert all(
            arc_from[edge - 1] == node
            for edge in range(outgoing_start[node - 1], outgoing_start[node])
        )
        assert all(
            arc_to[incoming_edge[position - 1] - 1] == node
            for position in range(incoming_start[node - 1], incoming_start[node])
        )
    assert all(start < end for start, end in zip(arc_from, arc_to, strict=True))
    reachable = {source}
    for node in range(source, node_count + 1):
        if node in reachable:
            reachable.update(
                end
                for start, end in zip(arc_from, arc_to, strict=True)
                if start == node
            )
    assert sink in reachable

    assert action_count == manifest["candidates"]


def test_mission1_path_helpers_inspect_and_prepare_replan_inputs(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    example = (
        repository_root
        / "conf/skills/hyper/creating-minizinc-problem-files/examples"
        / "event-information-patrol"
    )
    environment = example / "replan-environment.json"
    belief = example / "replan-belief.json"

    inspected = subprocess.run(
        [sys.executable, str(example / "inspect_inputs.py"), environment, belief],
        capture_output=True,
        check=True,
        text=True,
    )
    summary = json.loads(inspected.stdout)
    risk_rate_inputs = summary.pop("risk_rate_inputs")
    assert summary == {
        "belief_input_revision": 16,
        "belief_kind": "reporting_reliability",
        "belief_revision": 2,
        "belief_ship_ids": [1, 7, 8],
        "mission_id": "mission-1",
        "mission_time_seconds": 8.0,
        "public_report_count": 6,
        "report_check_count": 3,
    }
    assert [item["entity_id"] for item in risk_rate_inputs] == [1, 7, 8]
    assert [item["public_report_rate"] for item in risk_rate_inputs] == [
        0.0,
        pytest.approx(2.0 / 9.0),
        pytest.approx(1.0 / 6.0),
    ]
    assert risk_rate_inputs[1]["target_posterior_risk"] == pytest.approx(
        0.6231017419834954
    )

    model = tmp_path / "arbitrary revision/model.mzn"
    data = tmp_path / "arbitrary revision/data.dzn"
    prepared = subprocess.run(
        [
            sys.executable,
            str(example / "prepare_problem.py"),
            environment,
            belief,
            model,
            data,
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    assert json.loads(prepared.stdout)["covered_report_ids"] == [
        "report-future-a",
        "report-future-b",
    ]
    assert model.read_bytes() == (example / "model.mzn").read_bytes()
    assert data.read_bytes() == (example / "replan-data.dzn").read_bytes()

    inspected_problem = subprocess.run(
        [sys.executable, str(example / "inspect_problem.py"), data],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(inspected_problem.stdout) == {
        "advisory_modes": ["pursue_ship"],
        "arc_count": 6,
        "candidate_arrays_aligned": True,
        "candidate_count": 3,
        "candidate_counts_by_mode": {"fixed_view": 2, "pursue_ship": 1},
        "component_score_consistent": True,
        "forward_arcs": True,
        "incoming_index_valid": True,
        "node_count": 5,
        "outgoing_index_valid": True,
        "pursuit_risk_rate_inputs": [
            {
                "entity_id": 7,
                "expected_omission_probability": 207711,
                "public_report_rate": 222222,
                "score_scale": 1000000,
                "target_posterior_risk": 623102,
            }
        ],
        "pursuit_ship_ids": [7],
        "report_arrays_aligned": True,
        "report_id_count": 5,
        "source_to_sink": True,
        "valid": True,
    }

    broken = tmp_path / "broken.dzn"
    broken.write_text(
        data.read_text(encoding="utf-8").replace(
            "outgoing_start = [1, 4, 5, 6, 7, 7];",
            "outgoing_start = [1, 4, 5, 6, 6, 7];",
        ),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [sys.executable, str(example / "inspect_problem.py"), broken],
        capture_output=True,
        check=False,
        text=True,
    )
    assert rejected.returncode != 0
    assert "outgoing index does not match arc_from" in rejected.stderr


def test_full_physical_vehicle_patrol_solves_within_executor_limit(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    example = (
        repository_root
        / "conf/skills/hyper/creating-minizinc-problem-files/examples"
        / "event-information-patrol"
    )
    report = json.loads(
        (
            repository_root
            / "data/ships_report_and_trajectory_example/ships/events_report.json"
        ).read_text(encoding="utf-8")
    )
    environment = tmp_path / "environment.json"
    environment.write_text(
        json.dumps(
            {
                "static_info": report,
                "scene_graph": {
                    "mission_time_seconds": 0.0,
                    "entities": [
                        {
                            "type": "drone",
                            "location": {"x": 0.0, "y": 0.0},
                            "max_velocity": 30.0,
                            "fov_radius": 100.0,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    generated = tmp_path / "data.dzn"
    subprocess.run(
        [sys.executable, str(example / "generate_data.py"), environment, generated],
        capture_output=True,
        check=True,
        text=True,
    )
    executor = MiniZincExecutor(
        repository_root / "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc",
        tmp_path / "planner-artifacts",
        timeout_seconds=30,
    )
    assets = {
        "model.mzn": (example / "model.mzn").read_bytes(),
        "data.dzn": generated.read_bytes(),
    }

    assert executor.check(assets).accepted is True
    result = executor.execute(assets, "coin-bc")

    assert result.outcome is PlanningOutcome.SOLVED
    stream = [json.loads(line) for line in result.stdout.splitlines()]
    solution = next(item for item in stream if item["type"] == "solution")
    native = json.loads(solution["output"]["default"])
    assert native["combined_score"] > 0
    assert native["maneuver_count"] == len(native["assignments"])
    assert all(
        item["surveillance_mode"] in {"fixed_view", "pursue_ship"}
        for item in native["assignments"]
    )
