"""Public-only one-check value-of-information diagnostic, not a Mission planner.

Assume a hypothetical actual-event check is supplied at the current pose/time.
Compare choosing a future route before versus after its outcome. This isolates
recall decision value, excluding estimation rewards, hidden-omission yield and
the cost/availability of acquiring that check by default. Acquisition-aware
mode instead prices one reachable singleton published-report observation and
conditions its outcomes on report presence. No executable plan is published.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from onr.application import mission1_planning as planning
from onr.application.reporting_reliability import OUTCOMES, ReportingReliabilityManager
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


def snapshot(manager, stamp):
    return manager.snapshot(input_event_id="diagnostic", input_revision=0, created_at=stamp)


def reconstruct(belief):
    """Outcome counts are sufficient under the existing exchangeable grid model."""
    manager = ReportingReliabilityManager(belief.mission_id, [s.entity_id for s in belief.ships])
    for ship in belief.ships:
        for outcome, count in ship.outcome_counts.items():
            for index in range(count):
                manager._update_one({"check_id": f"history:{ship.entity_id}:{outcome}:{index}",
                                     "entity_id": ship.entity_id, "outcome": outcome})
    rebuilt = snapshot(manager, belief.created_at)
    for original, recovered in zip(belief.ships, rebuilt.ships, strict=True):
        for name in ("mean", "variance", "expected_omission_probability"):
            if not math.isclose(getattr(original, name), getattr(recovered, name), abs_tol=1e-10):
                raise ValueError("belief is not reconstructible from the configured prior and public counts")
    return manager


def recall_graph(graph, opportunities, belief):
    risks = {s.entity_id: s.mean for s in belief.ships}
    entities = {r.report_id: r.entity_id for r in opportunities}
    candidates = []
    for candidate in graph.candidates:
        recall = 0.5 * math.fsum(risks[entities[r]] for r in candidate.report_ids)
        candidates.append(replace(candidate, recall_utility=recall, estimation_utility=0,
                                  omission_yield=0, combined_score=recall))
    return replace(graph, candidates=tuple(candidates))


def outcome_branches(manager, entity_id, stamp, *, published=False):
    prior = snapshot(manager, stamp)
    ship = next(s for s in prior.ships if s.entity_id == entity_id)
    probabilities = {"clean": 1 - ship.mean,
                     "altered": ship.mean - ship.expected_omission_probability,
                     "omitted": ship.expected_omission_probability}
    if published:
        # A known published report cannot itself be omitted. Its clean/altered
        # likelihoods condition on report presence, not an invented omission.
        presence = 1 - ship.expected_omission_probability
        probabilities = {key: value / presence for key, value in probabilities.items()
                         if key != "omitted"}
    result = []
    for outcome in OUTCOMES:
        probability = probabilities.get(outcome, 0)
        if probability <= 0:
            continue
        branch = copy.deepcopy(manager)
        branch._update_one({"check_id": f"hypothetical:{entity_id}:{outcome}",
                            "entity_id": entity_id, "outcome": outcome})
        posterior = snapshot(branch, stamp)
        result.append((outcome, probability, posterior))
    return prior, result


def inspect_entity(graph, opportunities, manager, entity_id, stamp, verify=None, *, prepared=None):
    prior, outcomes = prepared if prepared is not None else outcome_branches(manager, entity_id, stamp)
    ship = next(s for s in prior.ships if s.entity_id == entity_id)
    branches = []
    weighted_candidates = [0.0] * len(graph.candidates)
    adaptive = 0.0
    for outcome, probability, posterior in outcomes:
        scored = recall_graph(graph, opportunities, posterior)
        route = planning.longest_path_oracle(scored)
        if verify is not None:
            verify(scored, route, entity_id, outcome)
        adaptive += probability * route.score
        for index, candidate in enumerate(scored.candidates):
            weighted_candidates[index] += probability * round(candidate.recall_utility * planning.SCORE_SCALE)
        branches.append({"outcome": outcome, "probability": probability,
                         "posterior_mean": next(s.mean for s in posterior.ships if s.entity_id == entity_id),
                         "future_score": route.score, "report_ids": list(route.covered_report_ids)})
    # Maximise the same branch-weighted rewards without observing the branch.
    # Rounding error is bounded separately, not treated as decision value.
    fixed_graph = replace(graph, candidates=tuple(
        replace(c, recall_utility=score / planning.SCORE_SCALE, estimation_utility=0,
                omission_yield=0, combined_score=score / planning.SCORE_SCALE)
        for c, score in zip(graph.candidates, weighted_candidates, strict=True)))
    fixed = planning.longest_path_oracle(fixed_graph)
    tolerance = len(graph.candidates) / planning.SCORE_SCALE
    if adaptive + tolerance < fixed.score:
        raise AssertionError("conditioning cannot lower the optimum of the same future decision set")
    return {"entity_id": entity_id, "posterior_mean": ship.mean,
            "variance_reduction": ship.expected_variance_reduction,
            "adaptive_expected_recall_utility": adaptive,
            "fixed_expected_recall_utility": fixed.score,
            "decision_value": adaptive - fixed.score, "rounding_tolerance": tolerance,
            "distinct_branch_routes": len({tuple(b["report_ids"]) for b in branches}),
            "branches": branches}


def acquisition_suffix(graph, first, vehicle, chronological):
    """Remaining decisions after actually reaching and observing first."""
    reports = set(first.report_ids)
    candidates = tuple(c for c in graph.candidates
                       if reports.isdisjoint(c.report_ids)
                       and (not chronological or c.start_s - c.observation_delay_s
                            > first.start_s - first.observation_delay_s + first.report_span_s + 1e-9)
                       and first.end_s + planning._navigation_time(
                           first.end_x, first.end_y, c.x, c.y, vehicle["max_velocity"],
                           first.arrival_direction, c.arrival_direction,
                           vehicle.get("quarter_turn_seconds", 0)) <= c.start_s + 1e-9)
    return planning.CandidateDAG(candidates, planning._candidate_arcs(
        candidates, vehicle["max_velocity"], vehicle.get("quarter_turn_seconds", 0),
        chronological, information_aware=True), 0, len(candidates) + 1)


def inspect_acquisitions(graph, opportunities, manager, stamp, vehicle, chronological, verify=None):
    """One reachable published check, then outcome-conditioned future routes.

    Restrict the first observation to singleton fixed views; incidental checks
    and later evidence adaptation are not modeled. The fixed horizon is shared
    by every first choice, so travel/dwell consume future opportunities.
    """
    by_id = {r.report_id: r for r in opportunities}
    prepared = {}
    rows = []
    for index, first in enumerate(graph.candidates):
        if first.mode != "fixed_view" or len(first.report_ids) != 1:
            continue
        entity = by_id[first.report_ids[0]].entity_id
        if entity not in prepared:
            prepared[entity] = outcome_branches(manager, entity, stamp, published=True)
        suffix = acquisition_suffix(graph, first, vehicle, chronological)
        row = inspect_entity(suffix, opportunities, manager, entity, stamp,
                             prepared=prepared[entity])
        immediate = 0.5 * next(probability for outcome, probability, _ in prepared[entity][1]
                               if outcome == "altered")
        row.update(candidate_id=first.candidate_id, candidate_index=index,
                   first_report_ids=list(first.report_ids), start_s=first.start_s,
                   end_s=first.end_s, x=first.x, y=first.y,
                   arrival_direction=first.arrival_direction,
                   suffix_candidates=len(suffix.candidates), immediate_recall_utility=immediate,
                   adaptive_total=immediate + row["adaptive_expected_recall_utility"],
                   fixed_total=immediate + row["fixed_expected_recall_utility"])
        rows.append(row)
    if rows:
        best = max(rows, key=lambda r: (r["adaptive_total"], -r["end_s"], -r["candidate_index"]))
        if verify is not None:
            first = graph.candidates[best["candidate_index"]]
            suffix = acquisition_suffix(graph, first, vehicle, chronological)
            inspect_entity(suffix, opportunities, manager, best["entity_id"], stamp, verify,
                           prepared=prepared[best["entity_id"]])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("environment", "belief", "agent-var", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--horizon-seconds", type=float, required=True)
    parser.add_argument("--minizinc", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--acquisition-aware", action="store_true",
                        help="Price one reachable singleton published check before future routing")
    args = parser.parse_args()
    if args.agent_var.resolve().name != "var" or not args.output.resolve().is_relative_to(args.agent_var.resolve()):
        parser.error("output must be under caller-provided Agent var")
    if bool(args.minizinc) != bool(args.model):
        parser.error("native verification requires both --minizinc and --model")
    args.output.mkdir(parents=True, exist_ok=False)
    environment = json.loads(args.environment.read_text())
    belief = ReportingReliabilitySnapshot.from_dict(json.loads(args.belief.read_text()))
    manager = reconstruct(belief)
    with patch.object(planning, "expand_information_states", side_effect=lambda graph, opportunities: graph):
        graph = planning.build_candidate_dag(environment, belief,
                                             information_horizon_seconds=args.horizon_seconds)
    # Rebuild edges: information/omission-positive intermediary pruning is not
    # valid when this diagnostic deliberately values public recall alone.
    opportunities = planning._opportunities(environment, belief)
    graph = recall_graph(graph, opportunities, belief)
    vehicle = environment["controlled_vehicle"]
    graph = replace(graph, arcs=planning._candidate_arcs(
        graph.candidates, vehicle["max_velocity"],
        vehicle.get("quarter_turn_seconds", 0),
        environment.get("observation_window_seconds", 0) > 0, information_aware=True))

    def verify(scored, route, entity, outcome):
        data = args.output / f"entity-{entity}-{outcome}.dzn"
        data.write_text(planning.serialize_minizinc_data(scored))
        native = subprocess.run([str(args.minizinc), "--solver", "coin-bc", str(args.model), str(data)],
                                capture_output=True, text=True, timeout=30, check=True)
        data.with_suffix(".stdout").write_text(native.stdout)
        data.with_suffix(".stderr").write_text(native.stderr)
        result = json.loads(native.stdout.splitlines()[0])
        assert "==========" in native.stdout
        assert result["combined_score"] == round(route.score * planning.SCORE_SCALE)
        assert [r for a in result["assignments"] for r in a["parameters"]["report_ids"]] == list(route.covered_report_ids)

    if args.acquisition_aware:
        rows = inspect_acquisitions(graph, opportunities, manager, belief.created_at, vehicle,
                                    environment.get("observation_window_seconds", 0) > 0,
                                    verify if args.minizinc else None)
        best_adaptive = max(rows, key=lambda r: r["adaptive_total"], default=None)
        best_fixed = max(rows, key=lambda r: r["fixed_total"], default=None)
        print(json.dumps({"first_choices": len(rows), "best_adaptive": best_adaptive,
                          "best_fixed": best_fixed}), flush=True)
    else:
        rows = []
        for ship in belief.ships:
            row = inspect_entity(graph, opportunities, manager, ship.entity_id, belief.created_at,
                                 verify if args.minizinc else None)
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "branches"}), flush=True)
    result = {"scope": __doc__, "horizon_seconds": args.horizon_seconds,
              "acquisition_aware": args.acquisition_aware,
              "native_verification_scope": "best acquisition branches only" if args.acquisition_aware else "all entity branches",
              "candidate_count": len(graph.candidates), "native_verified": bool(args.minizinc),
              "ships": rows}
    (args.output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
