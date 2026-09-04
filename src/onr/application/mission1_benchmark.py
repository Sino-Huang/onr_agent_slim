"""Mission 1 benchmark metrics kept separate by scientific role."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


@dataclass(frozen=True, slots=True)
class Mission1BenchmarkMetrics:
    issue_discovery_recall: float
    mse_zero: float
    mse_positive: float
    balanced_mse: float
    prior_balanced_mse: float
    balanced_mse_improvement: float
    inferred_q_mean: float
    inferred_q_credible_interval: tuple[float, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_discovery_recall": self.issue_discovery_recall,
            "mse_zero": self.mse_zero,
            "mse_positive": self.mse_positive,
            "balanced_mse": self.balanced_mse,
            "prior_balanced_mse": self.prior_balanced_mse,
            "balanced_mse_improvement": self.balanced_mse_improvement,
            "inferred_q_mean": self.inferred_q_mean,
            "inferred_q_credible_interval": list(self.inferred_q_credible_interval),
        }


def mission1_benchmark_metrics(
    true_probabilities: Mapping[int, float],
    snapshot: ReportingReliabilitySnapshot,
    discovered_issue_ids: Iterable[str],
    total_corrupted_outcomes: int,
    *,
    prior_mean: float = 0.132916,
) -> Mission1BenchmarkMetrics:
    estimates = {ship.entity_id: ship.mean for ship in snapshot.ships}
    if set(estimates) != set(true_probabilities):
        raise ValueError("benchmark truth and reliability snapshot rosters differ")
    zero_errors = [estimates[key] ** 2 for key, value in true_probabilities.items() if value == 0.0]
    positive_errors = [
        (estimates[key] - value) ** 2
        for key, value in true_probabilities.items()
        if value > 0.0
    ]
    if not zero_errors or not positive_errors:
        raise ValueError("balanced MSE requires honest and nonzero-probability ships")
    mse_zero = math.fsum(zero_errors) / len(zero_errors)
    mse_positive = math.fsum(positive_errors) / len(positive_errors)
    prior_zero = prior_mean * prior_mean
    prior_positive = math.fsum(
        (prior_mean - value) ** 2
        for value in true_probabilities.values()
        if value > 0.0
    ) / len(positive_errors)
    prior_balanced = (prior_zero + prior_positive) / 2.0
    balanced = (mse_zero + mse_positive) / 2.0
    unique_issues = len(set(discovered_issue_ids))
    recall = (
        1.0
        if total_corrupted_outcomes == 0
        else unique_issues / total_corrupted_outcomes
    )
    if not 0.0 <= recall <= 1.0:
        raise ValueError("discovered issues exceed hidden corrupted outcomes")
    return Mission1BenchmarkMetrics(
        issue_discovery_recall=recall,
        mse_zero=mse_zero,
        mse_positive=mse_positive,
        balanced_mse=balanced,
        prior_balanced_mse=prior_balanced,
        balanced_mse_improvement=prior_balanced - balanced,
        inferred_q_mean=snapshot.omission.mean,
        inferred_q_credible_interval=snapshot.omission.credible_interval,
    )


__all__ = ["Mission1BenchmarkMetrics", "mission1_benchmark_metrics"]
