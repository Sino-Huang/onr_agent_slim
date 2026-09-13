"""Fast, offline ideal-coverage scoring of public-input Mission 1 plans.

The solver never receives truth. Evaluation assumes perfect scheduled arrival,
360-degree radius coverage without occlusion, and perfect target tracking during
pursuit. It counts only scheduled surveillance windows, not transit sightings.
This is a plan-quality diagnostic, not a prediction of live camera performance.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path

from onr.adapters.minizinc import MiniZincExecutor
from onr.application.mission1_planning import (
    SCORE_SCALE,
    TIME_SCALE,
    build_candidate_dag,
    longest_path_oracle,
    serialize_minizinc_data,
)
from onr.application.reporting_reliability import ReportingReliabilityManager
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def solve_public_plan(environment, belief, model, executable, output, *, information_horizon_seconds=None,
                      route_horizon_seconds=None):
    """Only public evidence and the supplied posterior enter this function."""
    output.mkdir(parents=True, exist_ok=True)
    # Preserve the exact public snapshot even when graph generation or native
    # solving fails; it is diagnostic input, not an accepted planning result.
    write_json(output / "environment.json", environment)
    write_json(output / "belief.json", belief.to_dict())
    if information_horizon_seconds is not None:
        write_json(output / "planning-context.json", {
            "information_horizon_seconds": information_horizon_seconds,
            "information_scoring": "selected_route_counts",
            "scope": "Bounded planning; not public-schedule completion",
            **({"route_horizon_seconds": route_horizon_seconds,
                "information_deadline_s": environment["mission_time_seconds"] + information_horizon_seconds}
               if route_horizon_seconds is not None else {}),
        })
    started = time.perf_counter()
    graph = build_candidate_dag(environment, belief, information_horizon_seconds=information_horizon_seconds,
                                route_horizon_seconds=route_horizon_seconds)
    oracle = longest_path_oracle(graph)
    assert len(oracle.covered_report_ids) == len(set(oracle.covered_report_ids))
    assets = {
        "model.mzn": model.read_bytes(),
        "data.dzn": serialize_minizinc_data(graph).encode(),
    }
    generation_seconds = time.perf_counter() - started
    executor = MiniZincExecutor(executable.resolve(), output / "solver")
    started = time.perf_counter()
    check = executor.check(assets)
    validation_seconds = time.perf_counter() - started
    assert check.accepted, check.stderr
    started = time.perf_counter()
    result = executor.execute(assets, "coin-bc")
    solver_seconds = time.perf_counter() - started
    (output / "solver.stdout").write_text(result.stdout)
    (output / "solver.stderr").write_text(result.stderr)
    stream = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(row.get("status") == "OPTIMAL_SOLUTION" for row in stream), result.stderr
    raw = next(
        row["output"] for row in reversed(stream) if row.get("type") == "solution"
    )
    solution = json.loads(raw.get("default", raw.get("raw")))
    assert [a["candidate_id"] for a in solution["assignments"]] == [
        c.candidate_id for c in oracle.candidates
    ]
    assert solution["combined_score"] == round(oracle.score * solution["score_scale"])
    assert [
        (a["surveillance_mode"], a["entity_id"]) for a in solution["assignments"]
    ] == [(c.mode, c.entity_id) for c in oracle.candidates]
    assert solution["maneuver_count"] == len(oracle.candidates)
    assert solution["surveillance_duration"] == round(oracle.duration_s * TIME_SCALE)
    for assignment, candidate in zip(solution["assignments"], oracle.candidates, strict=True):
        assert assignment["parameters"].get("arrival_direction") == candidate.arrival_direction
        assert assignment["start"] == round(candidate.start_s * TIME_SCALE)
        assert assignment["duration"] == round(candidate.duration_s * TIME_SCALE)
        assert assignment["parameters"]["report_span"] == round(candidate.report_span_s * TIME_SCALE)
        assert assignment["parameters"]["report_ids"] == list(candidate.report_ids)
        assert assignment["parameters"].get("scored_observation_windows", []) == [
            {"start": round(start * TIME_SCALE), "duration": round((end - start) * TIME_SCALE), "time_scale": TIME_SCALE}
            for start, end in candidate.scored_observation_windows
        ]
        assert assignment["parameters"]["utility"] == {
            "recall": round(candidate.recall_utility * SCORE_SCALE),
            "estimation": round(candidate.estimation_utility * SCORE_SCALE),
            "omission_yield": round(candidate.omission_yield * SCORE_SCALE),
            "combined": sum(round(value * SCORE_SCALE) for value in (
                candidate.recall_utility, candidate.estimation_utility, candidate.omission_yield)),
            "scale": SCORE_SCALE,
        }
    write_json(output / "solution.json", solution)
    return solution, {
        "generation_seconds": generation_seconds,
        "validation_seconds": validation_seconds,
        "solver_seconds": solver_seconds,
        "candidate_count": len(graph.candidates),
        "optimal_oracle_parity": True,
        "information_horizon_seconds": information_horizon_seconds,
        **({"route_horizon_seconds": route_horizon_seconds} if route_horizon_seconds is not None else {}),
    }


def ideal_visible_ids(assignment, positions, radius):
    """Perfect tracking for pursuit; fixed views use exact solver coordinates."""
    if assignment["surveillance_mode"] == "pursue_ship":
        centre = positions.get(assignment["entity_id"])
        if centre is None:
            return []
    else:
        centre = (assignment["parameters"]["x"], assignment["parameters"]["y"])
    return [
        ship
        for ship, position in positions.items()
        if math.dist(centre, position) <= radius
    ]


def score_ideal_plan(solution, converter, radius, step, *, visibility=None):
    """Use runtime discrepancy pairing/latching with idealized visibility."""
    converter.reset_detected_discrepancies()
    assignments = solution["assignments"]
    seen_reports = [r for a in assignments for r in a["parameters"]["report_ids"]]
    assert len(seen_reports) == len(set(seen_reports))
    previous_end = -math.inf
    for assignment in assignments:
        scale = assignment["time_scale"]
        start = assignment["start"] / scale
        end = (assignment["start"] + assignment["duration"]) / scale
        assert start >= previous_end
        previous_end = end
        # Half-open dwell: no extra sensing tick after the planned departure.
        for tick in range(math.ceil(start / step), math.ceil(end / step)):
            now = tick * step
            positions = {}
            for ship, trajectory in converter.trajectories.items():
                point = trajectory.get_traj_point_at_time(now)
                if point is not None:
                    positions[ship] = (point.ned_north, point.ned_east)
            visible = (
                ideal_visible_ids(assignment, positions, radius) if visibility is None
                else visibility(assignment, positions)
            )
            converter.update_detected_discrepancies(visible, now)
    return summarize_detected_discrepancies(converter, assignments)


def summarize_detected_discrepancies(converter, assignments):
    """Score the latched ledger without replaying/resetting an adaptive run."""
    checks = converter.get_event_report_checks()
    realized = Counter()
    corrupted_times = []
    for ship, events in converter.ground_truth_events.items():
        reports = converter.event_reports.get(ship, [])
        for index, actual in enumerate(events):
            pair = converter._event_report_pairings.get(ship, {}).get(index)
            outcome = (
                "omitted"
                if pair is None
                else (
                    "altered"
                    if converter._compare_actual_report(
                        actual, reports[pair], actual["time"]
                    )
                    else "clean"
                )
            )
            realized[outcome] += 1
            if outcome != "clean":
                corrupted_times.append(actual["time"])
    issues = {
        row["issue_id"]
        for rows in converter.get_detected_discrepancies().values()
        for row in rows
    }
    denominator = realized["altered"] + realized["omitted"]
    assert len(issues) == sum(c["outcome"] != "clean" for c in checks)
    return {
        "issues_discovered": len(issues),
        "total_corrupted": denominator,
        "recall": len(issues) / denominator if denominator else None,
        "realized_outcomes": dict(realized),
        "check_outcomes": dict(Counter(c["outcome"] for c in checks)),
        "checks": checks,
        "corrupted_outcomes_by_time": dict(sorted(Counter(corrupted_times).items())),
        "modes": dict(Counter(a["surveillance_mode"] for a in assignments)),
        "pursuit_entities": [
            a["entity_id"] for a in assignments if a["entity_id"] is not None
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "environment",
        "belief",
        "model",
        "minizinc",
        "scenario",
        "reports",
        "truth",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--radii", nargs="+", type=float, required=True)
    args = parser.parse_args()
    assert args.output.resolve().is_relative_to(
        Path(__file__).resolve().parents[1] / "var"
    )
    args.output.mkdir(parents=True, exist_ok=False)
    environment = json.loads(args.environment.read_text())
    belief = ReportingReliabilitySnapshot.from_dict(json.loads(args.belief.read_text()))
    plans = []
    # Finish ALL solves before opening evaluator-only truth/scenario inputs.
    for index, radius in enumerate(args.radii):
        assert math.isfinite(radius) and radius > 0
        case = args.output / f"case-{index}"
        case.mkdir()
        public = {
            **environment,
            "controlled_vehicle": {
                **environment["controlled_vehicle"],
                "fov_radius": radius,
            },
        }
        solution, timing = solve_public_plan(
            public, belief, args.model, args.minizinc, case
        )
        plans.append((radius, case, solution, timing))
    from loguru import logger
    from onr_physical_runtime.scenario import ScenarioConfig

    logger.remove()
    scenario = ScenarioConfig.from_yaml(args.scenario)
    converter, env, config = scenario.build(event_reports_path_override=args.reports)
    truth = json.loads(args.truth.read_text())
    probabilities = {
        int(k): p for k, p in truth["ship_corruption_probabilities"].items()
    }
    summaries = []
    try:
        for radius, case, solution, timing in plans:
            started = time.perf_counter()
            score = score_ideal_plan(
                solution, converter, radius, config.time_step_duration
            )
            manager = ReportingReliabilityManager(
                environment["mission_id"], probabilities
            )
            manager.update_checks(
                environment["world_model_info"].get("event_report_checks", []),
                input_event_id="offline-input-checks",
                input_revision=0,
                created_at=belief.created_at,
            )
            manager.update_checks(
                score["checks"],
                input_event_id="offline-ideal-checks",
                input_revision=1,
                created_at=belief.created_at,
            )
            posterior = manager.snapshot(
                input_event_id="offline-ideal-checks",
                input_revision=1,
                created_at=belief.created_at,
            )
            errors = {
                s.entity_id: (s.mean - probabilities[s.entity_id]) ** 2
                for s in posterior.ships
            }
            cohorts = [
                [
                    error
                    for ship, error in errors.items()
                    if (probabilities[ship] == 0) == honest
                ]
                for honest in (True, False)
            ]
            mse0, mseplus = [
                sum(values) / len(values) if values else None for values in cohorts
            ]
            score.update(
                {
                    "radius_m": radius,
                    **timing,
                    "evaluation_seconds": time.perf_counter() - started,
                    "MSE_0": mse0,
                    "MSE_plus": mseplus,
                    "balanced_MSE": (mse0 + mseplus) / 2
                    if mse0 is not None and mseplus is not None
                    else None,
                    "assumptions": "Perfect scheduled arrival; omnidirectional radius without occlusion; perfect pursuit. Only scheduled surveillance windows, no transit sightings or replanning. Offline truth never passed to planner.",
                    "scope": "Ideal plan diagnostic, not measured live recall.",
                }
            )
            write_json(case / "evaluation.json", score)
            summaries.append(
                {
                    k: v
                    for k, v in score.items()
                    if k not in ("checks", "corrupted_outcomes_by_time")
                }
            )
    finally:
        env.close()
    write_json(args.output / "summary.json", summaries)
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
