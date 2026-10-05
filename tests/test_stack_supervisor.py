"""Stack Supervisor lifecycle against fake Python services (issue #75 Phase 1)."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from onr.runtime_host import RuntimeHost, create_app
from onr.runtime_host.run_root import RunRoot
from onr.runtime_host.stack import (
    PrepStep,
    ReadinessProbe,
    ServiceSpec,
    StackFailure,
    StackPlan,
    StackSupervisor,
    plan_mission_run,
)
from tests.support import launcher_goldens
from tests.test_runtime_host import _clock, _config, _ids
from tests.test_runtime_host_v12 import activate, run_root, setup_host

REPOSITORY = Path(__file__).parents[1]
CONTRACT = REPOSITORY / "docs/design/operator-console/contract"

pytestmark = pytest.mark.skipif(
    not launcher_goldens.PHYSICAL_ROOT.is_dir(),
    reason="needs the sibling onr_physical_runtime checkout",
)

# A long-running service: records SIGTERM in the order file, then becomes
# ready by creating its ready file. ``mode`` = ready | crash | hang | stubborn
# | finish (ready, then exits 0) | finish-crash (ready, then exits 3).
_SERVICE = r"""
import pathlib, signal, sys, time
name, mode, ready, order = sys.argv[1:5]
def stop(*_):
    with open(order, "a") as log:
        log.write(name + "\n")
    sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "stubborn" else stop)
print(f"{name} starting", flush=True)
if mode == "crash":
    print(f"{name} fatal: boom", flush=True)
    sys.exit(3)
if mode != "hang":
    pathlib.Path(ready).touch()
    print(f"{name} ready", flush=True)
if mode.startswith("finish"):
    time.sleep(0.2)
    print(f"{name} done", flush=True)
    sys.exit(3 if mode == "finish-crash" else 0)
while True:
    time.sleep(0.05)
"""


class FakeClock:
    """Monotonic clock that advances only when the supervisor sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        time.sleep(0.01)


def _service(
    tmp_path: Path, name: str, mode: str, *, grace: float = 5.0
) -> ServiceSpec:
    ready = tmp_path / f"{name}.ready"
    return ServiceSpec(
        name=name,
        argv=(
            sys.executable,
            "-c",
            _SERVICE,
            name,
            mode,
            str(ready),
            str(tmp_path / "order"),
        ),
        env={},
        cwd=tmp_path,
        readiness=(
            ReadinessProbe("file", str(ready), f"the {name}", f"{name} was not ready"),
        ),
        timeout_seconds=30.0,
        stop_grace_seconds=grace,
    )


def _plan(
    tmp_path: Path, *services: ServiceSpec, post_ready: tuple[PrepStep, ...] = ()
) -> StackPlan:
    plan = plan_mission_run(
        preset_id="mission1-harbor",
        stack=None,
        mission_id="mission-supervisor-test",
        run_root=RunRoot(tmp_path / "run"),
        repo_root=REPOSITORY,
        viewer_port=5130,
    )
    return replace(plan, services=services, prepare=(), post_ready=post_ready)


def _supervisor(plan: StackPlan) -> StackSupervisor:
    clock = FakeClock()
    return StackSupervisor(
        plan, clock=clock, sleep=clock.sleep, poll_interval_seconds=0.5
    )


def _status(plan: StackPlan) -> dict[str, dict[str, object]]:
    document = json.loads(plan.run_root.stack_status.read_text())
    return {entry["name"]: entry for entry in document["services"]}


def _order(tmp_path: Path) -> list[str]:
    path = tmp_path / "order"
    return path.read_text().split() if path.exists() else []


def test_services_start_in_order_and_stop_in_reverse(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        _service(tmp_path, "engine", "ready"),
        _service(tmp_path, "runtime", "ready"),
    )
    supervisor = _supervisor(plan)

    supervisor.start()
    status = _status(plan)
    assert [status[name]["state"] for name in ("engine", "runtime")] == [
        "ready",
        "ready",
    ]
    assert status["runtime"]["last_line"] == "runtime ready"
    assert status["engine"]["log_artifact_id"] == "service-log-engine"
    assert (
        (plan.run_root.services_dir / "engine.log")
        .read_text()
        .startswith("engine starting")
    )
    supervisor.begin_closed_loop()
    assert supervisor.poll() is None

    supervisor.stop()
    assert _order(tmp_path) == ["runtime", "engine"]
    status = _status(plan)
    assert {status[name]["state"] for name in ("engine", "runtime", "closed-loop")} == {
        "stopped"
    }
    assert status["engine"]["exit_code"] == 0


def test_status_entries_match_the_operator_stack_contract(tmp_path: Path) -> None:
    example = json.loads(
        (CONTRACT / "v1.2/mission-run-operator-stack.response.json").read_text()
    )
    plan = _plan(tmp_path, _service(tmp_path, "physical-runtime", "ready"))
    with _supervisor(plan) as supervisor:
        supervisor.start()
        document = json.loads(plan.run_root.stack_status.read_text())

    v12_keys = set(example["stack"]["services"][0])
    assert [set(entry) for entry in document["services"]] == [
        v12_keys | {"ready_timeout_seconds"},
        v12_keys,
    ]
    assert document["services"][0]["ready_timeout_seconds"] == 30.0
    assert "step" not in document
    assert document["toggles"] == example["stack"]["toggles"]
    assert document["preset_id"] == "mission1-harbor"
    closed_loop = document["services"][-1]
    assert (closed_loop["name"], closed_loop["log_artifact_id"]) == (
        "closed-loop",
        "worker-log",
    )


def test_a_starting_service_names_the_readiness_probe_it_still_waits_for(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "initial-update.json"
    service = replace(
        _service(tmp_path, "physical-runtime", "hang"),
        readiness=(
            ReadinessProbe("file", str(marker), "the initial update", "no update"),
            ReadinessProbe("http", "http://viewer", "the viewer", "no viewer"),
        ),
    )
    plan = _plan(tmp_path, service)
    observed: list[object] = []

    def viewer_probe(_url: str) -> bool:
        observed.append(_status(plan)["physical-runtime"].get("waiting_for"))
        marker.touch()
        return len(observed) == 3

    clock = FakeClock()
    with StackSupervisor(
        plan, probe_http=viewer_probe, clock=clock, sleep=clock.sleep
    ) as supervisor:
        supervisor.start()
        ready = _status(plan)["physical-runtime"]

    # Probes run in order: the initial update is pending until the marker
    # exists, then only the viewer is.
    assert observed == ["the initial update", "the initial update", "the viewer"]
    assert ready["state"] == "ready"
    assert "waiting_for" not in ready


def test_a_running_prep_step_is_reported_until_it_finishes(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    step = PrepStep(
        name="mission1-surveillance-views",
        argv=(
            sys.executable,
            "-c",
            "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])",
            str(tmp_path / "run" / "stack-status.json"),
            str(seen),
        ),
        cwd=tmp_path,
        timeout_seconds=120.0,
    )
    plan = _plan(
        tmp_path, _service(tmp_path, "physical-runtime", "ready"), post_ready=(step,)
    )

    with _supervisor(plan) as supervisor:
        supervisor.start()
        after = json.loads(plan.run_root.stack_status.read_text())

    seen_document = json.loads(seen.read_text())
    during = seen_document["step"]
    assert {key: during[key] for key in ("name", "stage", "timeout_seconds")} == {
        "name": "mission1-surveillance-views",
        "stage": "post_ready",
        "timeout_seconds": 120.0,
    }
    assert isinstance(during["started_at"], str)
    assert "step" not in after
    # v1.5: the history entry is written when the step starts and finishes.
    assert seen_document["steps"] == [
        {
            "name": "mission1-surveillance-views",
            "stage": "post_ready",
            "state": "running",
            "started_at": during["started_at"],
            "finished_at": None,
            "log_artifact_id": "service-log-mission1-surveillance-views",
            "timeout_seconds": 120.0,
        }
    ]
    (finished,) = after["steps"]
    assert finished["state"] == "done"
    assert finished["started_at"] == during["started_at"]
    assert isinstance(finished["finished_at"], str)
    assert finished["finished_at"] >= finished["started_at"]


def test_prep_step_history_survives_a_host_restart(tmp_path: Path) -> None:
    host, client, _ = setup_host(tmp_path)
    activate(client)
    step = PrepStep(
        name="airsim-fixture",
        argv=(sys.executable, "-c", "print('fixture copied')"),
        cwd=tmp_path,
        timeout_seconds=60.0,
    )
    plan = replace(
        _plan(tmp_path, _service(tmp_path, "airsim-engine", "ready")),
        run_root=run_root(host),
        prepare=(step,),
    )
    with _supervisor(plan) as supervisor:
        supervisor.start()

    # A new Host keeps no in-memory stack state: it reads stack-status.json.
    restarted = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=lambda _context: None,
        launch_worker=lambda _worker: None,
    )
    client = TestClient(create_app(host=restarted), client=("127.0.0.1", 50000))
    stack = client.get("/api/v1/mission-runs/run-1/operator-view?section=stack").json()[
        "stack"
    ]
    (record,) = stack["steps"]
    assert {
        key: record[key] for key in ("name", "stage", "state", "log_artifact_id")
    } == {
        "name": "airsim-fixture",
        "stage": "prepare",
        "state": "done",
        "log_artifact_id": "service-log-airsim-fixture",
    }
    assert record["finished_at"] >= record["started_at"]
    # The prep-step log is served through the allowlisted service-log route.
    content = client.get(
        "/api/v1/mission-runs/run-1/artifacts/service-log-airsim-fixture/content"
    ).json()
    assert content["content"] == "fixture copied\n"
    example = json.loads(
        (
            CONTRACT / "v1.5/mission-run-operator-stack.post-ready.response.json"
        ).read_text()
    )
    assert set(record) == set(example["stack"]["steps"][0])


def test_crash_before_ready_fails_the_stack_and_tears_down_started_services(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path,
        _service(tmp_path, "engine", "ready"),
        _service(tmp_path, "perception", "crash"),
    )
    supervisor = _supervisor(plan)

    with pytest.raises(StackFailure) as failure:
        supervisor.start()

    assert failure.value.terminal_detail() == {
        "kind": "stack_failed",
        "service": "perception",
        "message": "exited with status 3",
        "log_artifact_id": "service-log-perception",
    }
    status = _status(plan)
    assert (status["perception"]["state"], status["perception"]["exit_code"]) == (
        "failed",
        3,
    )
    assert status["perception"]["importance"] == "critical"
    assert status["perception"]["last_line"] == "perception fatal: boom"
    assert status["engine"]["state"] == "stopped"
    assert _order(tmp_path) == ["engine"]


def test_service_that_never_becomes_ready_times_out(tmp_path: Path) -> None:
    plan = _plan(tmp_path, _service(tmp_path, "perception", "hang"))
    supervisor = _supervisor(plan)

    with pytest.raises(StackFailure) as failure:
        supervisor.start()

    assert failure.value.service == "perception"
    assert failure.value.message == "perception was not ready within 30 seconds"
    assert _status(plan)["perception"]["state"] == "failed"
    assert _order(tmp_path) == ["perception"]


def test_readiness_timeout_names_the_failed_service_and_its_log(
    tmp_path: Path,
) -> None:
    """The failure card's data: failed service, its log Artifact, Run Root."""

    def worker(context) -> None:
        plan = replace(
            _plan(tmp_path, _service(tmp_path, "perception", "hang")),
            run_root=context.run_root,
        )
        _supervisor(plan).start()

    host, client, pending = setup_host(tmp_path, worker)
    activate(client)
    pending.pop()()

    record = client.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert (record["status"], record["terminal_classification"]) == (
        "failed",
        "stack_failed",
    )
    assert record["terminal_detail"] == {
        "kind": "stack_failed",
        "service": "perception",
        "message": "perception was not ready within 30 seconds",
        "log_artifact_id": "service-log-perception",
    }
    example = json.loads(
        (CONTRACT / "v1.5/mission-runs.current.stack-failed.response.json").read_text()
    )
    assert set(record["terminal_detail"]) == set(
        example["mission_run"]["terminal_detail"]
    )
    # The named log is the allowlisted Artifact the card's `l` opens.
    content = client.get(
        "/api/v1/mission-runs/run-1/artifacts/service-log-perception/content"
    ).json()
    assert content["content"] == "perception starting\n"
    overview = client.get(
        "/api/v1/mission-runs/run-1/operator-view?section=overview"
    ).json()["overview"]
    assert overview["run_root"] == str(run_root(host).path)
    stack = client.get("/api/v1/mission-runs/run-1/operator-view?section=stack").json()[
        "stack"
    ]
    (perception,) = [item for item in stack["services"] if item["name"] == "perception"]
    assert (perception["state"], perception["log_artifact_id"]) == (
        "failed",
        "service-log-perception",
    )


def test_teardown_kills_a_service_that_ignores_sigterm(tmp_path: Path) -> None:
    plan = _plan(tmp_path, _service(tmp_path, "engine", "stubborn", grace=0.2))
    supervisor = _supervisor(plan)
    supervisor.start()

    supervisor.stop()

    status = _status(plan)["engine"]
    assert (status["state"], status["exit_code"]) == ("stopped", -signal.SIGKILL)


def _recording_writes(
    supervisor: StackSupervisor, plan: StackPlan
) -> list[dict[str, object]]:
    """Every ``stack-status.json`` document the supervisor writes from now on."""

    documents: list[dict[str, object]] = []
    write = supervisor._write_status

    def recording() -> None:
        write()
        documents.append(json.loads(plan.run_root.stack_status.read_text()))

    supervisor._write_status = recording  # type: ignore[method-assign]
    return documents


def test_teardown_writes_each_service_stopping_then_stopped_in_reverse_order(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path,
        _service(tmp_path, "engine", "stubborn", grace=0.2),
        _service(tmp_path, "runtime", "ready"),
        _service(tmp_path, "visualizer", "ready"),
    )
    supervisor = _supervisor(plan)
    supervisor.start()
    supervisor.begin_closed_loop()
    documents = _recording_writes(supervisor, plan)

    supervisor.stop()

    # Each intermediate status is on disk before the next service is signalled.
    seen = {
        name: "ready" for name in ("engine", "runtime", "visualizer", "closed-loop")
    }
    transitions: list[tuple[str, str, object]] = []
    for document in documents:
        for entry in document["services"]:  # type: ignore[union-attr]
            if entry["state"] != seen[entry["name"]]:
                seen[entry["name"]] = entry["state"]
                transitions.append(
                    (entry["name"], entry["state"], entry.get("stop_mode"))
                )
    assert transitions == [
        ("visualizer", "stopping", None),
        ("visualizer", "stopped", "graceful"),
        ("runtime", "stopping", None),
        ("runtime", "stopped", "graceful"),
        ("engine", "stopping", None),
        ("engine", "stopped", "forced"),
        ("closed-loop", "stopped", None),
    ]
    assert _order(tmp_path) == ["visualizer", "runtime"]

    # While stopping: when SIGTERM was sent and the grace before SIGKILL.
    stopping = next(
        entry
        for document in documents
        for entry in document["services"]  # type: ignore[union-attr]
        if entry["name"] == "engine" and entry["state"] == "stopping"
    )
    assert stopping["stop_grace_seconds"] == 0.2
    assert isinstance(stopping["stop_requested_at"], str)
    assert "stop_mode" not in stopping and "stopped_at" not in stopping

    first, last = documents[0]["teardown"], documents[-1]["teardown"]
    assert first == {
        "started_at": first["started_at"],  # type: ignore[index]
        "finished_at": None,
        "stop_order": ["visualizer", "runtime", "engine"],
    }
    assert last["finished_at"] >= last["started_at"]  # type: ignore[index]
    final = _status(plan)
    assert final["engine"]["exit_code"] == -signal.SIGKILL
    assert final["engine"]["stopped_at"] >= final["engine"]["stop_requested_at"]  # type: ignore[operator]
    assert "stop_mode" not in final["closed-loop"]

    # Idempotent: a second stop changes nothing it already recorded.
    supervisor.stop()
    assert _status(plan) == final


def test_a_service_that_already_failed_keeps_its_state_through_teardown(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, _service(tmp_path, "perception", "hang"))
    supervisor = _supervisor(plan)
    documents = _recording_writes(supervisor, plan)

    with pytest.raises(StackFailure):
        supervisor.start()

    states = [
        entry["state"]
        for document in documents
        for entry in document["services"]  # type: ignore[union-attr]
        if entry["name"] == "perception"
    ]
    assert "stopping" not in states and states[-1] == "failed"
    assert "stop_mode" not in _status(plan)["perception"]


@pytest.mark.parametrize(
    ("failed", "cancelled", "state"),
    [(False, False, "exited"), (True, False, "failed"), (True, True, "stopped")],
)
def test_an_owner_cancelled_closed_loop_is_stopped_not_failed(
    tmp_path: Path, failed: bool, cancelled: bool, state: str
) -> None:
    plan = _plan(tmp_path)
    supervisor = _supervisor(plan)
    supervisor.start()
    supervisor.begin_closed_loop()
    supervisor.end_closed_loop(failed=failed, cancelled=cancelled)
    assert _status(plan)["closed-loop"]["state"] == state


def _engine_status(host: RuntimeHost, *, started: bool) -> None:
    root = run_root(host)
    root.services_dir.mkdir(parents=True, exist_ok=True)
    root.stack_status.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "preset_id": "mission1-harbor",
                "toggles": {},
                "services": [
                    {
                        "name": "airsim-engine",
                        "state": "stopped",
                        "started_at": "2026-08-27T14:00:01Z" if started else None,
                        "log_artifact_id": "service-log-airsim-engine",
                        "importance": "routine",
                    }
                ],
                "teardown": {
                    "started_at": "2026-08-27T14:09:00Z",
                    "finished_at": "2026-08-27T14:09:06Z",
                    "stop_order": ["airsim-engine"],
                },
            }
        )
    )


def _teardown(client: TestClient) -> dict[str, object]:
    return client.get("/api/v1/mission-runs/run-1/operator-view?section=stack").json()[
        "stack"
    ]["teardown"]


def test_harbor_config_restoration_is_confirmed_only_by_an_engine_or_guardian_report(
    tmp_path: Path,
) -> None:
    host, client, _ = setup_host(tmp_path)
    activate(client)
    root = run_root(host)

    _engine_status(host, started=False)
    receipt = _teardown(client)
    assert receipt["harbor_config"] == {"state": "not_applicable", "reported_by": None}
    assert receipt["worker"] == "running"

    _engine_status(host, started=True)
    engine_log = root.service_log("airsim-engine")
    engine_log.write_text("Engine ready and frozen: ready.json\n")
    assert _teardown(client)["harbor_config"] == {
        "state": "unknown",
        "reported_by": None,
    }

    # The engine prints its line on every exit path; a failed restore voids it.
    engine_log.write_text(
        "Engine stopped; configuration restored\n"
        "RuntimeError: Engine configuration restoration failed\n"
    )
    assert _teardown(client)["harbor_config"]["state"] == "unknown"  # type: ignore[index]

    root.engine.mkdir(parents=True, exist_ok=True)
    (root.engine / "guardian.log").write_text(
        "Guardian: engine stopped; configuration restored\n"
    )
    assert _teardown(client)["harbor_config"] == {
        "state": "confirmed",
        "reported_by": "guardian",
    }

    engine_log.write_text("Engine stopped; configuration restored\n")
    assert _teardown(client)["harbor_config"] == {
        "state": "confirmed",
        "reported_by": "engine",
    }

    host._transition("run-1", "cancelled", terminal_classification="cancelled_by_owner")
    receipt = _teardown(client)
    assert receipt["worker"] == "stopped"
    example = json.loads(
        (
            CONTRACT / "v1.5/mission-run-operator-stack.cancelled.response.json"
        ).read_text()
    )
    assert set(receipt) == set(example["stack"]["teardown"])


def test_poll_reports_a_required_service_that_died_after_ready(tmp_path: Path) -> None:
    plan = _plan(tmp_path, _service(tmp_path, "physical-runtime", "ready"))
    with _supervisor(plan) as supervisor:
        supervisor.start()
        pid = _status(plan)["physical-runtime"]["pid"]
        assert isinstance(pid, int)
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        failure = supervisor.poll()
        while failure is None and time.monotonic() < deadline:
            time.sleep(0.05)
            failure = supervisor.poll()

    assert failure is not None
    assert (failure.service, failure.message) == (
        "physical-runtime",
        "exited with status -9",
    )
    assert _status(plan)["physical-runtime"]["state"] == "failed"


def _poll_until_exit(supervisor: StackSupervisor, plan: StackPlan, name: str) -> object:
    deadline = time.monotonic() + 30
    failure = supervisor.poll()
    while _status(plan)[name]["exit_code"] is None and time.monotonic() < deadline:
        time.sleep(0.05)
        failure = supervisor.poll()
    return failure


def _joint34_worker(tmp_path: Path, mode: str) -> ServiceSpec:
    """Joint 3+4's real mission4-worker spec, running the fake service."""
    plan = plan_mission_run(
        preset_id="joint34",
        stack=None,
        mission_id="mission-supervisor-test",
        run_root=RunRoot(tmp_path / "joint34"),
        repo_root=REPOSITORY,
        viewer_port=5130,
    )
    worker = next(spec for spec in plan.services if spec.name == "mission4-worker")
    fake = _service(tmp_path, "mission4-worker", mode)
    return replace(worker, argv=fake.argv, cwd=fake.cwd, readiness=fake.readiness)


def test_mission4_worker_finishing_its_script_does_not_fail_the_stack(
    tmp_path: Path,
) -> None:
    # Joint 3+4 keeps running Mission 3 after the worker has played its script
    # and the Mission 4 search closed (run-eefdfe20: failed 40 min in).
    plan = _plan(
        tmp_path,
        _service(tmp_path, "physical-runtime", "ready"),
        _joint34_worker(tmp_path, "finish"),
    )
    with _supervisor(plan) as supervisor:
        supervisor.start()
        supervisor.begin_closed_loop()
        failure = _poll_until_exit(supervisor, plan, "mission4-worker")
        assert failure is None
        worker = _status(plan)["mission4-worker"]
        assert (worker["state"], worker["exit_code"], worker["importance"]) == (
            "exited",
            0,
            "routine",
        )
        assert supervisor.poll() is None


@pytest.mark.parametrize(
    ("service", "mode", "message"),
    [
        ("mission4-worker", "finish-crash", "exited with status 3"),
        ("physical-runtime", "finish", "exited with status 0"),
    ],
)
def test_a_crashing_worker_or_any_exit_of_another_required_service_fails_the_stack(
    tmp_path: Path, service: str, mode: str, message: str
) -> None:
    spec = (
        _joint34_worker(tmp_path, mode)
        if service == "mission4-worker"
        else _service(tmp_path, service, mode)
    )
    plan = _plan(tmp_path, spec)
    with _supervisor(plan) as supervisor:
        supervisor.start()
        failure = _poll_until_exit(supervisor, plan, service)

    assert failure is not None
    assert (failure.service, failure.message) == (service, message)


def test_failed_post_ready_step_is_a_stack_failure(tmp_path: Path) -> None:
    step = PrepStep(
        name="mission1-public-input",
        argv=(
            sys.executable,
            "-c",
            "import sys; print('no initial update'); sys.exit(4)",
        ),
        cwd=tmp_path,
    )
    plan = _plan(
        tmp_path, _service(tmp_path, "physical-runtime", "ready"), post_ready=(step,)
    )

    with pytest.raises(StackFailure) as failure:
        _supervisor(plan).start()

    assert failure.value.service == "mission1-public-input"
    assert failure.value.message == "exited with status 4: no initial update"
    assert _status(plan)["physical-runtime"]["state"] == "stopped"
    (record,) = json.loads(plan.run_root.stack_status.read_text())["steps"]
    assert (record["name"], record["state"]) == ("mission1-public-input", "failed")
    assert isinstance(record["finished_at"], str)
