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

REPOSITORY = Path(__file__).parents[1]
CONTRACT = REPOSITORY / "docs/design/operator-console/contract/v1.2"

pytestmark = pytest.mark.skipif(
    not launcher_goldens.PHYSICAL_ROOT.is_dir(),
    reason="needs the sibling onr_physical_runtime checkout",
)

# A long-running service: records SIGTERM in the order file, then becomes
# ready by creating its ready file. ``mode`` = ready | crash | hang | stubborn.
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
        (CONTRACT / "mission-run-operator-stack.response.json").read_text()
    )
    plan = _plan(tmp_path, _service(tmp_path, "physical-runtime", "ready"))
    with _supervisor(plan) as supervisor:
        supervisor.start()
        document = json.loads(plan.run_root.stack_status.read_text())

    expected_keys = example["stack"]["services"][0].keys()
    assert [entry.keys() for entry in document["services"]] == [
        expected_keys,
        expected_keys,
    ]
    assert document["toggles"] == example["stack"]["toggles"]
    assert document["preset_id"] == "mission1-harbor"
    closed_loop = document["services"][-1]
    assert (closed_loop["name"], closed_loop["log_artifact_id"]) == (
        "closed-loop",
        "worker-log",
    )


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


def test_teardown_kills_a_service_that_ignores_sigterm(tmp_path: Path) -> None:
    plan = _plan(tmp_path, _service(tmp_path, "engine", "stubborn", grace=0.2))
    supervisor = _supervisor(plan)
    supervisor.start()

    supervisor.stop()

    status = _status(plan)["engine"]
    assert (status["state"], status["exit_code"]) == ("stopped", -signal.SIGKILL)


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
