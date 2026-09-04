from __future__ import annotations

from pathlib import Path

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.reporting_reliability import (
    FileReportingReliabilityStore,
    ReportingReliabilityManager,
    ReportingReliabilityService,
)
from onr.contracts.environment import EnvironmentTickResult
from onr.ports.transport import Subscription


NOW = "2026-09-03T00:00:00+10:00"


def _check(check_id: str, entity_id: int, outcome: str) -> dict[str, object]:
    return {
        "check_id": check_id,
        "report_id": None if outcome == "omitted" else f"report-{check_id}",
        "entity_id": entity_id,
        "event_time_s": 41.5,
        "checked_at_s": 43.0,
        "outcome": outcome,
    }


def _tick(revision: int, checks: list[dict[str, object]]) -> EnvironmentTickResult:
    return EnvironmentTickResult(
        current_time=float(revision),
        environment_data={
            "mission_id": "mission-1",
            "mission_time_seconds": float(revision),
            "state_version": revision,
            "world_model_info": {"event_report_checks": checks},
        },
    )


def test_reporting_reliability_prior_and_evidence_direction() -> None:
    manager = ReportingReliabilityManager("mission-1", (1, 2))
    initial = manager.snapshot(
        input_event_id="initial", input_revision=0, created_at=NOW
    )

    assert initial.belief_kind == "reporting_reliability"
    assert initial.ships[0].mean == pytest.approx(0.132916, abs=2e-6)
    assert initial.omission.mean == pytest.approx(0.5, abs=1e-12)

    clean = manager.update_checks(
        (_check("clean-1", 1, "clean"),),
        input_event_id="tick-1",
        input_revision=1,
        created_at=NOW,
    )
    assert clean is not None
    assert clean.ships[0].mean < initial.ships[0].mean
    assert clean.omission.mean == pytest.approx(0.5, abs=1e-12)

    altered = manager.update_checks(
        (_check("altered-1", 2, "altered"),),
        input_event_id="tick-2",
        input_revision=2,
        created_at=NOW,
    )
    assert altered is not None
    assert altered.ships[1].mean > initial.ships[1].mean
    assert altered.omission.mean < 0.5

    omitted = manager.update_checks(
        (_check("omitted-1", 2, "omitted"),),
        input_event_id="tick-3",
        input_revision=3,
        created_at=NOW,
    )
    assert omitted is not None
    assert omitted.ships[1].mean > initial.ships[1].mean
    assert omitted.omission.mean > altered.omission.mean
    assert omitted.ships[1].outcome_counts == {
        "clean": 0,
        "altered": 1,
        "omitted": 1,
    }


def test_cumulative_ledger_replay_and_checkpoint_recovery_are_idempotent() -> None:
    checks = (_check("check-1", 1, "altered"), _check("check-2", 1, "clean"))
    manager = ReportingReliabilityManager("mission-1", (1,))
    first = manager.update_checks(
        checks, input_event_id="tick-2", input_revision=2, created_at=NOW
    )
    assert first is not None
    assert manager.update_checks(
        checks, input_event_id="tick-2-replay", input_revision=2, created_at=NOW
    ) is None

    recovered = ReportingReliabilityManager.from_checkpoint(manager.checkpoint())
    assert recovered.snapshot(
        input_event_id="tick-2", input_revision=2, created_at=NOW
    ).to_dict() == first.to_dict()
    assert recovered.update_checks(
        checks, input_event_id="tick-3", input_revision=3, created_at=NOW
    ) is None


def test_service_processes_each_buffered_tick_and_recovers_without_duplicates(
    tmp_path: Path,
) -> None:
    subscription = Subscription("context-coordination", "mission-1", "planning-evidence")
    transport = InProcessTransport((subscription,))
    store = FileReportingReliabilityStore(tmp_path)
    service = ReportingReliabilityService.create(
        "mission-1",
        (1,),
        store,
        transport,
        context_topic="planning-evidence",
        clock=lambda: NOW,
    )

    initial = service.load_current_snapshot()
    assert initial is not None and initial.belief_revision == 1
    assert service.current_snapshot_path() == (
        tmp_path
        / "bayesian-beliefs/mission-1"
        / f"reporting-reliability-{initial.content_sha256}.json"
    )
    assert service.current_snapshot_path().is_file()
    service.ingest_environment_tick(_tick(1, [_check("check-1", 1, "clean")]))
    service.ingest_environment_tick(
        _tick(
            2,
            [
                _check("check-1", 1, "clean"),
                _check("check-2", 1, "omitted"),
            ],
        )
    )
    current = service.load_current_snapshot()
    assert current is not None and current.belief_revision == 3
    assert current.ships[0].outcome_counts == {
        "clean": 1,
        "altered": 0,
        "omitted": 1,
    }

    restarted = ReportingReliabilityService.create(
        "mission-1",
        (1,),
        store,
        transport,
        context_topic="planning-evidence",
        clock=lambda: NOW,
    )
    restarted.ingest_environment_tick(
        _tick(2, [_check("check-1", 1, "clean"), _check("check-2", 1, "omitted")])
    )
    assert restarted.load_current_snapshot().to_dict() == current.to_dict()
