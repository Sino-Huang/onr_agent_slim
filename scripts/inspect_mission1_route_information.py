"""Public-only comparison of scheduled versus selected-route information credit.

This diagnostic does not choose a route, update beliefs, or report recall. The
route-conditioned curve is the existing additive-precision approximation, not
exact Bayesian lookahead or the value of future decisions after new evidence.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from onr.application.mission1_planning import _batch_information_value, _opportunities
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


def information_curve(variance: float, gain: float, normalizer: float, count: int) -> list[float]:
    """Normalized G(k) for k actually selected checks, including G(0)=0."""
    if variance <= 0 or gain <= 0 or normalizer <= 0:
        return [0.0] * (count + 1)
    return [0.0] + [
        0.5 * variance * k * gain / (variance + (k - 1) * gain) / normalizer
        for k in range(1, count + 1)
    ]


def inspect_route(environment, belief, solution):
    opportunities = _opportunities(environment, belief)
    by_id = {item.report_id: item for item in opportunities}
    selected_ids = [report for assignment in solution["assignments"]
                    for report in assignment["parameters"]["report_ids"]]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("selected route repeats a public report")
    unknown = set(selected_ids) - by_id.keys()
    if unknown:
        raise ValueError("selected route references unavailable public reports")
    selected = Counter(by_id[report].entity_id for report in selected_ids)
    available = Counter(item.entity_id for item in opportunities)
    normalizer = max((item.estimation for item in opportunities), default=0.0)
    ships = {ship.entity_id: ship for ship in belief.ships}
    rows = []
    for entity_id, count in sorted(available.items()):
        ship = ships[entity_id]
        items = [item for item in opportunities if item.entity_id == entity_id]
        curve = information_curve(ship.variance, ship.expected_variance_reduction,
                                  normalizer, count)
        n = selected[entity_id]
        scheduled_credit = sum(_batch_information_value([by_id[report]])
                               for report in selected_ids if by_id[report].entity_id == entity_id)
        rows.append({
            "entity_id": entity_id,
            "remaining_public_reports": count,
            "selected_public_reports": n,
            "posterior_mean": ship.mean,
            "posterior_variance": ship.variance,
            "one_check_expected_variance_reduction": ship.expected_variance_reduction,
            "scheduled_information_credit": scheduled_credit,
            "selected_route_information_credit": curve[n],
            "first_selected_report_time_s": min(
                (by_id[report].time_s for report in selected_ids
                 if by_id[report].entity_id == entity_id), default=None,
            ),
            "uniform_first_check_credit": _batch_information_value(items[:1]),
            "route_first_check_credit": curve[1],
        })
    return {
        "scope": "Public-only score diagnostic; no route optimization, detection, or recall claim",
        "information_model": "Selected-count additive-precision approximation; no downstream decision value",
        "selected_report_count": len(selected_ids),
        "selected_entity_count": len(selected),
        "scheduled_information_credit": sum(row["scheduled_information_credit"] for row in rows),
        "selected_route_information_credit": sum(row["selected_route_information_credit"] for row in rows),
        "ships": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("environment", "belief", "solution", "agent-var", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.agent_var.resolve().name != "var" or not args.output.resolve().is_relative_to(args.agent_var.resolve()):
        parser.error("output must be inside the caller-provided Agent var directory")
    read = lambda path: json.loads(path.read_text())
    result = inspect_route(read(args.environment), ReportingReliabilitySnapshot.from_dict(read(args.belief)),
                           read(args.solution))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "ships"}))


if __name__ == "__main__":
    main()
