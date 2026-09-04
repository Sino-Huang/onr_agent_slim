"""Evaluate Mission 1 prior and altered-evidence planning across a corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from onr.adapters.minizinc import MiniZincExecutor
from onr.application.mission1_planning import (
    SCORE_SCALE,
    TIME_SCALE,
    build_candidate_dag,
    longest_path_oracle,
    serialize_minizinc_data,
)
from onr.application.reporting_reliability import ReportingReliabilityManager
from onr.contracts.planning import PlanningOutcome
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot

_PRIVATE_REPORT_FIELDS = frozenset({"source_event_index"})


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _public_reports(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("events_report.json must contain an array")
    reports: list[dict[str, object]] = []
    for order, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError("every public report must be an object")
        public = {
            str(key): item
            for key, item in raw.items()
            if key not in _PRIVATE_REPORT_FIELDS
        }
        if "report_id" not in public:
            encoded = json.dumps(
                {"public_order": order, "report": public},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            public["report_id"] = f"report-{hashlib.sha256(encoded).hexdigest()[:24]}"
        reports.append(public)
    return reports


def _environment(
    mission_id: str,
    reports: list[dict[str, object]],
    *,
    mission_time_s: float,
    position: Sequence[object],
    checks: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "mission_id": mission_id,
        "mission_time_seconds": mission_time_s,
        "state_version": round(mission_time_s * 2),
        "controlled_vehicle": {
            "entity_id": "evaluation-drone",
            "position": {
                "x": float(cast(Any, position[0])),
                "y": float(cast(Any, position[1])),
                "z": -25.0,
            },
            "max_velocity": 30.0,
            "fov_radius": 100.0,
        },
        "world_model_info": {"event_report_checks": list(checks)},
        "static_info": reports,
    }


def _counterfactual_check(
    reports: Sequence[Mapping[str, object]],
) -> tuple[int, dict[str, object]]:
    schedules: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for report in reports:
        entity_id = report.get("entity_id")
        if isinstance(entity_id, int) and not isinstance(entity_id, bool):
            schedules[entity_id].append(report)
    eligible = []
    for entity_id, schedule in schedules.items():
        times = [float(cast(Any, report["time"])) for report in schedule]
        if len(set(times)) < 2:
            continue
        rate = (len(times) - 1) / (max(times) - min(times))
        eligible.append((rate, len(schedule), -entity_id, entity_id))
    if not eligible:
        raise ValueError("counterfactual evidence requires a ship report interval")
    _, _, _, entity_id = max(eligible)
    check = {
        "check_id": f"counterfactual-altered-history-{entity_id}",
        "report_id": f"counterfactual-history-{entity_id}",
        "entity_id": entity_id,
        "event_time_s": -0.5,
        "checked_at_s": 0.0,
        "outcome": "altered",
    }
    return entity_id, check


def _native_solution(stdout: str) -> tuple[dict[str, object], str | None]:
    solution: dict[str, object] | None = None
    status = None
    for line in stdout.splitlines():
        event = json.loads(line)
        if event.get("type") == "solution":
            output = event.get("output")
            default = output.get("default") if isinstance(output, Mapping) else None
            if isinstance(default, str):
                decoded = json.loads(default)
                if isinstance(decoded, dict):
                    solution = decoded
        elif event.get("type") == "status":
            status = event.get("status")
    if solution is None:
        raise ValueError("MiniZinc produced no native Mission 1 solution")
    return solution, status if isinstance(status, str) else None


def _evaluate_snapshot(
    *,
    output: Path,
    kind: str,
    environment: dict[str, object],
    belief: ReportingReliabilitySnapshot,
    model: bytes,
    executor: MiniZincExecutor,
) -> dict[str, object]:
    started = time.perf_counter()
    graph = build_candidate_dag(environment, belief)
    oracle = longest_path_oracle(graph)
    data = serialize_minizinc_data(graph).encode()
    generation_seconds = time.perf_counter() - started

    snapshot = output / kind
    _write_json(snapshot / "environment.json", environment)
    _write_json(snapshot / "belief.json", belief.to_dict())
    (snapshot / "model.mzn").write_bytes(model)
    (snapshot / "data.dzn").write_bytes(data)
    assets = {"model.mzn": model, "data.dzn": data}

    started = time.perf_counter()
    check = executor.check(assets)
    check_seconds = time.perf_counter() - started
    if not check.accepted:
        raise RuntimeError(
            f"{output.name}/{kind} failed MiniZinc validation: {check.stderr}"
        )
    started = time.perf_counter()
    result = executor.execute(assets, "coin-bc")
    solver_seconds = time.perf_counter() - started
    if result.outcome is not PlanningOutcome.SOLVED:
        raise RuntimeError(
            f"{output.name}/{kind} MiniZinc outcome was {result.outcome}: "
            f"{result.stderr}"
        )
    native, status = _native_solution(result.stdout)
    assignments = native.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError("MiniZinc native assignments are missing")
    oracle_ids = [candidate.candidate_id for candidate in oracle.candidates]
    solver_ids = [
        assignment.get("candidate_id")
        for assignment in assignments
        if isinstance(assignment, Mapping)
    ]
    by_id = {candidate.candidate_id: candidate for candidate in graph.candidates}
    if len(solver_ids) != len(set(solver_ids)) or any(
        candidate_id not in by_id for candidate_id in solver_ids
    ):
        raise ValueError(f"{output.name}/{kind} returned an invalid candidate route")
    selected = [by_id[str(candidate_id)] for candidate_id in solver_ids]
    solver_modes = [candidate.mode for candidate in selected]
    solver_entities = [candidate.entity_id for candidate in selected]
    assignment_fields_match = all(
        isinstance(assignment, Mapping)
        and assignment.get("surveillance_mode") == candidate.mode
        and assignment.get("entity_id") == candidate.entity_id
        and isinstance(assignment.get("parameters"), Mapping)
        and assignment["parameters"].get("report_ids") == list(candidate.report_ids)
        for assignment, candidate in zip(assignments, selected, strict=True)
    )
    objective_parity = (
        status == "OPTIMAL_SOLUTION"
        and assignment_fields_match
        and native.get("combined_score") == round(oracle.score * SCORE_SCALE)
        and native.get("maneuver_count") == len(oracle.candidates)
        and native.get("surveillance_duration") == round(oracle.duration_s * TIME_SCALE)
    )
    semantic_parity = (
        objective_parity
        and solver_modes == [candidate.mode for candidate in oracle.candidates]
        and solver_entities == [candidate.entity_id for candidate in oracle.candidates]
    )
    exact_route_parity = solver_ids == oracle_ids
    if not semantic_parity or not exact_route_parity:
        raise ValueError(f"{output.name}/{kind} oracle/MiniZinc mismatch")
    solver_summary = {
        "status": status,
        "combined_score": native["combined_score"],
        "assignment_ids": solver_ids,
        "modes": solver_modes,
    }
    _write_json(snapshot / "solver.json", solver_summary)

    modes = [candidate.mode for candidate in oracle.candidates]
    return {
        "snapshot": kind,
        "candidate_count": len(graph.candidates),
        "candidate_counts_by_mode": {
            mode: sum(candidate.mode == mode for candidate in graph.candidates)
            for mode in ("fixed_view", "pursue_ship")
        },
        "pursuit_ship_ids": sorted(
            {
                candidate.entity_id
                for candidate in graph.candidates
                if candidate.mode == "pursue_ship" and candidate.entity_id is not None
            }
        ),
        "selected_modes": modes,
        "selected_candidate_ids": oracle_ids,
        "first_mode": modes[0] if modes else None,
        "selected_entity_ids": [
            candidate.entity_id
            for candidate in oracle.candidates
            if candidate.entity_id is not None
        ],
        "score": oracle.score,
        "covered_report_count": len(oracle.covered_report_ids),
        "generation_seconds": generation_seconds,
        "validation_seconds": check_seconds,
        "solver_seconds": solver_seconds,
        "solver_status": status,
        "oracle_minizinc_match": semantic_parity and exact_route_parity,
        "exact_candidate_id_route_match": exact_route_parity,
    }


def evaluate_corpus(
    benchmark_root: Path,
    output: Path,
    *,
    minizinc: Path,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    manifest = _json(benchmark_root / "benchmark_manifest.json")
    instances = manifest.get("instances") if isinstance(manifest, Mapping) else None
    if not isinstance(instances, list):
        raise ValueError("benchmark manifest has no instance list")
    model_path = (
        Path(__file__).resolve().parents[1]
        / "conf/skills/hyper/creating-minizinc-problem-files/examples/"
        "event-information-patrol/model.mzn"
    )
    model = model_path.read_bytes()
    records: list[dict[str, object]] = []
    switches = Counter()
    route_switch_count = 0
    target_pursuit_switches = Counter()

    for entry in instances:
        if not isinstance(entry, Mapping):
            raise ValueError("benchmark instance record must be an object")
        instance_id = str(entry["instance_id"])
        instance = benchmark_root / str(entry["instance_directory"])
        reports = _public_reports(_json(instance / "events_report.json"))
        entity_ids = sorted(
            {
                entity_id
                for report in reports
                if isinstance((entity_id := report.get("entity_id")), int)
                and not isinstance(entity_id, bool)
            }
        )
        mission_id = f"corpus-{instance_id}"
        prior_manager = ReportingReliabilityManager(mission_id, entity_ids)
        prior_belief = prior_manager.snapshot(
            input_event_id=f"{instance_id}:prior",
            input_revision=0,
            created_at="2026-09-05T00:00:00+10:00",
        )
        prior_environment = _environment(
            mission_id,
            reports,
            mission_time_s=0.0,
            position=(0.0, 0.0),
            checks=(),
        )

        target_id, check = _counterfactual_check(reports)
        evidence_manager = ReportingReliabilityManager(mission_id, entity_ids)
        evidence_manager.update_checks(
            (check,),
            input_event_id=f"{instance_id}:counterfactual",
            input_revision=1,
            created_at="2026-09-05T00:00:01+10:00",
        )
        evidence_belief = evidence_manager.snapshot(
            input_event_id=f"{instance_id}:counterfactual",
            input_revision=1,
            created_at="2026-09-05T00:00:01+10:00",
        )
        evidence_environment = _environment(
            mission_id,
            reports,
            mission_time_s=0.0,
            position=(0.0, 0.0),
            checks=(check,),
        )

        instance_output = output / "snapshots" / instance_id
        executor = MiniZincExecutor(
            minizinc,
            output / "solver-artifacts" / instance_id,
            timeout_seconds=timeout_seconds,
        )
        prior = _evaluate_snapshot(
            output=instance_output,
            kind="prior",
            environment=prior_environment,
            belief=prior_belief,
            model=model,
            executor=executor,
        )
        evidence = _evaluate_snapshot(
            output=instance_output,
            kind="counterfactual-altered",
            environment=evidence_environment,
            belief=evidence_belief,
            model=model,
            executor=executor,
        )
        truth = _json(instance / "mission1_truth.json")
        probabilities = (
            truth.get("ship_corruption_probabilities")
            if isinstance(truth, Mapping)
            else None
        )
        truth_label = (
            probabilities.get(str(target_id))
            if isinstance(probabilities, Mapping)
            else None
        )
        switch = f"{prior['first_mode']}->{evidence['first_mode']}"
        switches[switch] += 1
        route_changed = (
            prior["selected_candidate_ids"] != evidence["selected_candidate_ids"]
        )
        route_switch_count += route_changed
        prior_targets = set(cast(list[int], prior["selected_entity_ids"]))
        evidence_targets = set(cast(list[int], evidence["selected_entity_ids"]))
        target_switch = f"{target_id in prior_targets}->{target_id in evidence_targets}"
        target_pursuit_switches[target_switch] += 1
        records.extend(
            (
                {
                    "instance_id": instance_id,
                    "difficulty": entry.get("difficulty"),
                    "counterfactual_entity_id": target_id,
                    "counterfactual_truth_probability_diagnostic": truth_label,
                    "evidence_conditioned_route_changed": route_changed,
                    **prior,
                },
                {
                    "instance_id": instance_id,
                    "difficulty": entry.get("difficulty"),
                    "counterfactual_entity_id": target_id,
                    "counterfactual_truth_probability_diagnostic": truth_label,
                    "evidence_conditioned_route_changed": route_changed,
                    **evidence,
                },
            )
        )

    first_modes: dict[str, Counter[str]] = defaultdict(Counter)
    selected_modes: dict[str, Counter[str]] = defaultdict(Counter)
    timings: dict[str, list[float]] = defaultdict(list)
    for record in records:
        snapshot = str(record["snapshot"])
        if record["first_mode"] is not None:
            first_modes[snapshot][str(record["first_mode"])] += 1
        selected_modes[snapshot].update(
            map(str, cast(list[str], record["selected_modes"]))
        )
        timings[snapshot].append(float(cast(Any, record["solver_seconds"])))
    summary = {
        "benchmark_root": str(benchmark_root.resolve()),
        "instance_count": len(instances),
        "snapshot_count": len(records),
        "all_optimal": all(
            record["solver_status"] == "OPTIMAL_SOLUTION" for record in records
        ),
        "all_oracle_minizinc_match": all(
            bool(record["oracle_minizinc_match"]) for record in records
        ),
        "exact_candidate_id_route_match_count": sum(
            bool(record["exact_candidate_id_route_match"]) for record in records
        ),
        "first_mode_distributions": {
            kind: dict(counts) for kind, counts in sorted(first_modes.items())
        },
        "selected_mode_distributions": {
            kind: dict(counts) for kind, counts in sorted(selected_modes.items())
        },
        "first_mode_switches": dict(sorted(switches.items())),
        "evidence_conditioned_route_switch_count": route_switch_count,
        "target_pursuit_switches": dict(sorted(target_pursuit_switches.items())),
        "solver_timing_seconds": {
            kind: {
                "minimum": min(values),
                "median": statistics.median(values),
                "maximum": max(values),
            }
            for kind, values in sorted(timings.items())
        },
        "records": records,
    }
    _write_json(output / "summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    repository_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark_root", metavar="BENCHMARK_ROOT", type=Path)
    parser.add_argument("output_directory", metavar="AGENT_VAR_OUTPUT", type=Path)
    parser.add_argument(
        "--minizinc",
        type=Path,
        default=(
            repository_root / "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc"
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    output = args.output_directory.resolve()
    if not output.is_relative_to((repository_root / "var").resolve()):
        parser.error("AGENT_VAR_OUTPUT must be under this repository's var directory")
    output.mkdir(parents=True, exist_ok=True)
    summary = evaluate_corpus(
        args.benchmark_root.resolve(),
        output,
        minizinc=args.minizinc.resolve(),
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps({key: value for key, value in summary.items() if key != "records"})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
