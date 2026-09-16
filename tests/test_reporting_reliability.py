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
from onr.contracts.prior_knowledge import PriorKnowledge, PriorKnowledgeClaim
from onr.ports.transport import Subscription

NOW = "2026-09-03T00:00:00+10:00"


def test_diagnostic_prior_endpoints_and_gradual_flattening() -> None:
    targets = {1: 0.0, 2: 0.9}
    ordinary = ReportingReliabilityManager("mission-1", targets).snapshot(
        input_event_id="initial", input_revision=0, created_at=NOW
    )
    for flattening in (0.0, 0.25, 0.5, 0.75, 1.0):
        manager = ReportingReliabilityManager.with_diagnostic_prior(
            "mission-1", targets, flattening=flattening
        )
        snapshot = manager.snapshot(
            input_event_id="initial", input_revision=0, created_at=NOW
        )
        for ship, shared in zip(snapshot.ships, ordinary.ships):
            assert ship.mean == pytest.approx(
                (1 - flattening) * targets[ship.entity_id] + flattening * shared.mean
            )
            if flattening == 0:
                assert ship.variance == pytest.approx(0, abs=1e-14)
        assert snapshot.omission == ordinary.omission
        if flattening == 1:
            assert snapshot == ordinary


def test_diagnostic_prior_updates_and_checkpoint_provenance(tmp_path: Path) -> None:
    manager = ReportingReliabilityManager.with_diagnostic_prior(
        "mission-1", {1: 0.0, 2: 0.9}, flattening=0.5
    )
    before = manager.snapshot(
        input_event_id="initial", input_revision=0, created_at=NOW
    )
    after = manager.update_checks(
        [_check("clean", 1, "clean"), _check("bad", 2, "altered")],
        input_event_id="tick-1",
        input_revision=1,
        created_at=NOW,
    )
    assert after.ships[0].mean < before.ships[0].mean
    assert after.ships[1].mean > before.ships[1].mean
    store = FileReportingReliabilityStore(tmp_path)
    store.save(after, manager.checkpoint(), None)
    saved, checkpoint, _ = store.load("mission-1")
    restored = ReportingReliabilityManager.from_checkpoint(checkpoint)
    assert restored.checkpoint() == manager.checkpoint()
    assert checkpoint.configuration["diagnostic_prior"]["flattening"] == 0.5
    assert saved == after
    assert (
        restored.update_checks(
            [_check("bad", 2, "altered")],
            input_event_id="tick-2",
            input_revision=2,
            created_at=NOW,
        )
        is None
    )


@pytest.mark.parametrize(
    "targets,flattening",
    [({1: 0.9}, -0.1), ({1: 0.9}, 1.1), ({1: float("nan")}, 0.5), ({1: -1.0}, 0.5)],
)
def test_diagnostic_prior_rejects_invalid_probabilities(targets, flattening) -> None:
    with pytest.raises(ValueError):
        ReportingReliabilityManager.with_diagnostic_prior(
            "mission-1", targets, flattening=flattening
        )


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


def test_service_initializes_once_from_public_spatiotemporal_prior(
    tmp_path: Path,
) -> None:
    subscription = Subscription(
        "context-coordination", "mission-1", "planning-evidence"
    )
    transport = InProcessTransport((subscription,))
    store = FileReportingReliabilityStore(tmp_path)
    service = ReportingReliabilityService.create(
        "mission-1",
        (1, 2, 3),
        store,
        transport,
        context_topic="planning-evidence",
        clock=lambda: NOW,
    )
    prior = PriorKnowledge(
        belief_kind="reporting_reliability",
        claims=(
            PriorKnowledgeClaim(
                "hypothesis_cardinality",
                {"hypothesis": "anomalous_entity", "count": 1},
            ),
            PriorKnowledgeClaim(
                "spatiotemporal_priority",
                {
                    "start_time_s": 10.0,
                    "end_time_s": 20.0,
                    "north_min_m": 0.0,
                    "north_max_m": 100.0,
                    "east_min_m": 0.0,
                    "east_max_m": 100.0,
                },
            ),
        ),
    )
    public_environment = {
        "static_info": [
            {"entity_id": 1, "time": 12.0, "position": [10.0, 10.0, -25.0]},
            {"entity_id": 1, "time": 14.0, "position": [20.0, 20.0, -25.0]},
            {"entity_id": 2, "time": 16.0, "position": [30.0, 30.0, -25.0]},
            {"entity_id": 3, "time": 16.0, "position": [300.0, 300.0, -25.0]},
        ]
    }

    result = service.initialize_from_prior(prior, public_environment)

    assert result.status == "applied"
    assert result.matched_entity_ids == (1, 2)
    initialized = service.load_current_snapshot()
    assert initialized.belief_revision == 2
    assert (
        initialized.ships[0].mean
        > initialized.ships[1].mean
        > initialized.ships[2].mean
    )
    assert initialized.ships[2].mean > 0.0
    assert initialized.omission.mean == pytest.approx(0.5)

    repeated = service.initialize_from_prior(prior, public_environment)
    assert repeated.status == "already_applied"
    assert service.load_current_snapshot() == initialized

    restarted = ReportingReliabilityService.create(
        "mission-1",
        (1, 2, 3),
        store,
        transport,
        context_topic="planning-evidence",
        clock=lambda: NOW,
    )
    recovered = restarted.initialize_from_prior(prior, public_environment)
    assert recovered.status == "already_applied"
    assert restarted.load_current_snapshot() == initialized


def test_prior_does_not_identify_one_entity_or_reset_observed_evidence(
    tmp_path: Path,
) -> None:
    subscription = Subscription(
        "context-coordination", "mission-1", "planning-evidence"
    )
    transport = InProcessTransport((subscription,))
    service = ReportingReliabilityService.create(
        "mission-1",
        (1, 2),
        FileReportingReliabilityStore(tmp_path),
        transport,
        context_topic="planning-evidence",
        clock=lambda: NOW,
    )
    prior = PriorKnowledge(
        belief_kind="reporting_reliability",
        claims=(
            PriorKnowledgeClaim(
                "hypothesis_cardinality",
                {"hypothesis": "anomalous_entity", "count": 1},
            ),
            PriorKnowledgeClaim(
                "spatiotemporal_priority",
                {
                    "start_time_s": 10.0,
                    "end_time_s": 20.0,
                    "north_min_m": 0.0,
                    "north_max_m": 100.0,
                    "east_min_m": 0.0,
                    "east_max_m": 100.0,
                },
            ),
        ),
    )
    unique_environment = {
        "static_info": [
            {"entity_id": 1, "time": 12.0, "position": [10.0, 10.0, -25.0]}
        ]
    }

    rejected = service.initialize_from_prior(prior, unique_environment)

    assert rejected.status == "not_applied"
    assert service.load_current_snapshot().belief_revision == 1

    service.ingest_environment_tick(_tick(1, [_check("observed", 1, "clean")]))
    observed = service.load_current_snapshot()
    after_evidence = service.initialize_from_prior(
        prior,
        {
            "static_info": [
                {"entity_id": 1, "time": 12.0, "position": [10.0, 10.0, -25.0]},
                {"entity_id": 2, "time": 14.0, "position": [20.0, 20.0, -25.0]},
            ]
        },
    )
    assert after_evidence.status == "not_applied"
    assert service.load_current_snapshot() == observed


def test_soft_hypothesis_weights_are_relative_not_bayesian_parameters(
    tmp_path: Path,
) -> None:
    service = ReportingReliabilityService.create(
        "mission-1",
        (1, 2, 3),
        FileReportingReliabilityStore(tmp_path),
        InProcessTransport(
            (Subscription("context-coordination", "mission-1", "planning-evidence"),)
        ),
        context_topic="planning-evidence",
        clock=lambda: NOW,
    )
    prior = PriorKnowledge(
        "reporting_reliability",
        (
            PriorKnowledgeClaim(
                "hypothesis_cardinality",
                {"hypothesis": "anomalous_entity", "count": 1},
            ),
            PriorKnowledgeClaim(
                "hypothesis_weights",
                {
                    "weights": [
                        {"entity_id": 1, "weight": 3},
                        {"entity_id": 2, "weight": 1},
                    ]
                },
            ),
        ),
    )

    result = service.initialize_from_prior(prior, {"static_info": []})
    ships = service.load_current_snapshot().ships

    assert result.matched_entity_ids == (1, 2)
    assert ships[0].honest_probability == pytest.approx(0.325)
    assert ships[1].honest_probability == pytest.approx(0.775)
    assert ships[2].honest_probability == pytest.approx(0.9)


def test_cumulative_ledger_replay_and_checkpoint_recovery_are_idempotent() -> None:
    checks = (_check("check-1", 1, "altered"), _check("check-2", 1, "clean"))
    manager = ReportingReliabilityManager("mission-1", (1,))
    first = manager.update_checks(
        checks, input_event_id="tick-2", input_revision=2, created_at=NOW
    )
    assert first is not None
    assert (
        manager.update_checks(
            checks, input_event_id="tick-2-replay", input_revision=2, created_at=NOW
        )
        is None
    )

    recovered = ReportingReliabilityManager.from_checkpoint(manager.checkpoint())
    assert (
        recovered.snapshot(
            input_event_id="tick-2", input_revision=2, created_at=NOW
        ).to_dict()
        == first.to_dict()
    )
    assert (
        recovered.update_checks(
            checks, input_event_id="tick-3", input_revision=3, created_at=NOW
        )
        is None
    )


def test_service_processes_each_buffered_tick_and_recovers_without_duplicates(
    tmp_path: Path,
) -> None:
    subscription = Subscription(
        "context-coordination", "mission-1", "planning-evidence"
    )
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
