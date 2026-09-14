from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.reporting_reliability import (
    FileReportingReliabilityStore,
    ReportingReliabilityService,
)
from onr.ports.transport import Subscription

NOW = "2026-09-14T00:00:00Z"
spec = importlib.util.spec_from_file_location("prepare_reporting_prior", Path(__file__).parents[1] / "scripts/prepare_reporting_prior.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_prior_install_publishes_initial_event_through_existing_outbox(tmp_path: Path) -> None:
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({"ship_corruption_probabilities": {"1": 0.0, "2": 0.9}}))
    bundle, storage = tmp_path / "bundle", tmp_path / "storage"
    helper.prepare(truth, bundle, mission_id="mission-test", flattening=0.0, created_at=NOW)
    helper.install(bundle, storage, mission_id="mission-test")
    store = FileReportingReliabilityStore(storage)
    snapshot, _, pending = store.load("mission-test")
    assert pending is not None
    transport = InProcessTransport((Subscription("context-coordination", "mission-test", "normalized-plans"),))
    service = ReportingReliabilityService.create("mission-test", (1, 2), store, transport, clock=lambda: NOW)
    assert service.load_current_snapshot() == snapshot
    assert [s.mean for s in snapshot.ships] == pytest.approx([0, .9])
    assert transport.latest_event("normalized-plans", "mission-test").event_id == pending["event_id"]
    assert store.load("mission-test")[2] is None
    assert json.loads((storage / "diagnostic-prior.json").read_text())["diagnostic"] is True
    with pytest.raises(ValueError, match="replace"):
        helper.install(bundle, storage, mission_id="mission-test")


def test_prior_install_rejects_cross_mission_and_snapshot_mismatch(tmp_path: Path) -> None:
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({"ship_corruption_probabilities": {"1": .9}}))
    bundle = tmp_path / "bundle"
    helper.prepare(truth, bundle, mission_id="mission-test", flattening=.5, created_at=NOW)
    with pytest.raises(ValueError, match="Mission"):
        helper.install(bundle, tmp_path / "wrong", mission_id="other")
    other = tmp_path / "other"
    helper.prepare(truth, other, mission_id="mission-test", flattening=0, created_at=NOW)
    (bundle / "belief.json").write_bytes((other / "belief.json").read_bytes())
    with pytest.raises(ValueError, match="match its checkpoint"):
        helper.install(bundle, tmp_path / "mismatch", mission_id="mission-test")
