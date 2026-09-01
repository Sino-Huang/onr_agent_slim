import json
import re
import runpy
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

    def run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        observed.extend(arguments)
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
    with pytest.raises(ValueError, match="unsupported MiniZinc solver"):
        executor.execute(_PLANNER_ASSETS, cast(Any, "highs --output-to-file plan"))


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
    result = executor.execute(assets, "coin-bc")

    assert result.outcome is PlanningOutcome.SOLVED
    assert result.assignments == ()
    stream = [json.loads(line) for line in result.stdout.splitlines()]
    solution = next(item for item in stream if item["type"] == "solution")
    native = json.loads(solution["output"]["default"])
    assignments = native["assignments"]
    assert native["information_gain"] == 15_221
    assert native["stop_count"] == len(assignments) == 15
    assert [item["start"] for item in assignments] == [
        42,
        71,
        113,
        162,
        205,
        246,
        278,
        329,
        353,
        438,
        494,
        528,
        554,
        591,
        598,
    ]
    assert all(item["parameters"]["time_scale"] == 2 for item in assignments)
    model_text = assets["model.mzn"].decode()
    data_text = assets["data.dzn"].decode()
    assert 'include "network_flow.mzn"' not in model_text
    assert "network_flow_cost" not in model_text
    assert "outgoing_start[node]..outgoing_start[node + 1] - 1" in model_text
    assert "incoming_start[node]..incoming_start[node + 1] - 1" in model_text
    assert "flow[incoming_edge[position]]" in model_text
    assert "array[ACTIONS] of var 0..1: selected" not in model_text
    assert "action_gain[arc_to[edge] - 1] * flow[edge]" in model_text
    assert "action_count = 786;" in data_text
    assert "arc_count = 14423;" in data_text
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
    assert manifest == {
        "actions": 786,
        "full_arcs": 127_455,
        "intersections": 105,
        "longest_route": 15,
        "optimum_gain": 15_221,
        "optimum_stops": 15,
        "planning_fov_radius_m": 30.0,
        "planning_max_velocity_mps": 20.0,
        "raw_actions": 895,
        "reduced_arcs": 14_423,
        "source_events": 253,
    }
    data = generated.read_text(encoding="utf-8")
    action_count = _dzn_int(data, "action_count")
    arc_count = _dzn_int(data, "arc_count")
    node_count = _dzn_int(data, "node_count")
    source = _dzn_int(data, "source_node")
    sink = _dzn_int(data, "sink_node")
    action_arrays = [
        _dzn_array(data, name)
        for name in (
            "action_x",
            "action_y",
            "action_start",
            "action_end",
            "action_gain",
            "action_anchor_event",
            "action_capture_count",
        )
    ]
    arc_from = _dzn_array(data, "arc_from")
    arc_to = _dzn_array(data, "arc_to")
    arc_cost = _dzn_array(data, "arc_cost")
    outgoing_start = _dzn_array(data, "outgoing_start")
    incoming_start = _dzn_array(data, "incoming_start")
    incoming_edge = _dzn_array(data, "incoming_edge")
    assert all(len(values) == action_count for values in action_arrays)
    assert len(arc_from) == len(arc_to) == len(arc_cost) == arc_count
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
    assert _dzn_array(data, "node_balance") == [1] + [0] * action_count + [-1]
    assert (source, sink) in set(zip(arc_from, arc_to, strict=True))
    reachable = {source}
    for node in range(source, node_count + 1):
        if node in reachable:
            reachable.update(
                end
                for start, end in zip(arc_from, arc_to, strict=True)
                if start == node
            )
    assert sink in reachable

    namespace = runpy.run_path(str(example / "generate_data.py"))
    document = json.loads(environment.read_text(encoding="utf-8"))
    risks = namespace["extract_risk_by_entity"](namespace["BELIEF_MARGINALS"])
    events = namespace["event_rows"](document, risks)
    candidates = namespace["intersection_candidates"](document, events)
    actions, _ = namespace["observation_actions"](candidates, events, 30)
    assert (
        len(
            {
                (action.x, action.y, action.start, action.end, action.captured)
                for action in actions
            }
        )
        == action_count
    )

    drone = next(
        entity
        for entity in document["scene_graph"]["entities"]
        if entity["type"] == "drone"
    )
    drone["fov_radius"] = 100.0
    drone["max_velocity"] = 30.0
    _, full_vehicle_manifest = namespace["build_instance"](document)
    assert full_vehicle_manifest == {
        "actions": 3_089,
        "full_arcs": 1_661_195,
        "intersections": 105,
        "longest_route": 16,
        "optimum_gain": 21_572,
        "optimum_stops": 16,
        "planning_fov_radius_m": 100.0,
        "planning_max_velocity_mps": 30.0,
        "raw_actions": 3_349,
        "reduced_arcs": 145_400,
        "source_events": 253,
    }


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
        repository_root / "modules/MiniZincIDE-2.9.7-bundle-linux-x86_64/bin/minizinc",
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
    assert native["information_gain"] == 21_572
    assert native["stop_count"] == len(native["assignments"]) == 16
