from __future__ import annotations

import pytest

from onr.application.mission1_benchmark import mission1_benchmark_metrics
from onr.application.reporting_reliability import ReportingReliabilityManager


def test_benchmark_reports_recall_balanced_mse_prior_and_q_separately() -> None:
    manager = ReportingReliabilityManager("mission-1", (1, 2))
    snapshot = manager.snapshot(
        input_event_id="initial",
        input_revision=0,
        created_at="2026-09-03T00:00:00+10:00",
    )
    metrics = mission1_benchmark_metrics(
        {1: 0.0, 2: 0.8},
        snapshot,
        ("issue-a", "issue-a", "issue-b"),
        4,
    )

    assert metrics.issue_discovery_recall == 0.5
    assert metrics.balanced_mse == pytest.approx(
        (metrics.mse_zero + metrics.mse_positive) / 2.0
    )
    assert metrics.balanced_mse_improvement == pytest.approx(
        metrics.prior_balanced_mse - metrics.balanced_mse
    )
    assert metrics.inferred_q_mean == pytest.approx(0.5)
