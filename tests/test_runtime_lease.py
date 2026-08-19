from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path
import time

import pytest

from onr.runtime.lease import RuntimeLease, RuntimeLeaseStore


def _race_start(root: str, ready: object, results: object) -> None:
    store = RuntimeLeaseStore(Path(root), timeout=10, heartbeat_interval=1)
    ready.wait()  # type: ignore[attr-defined]
    try:
        lease = store.start()
    except RuntimeError:
        results.put(("rejected", None))  # type: ignore[attr-defined]
    else:
        results.put(("started", lease.session_id))  # type: ignore[attr-defined]


def test_lease_active_touch_stop_and_atomic_file(tmp_path: Path) -> None:
    store = RuntimeLeaseStore(tmp_path / "runtime", timeout=10, heartbeat_interval=1)
    lease = store.start(session_id="owner")
    assert lease.status == "active" and store.is_active()
    before = lease.last_seen
    time.sleep(0.002)
    touched = store.touch()
    assert touched is not None and touched.last_seen > before
    assert json.loads(store.path.read_text())["session_id"] == lease.session_id
    assert not list(store.root.glob("*.tmp"))
    stopped = store.stop()
    assert stopped is not None and stopped.status == "stopped"
    assert not store.is_active()


def test_start_touch_and_stop_are_owner_aware(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    owner = RuntimeLeaseStore(root, timeout=10, heartbeat_interval=1)
    observer = RuntimeLeaseStore(root, timeout=10, heartbeat_interval=1)
    active = owner.start(session_id="session-a")
    assert observer.touch() is None and observer.stop() is None
    with pytest.raises(RuntimeError, match="another active session"):
        observer.start(session_id="session-b")

    foreign = RuntimeLease("session-b", 123, active.started_at, active.last_seen, "active")
    root.joinpath("lease.json").write_text(json.dumps(foreign.to_dict()), encoding="utf-8")
    assert owner.touch() is None and owner.stop() is None
    assert json.loads(root.joinpath("lease.json").read_text())["session_id"] == "session-b"


def test_process_lock_serializes_competing_starts(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_race_start, args=(str(tmp_path / "runtime"), barrier, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=5) for _ in processes]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    assert sorted(outcome for outcome, _ in outcomes) == ["rejected", "started"]
    winner = next(session_id for outcome, session_id in outcomes if outcome == "started")
    serialized = json.loads((tmp_path / "runtime" / "lease.json").read_text())
    assert serialized["session_id"] == winner and serialized["status"] == "active"
    observer = RuntimeLeaseStore(tmp_path / "runtime", timeout=10, heartbeat_interval=1)
    assert observer.touch() is None and observer.stop() is None


def test_direct_runtime_composition_uses_canonical_lease_root(tmp_path: Path) -> None:
    from onr.adapters.inprocess_transport import InProcessTransport
    from onr.runtime.composition import RuntimeComposition
    from onr.runtime.config import (
        HeartbeatsConfig, LLMConfig, PlannerConfig, PlannersConfig, RuntimeConfig,
        ServicesConfig, StorageConfig, TransportConfig,
    )

    planner = PlannerConfig(Path("planner"), 1)
    config = RuntimeConfig(
        LLMConfig("test", "http://localhost", "model", "key", 0),
        PlannersConfig(planner, planner), HeartbeatsConfig(1, 1),
        TransportConfig("inprocess", tmp_path / "transport"),
        StorageConfig(tmp_path / "storage"),
        ServicesConfig("hyper", "control", "context", "fsm", "planner"),
    )
    runtime = RuntimeComposition(config, InProcessTransport())
    assert runtime.lease is not None
    assert runtime.lease.path == tmp_path / "storage" / "runtime" / "lease.json"


@pytest.mark.parametrize(
    "contents",
    ["not-json", "[]", '{"session_id":"only"}', '{"session_id":null,"pid":1,"started_at":"x","last_seen":"x","status":"active"}'],
)
def test_missing_or_corrupt_lease_is_inactive(tmp_path: Path, contents: str) -> None:
    store = RuntimeLeaseStore(tmp_path / "runtime", timeout=10, heartbeat_interval=1)
    assert store.read() is None and not store.is_active()
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text(contents, encoding="utf-8")
    assert store.read() is None and not store.is_active()


def test_inspect_absent_lease_does_not_create_runtime_storage(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = RuntimeLeaseStore(root, timeout=10, heartbeat_interval=1)

    assert store.inspect() is None
    assert not root.exists()


def test_stopped_and_stale_leases_are_inactive_and_replaceable(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = RuntimeLeaseStore(root, timeout=0.05, heartbeat_interval=0.01)
    old = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    stale = RuntimeLease("old", 123, old, old, "active")
    root.mkdir(parents=True)
    store.path.write_text(json.dumps(stale.to_dict()), encoding="utf-8")
    assert store.read() is not None and store.read().status == "stale"  # type: ignore[union-attr]
    assert not store.is_active()
    replacement = store.start(session_id="replacement")
    assert replacement.session_id == "replacement" and store.is_active()
    store.stop()
    assert store.read() is not None and store.read().status == "stopped"  # type: ignore[union-attr]
    assert not store.is_active()


def test_session_periodically_touches_and_cleans_up_on_error(tmp_path: Path) -> None:
    store = RuntimeLeaseStore(tmp_path / "runtime", timeout=0.12, heartbeat_interval=0.02)
    with pytest.raises(RuntimeError, match="mission failed"):
        with store.session() as lease:
            initial = lease.last_seen
            time.sleep(0.07)
            current = store.read()
            assert current is not None and current.status == "active"
            assert current.last_seen > initial
            raise RuntimeError("mission failed")
    final = store.read()
    assert final is not None and final.status == "stopped"
    assert not store.is_active()
