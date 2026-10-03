"""Run one Mission Run's Environment Stack inside the Run Worker (decision D1).

Services start in plan order as children in the caller's process group (so
the Host's process-group cancellation reaps them), each logging to
``services/<name>.log``. A service must pass its readiness probes before the
next starts. ``stack-status.json`` is rewritten atomically on every state
transition with the ``services[]`` entries of the operator-view ``stack``
section. Teardown runs in reverse order: SIGTERM, then SIGKILL after the
service's grace period (``live_engine`` uses its grace to stop Harbor and
restore ``environment.json``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Self

from onr.runtime_host.importance import service_importance
from onr.runtime_host.stack.builder import (
    PrepStep,
    ReadinessProbe,
    ServiceSpec,
    StackPlan,
)

CLOSED_LOOP_SERVICE = "closed-loop"
WORKER_LOG_ARTIFACT_ID = "worker-log"
_TAIL_BYTES = 4096


def service_log_artifact_id(name: str) -> str:
    return f"service-log-{name}"


class StackFailure(RuntimeError):
    """A required service crashed, timed out, or a prep step failed."""

    def __init__(self, service: str, message: str) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.message = message

    def terminal_detail(self) -> dict[str, str]:
        """The ``terminal_detail`` object for ``/mission-runs/current``."""

        return {
            "kind": "stack_failed",
            "service": self.service,
            "message": self.message,
        }


def http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class _ServiceState:
    name: str
    required: bool
    port: int | None
    log_artifact_id: str
    log_path: Path | None
    state: str = "pending"
    pid: int | None = None
    started_at: str | None = None
    ready_at: str | None = None
    exit_code: int | None = None

    def entry(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "state": self.state,
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "ready_at": self.ready_at,
            "exit_code": self.exit_code,
            "log_artifact_id": self.log_artifact_id,
            "last_line": _last_line(self.log_path),
            "importance": service_importance(
                self.state, required=self.required, exit_code=self.exit_code
            ),
        }


class StackSupervisor:
    """Start, watch and stop the services of one :class:`StackPlan`.

    Typical Run Worker use::

        with StackSupervisor(plan) as stack:
            stack.start()             # raises StackFailure (already torn down)
            stack.begin_closed_loop()
            ...                       # closed loop; stack.poll() between steps
            stack.end_closed_loop(failed=False)
    """

    def __init__(
        self,
        plan: StackPlan,
        *,
        probe_http: Callable[[str], bool] = http_ready,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = _utc_now,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.plan = plan
        self._probe_http = probe_http
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._poll_interval = poll_interval_seconds
        layout = plan.run_root
        self._states = {
            spec.name: _ServiceState(
                name=spec.name,
                required=spec.required,
                port=spec.port,
                log_artifact_id=service_log_artifact_id(spec.name),
                log_path=layout.service_log(spec.name),
            )
            for spec in plan.services
        }
        self._states[CLOSED_LOOP_SERVICE] = _ServiceState(
            name=CLOSED_LOOP_SERVICE,
            required=True,
            port=None,
            log_artifact_id=WORKER_LOG_ARTIFACT_ID,
            log_path=layout.worker_log,
        )
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._logs: dict[str, IO[bytes]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Run prep steps, start every service in order, then post-ready steps.

        On any failure the started services are torn down before
        :class:`StackFailure` propagates.
        """

        self.plan.run_root.create()
        self._write_status()
        try:
            for step in self.plan.prepare:
                self._run_step(step)
            for spec in self.plan.services:
                self._start_service(spec)
                self._await_ready(spec)
            for step in self.plan.post_ready:
                self._run_step(step)
        except BaseException:
            self.stop()
            raise

    def poll(self) -> StackFailure | None:
        """Record services that exited; return a failure for a required one."""

        failure: StackFailure | None = None
        changed = False
        for name, process in self._processes.items():
            state = self._states[name]
            code = process.poll()
            if code is None or state.state in {"exited", "failed", "stopped"}:
                continue
            state.exit_code = code
            state.state = "failed" if code != 0 else "exited"
            changed = True
            if state.required and failure is None:
                failure = StackFailure(name, f"exited with status {code}")
        if changed:
            self._write_status()
        return failure

    def begin_closed_loop(self) -> None:
        state = self._states[CLOSED_LOOP_SERVICE]
        state.state = "ready"
        state.started_at = state.ready_at = self._now()
        self._write_status()

    def end_closed_loop(self, *, failed: bool) -> None:
        state = self._states[CLOSED_LOOP_SERVICE]
        state.state = "failed" if failed else "exited"
        self._write_status()

    def stop(self) -> None:
        """Tear down running services in reverse start order (idempotent)."""

        for spec in reversed(self.plan.services):
            process = self._processes.get(spec.name)
            if process is None:
                continue
            state = self._states[spec.name]
            if process.poll() is None:
                _terminate(process, spec.stop_grace_seconds)
                if state.state not in {"failed", "exited"}:
                    state.state = "stopped"
            elif state.state not in {"failed", "exited", "stopped"}:
                state.state = "failed" if process.returncode != 0 else "exited"
            state.exit_code = process.returncode
            log = self._logs.pop(spec.name, None)
            if log is not None:
                log.close()
        loop = self._states[CLOSED_LOOP_SERVICE]
        if loop.state in {"starting", "ready"}:
            loop.state = "stopped"
        self._write_status()

    def status_payload(self) -> dict[str, object]:
        """The ``stack-status.json`` document."""

        request = self.plan.request
        return {
            "schema_version": 1,
            "preset_id": request.preset_id,
            "toggles": request.toggles.payload(),
            "updated_at": self._now(),
            "services": [state.entry() for state in self._states.values()],
        }

    # -- internals -----------------------------------------------------------

    def _start_service(self, spec: ServiceSpec) -> None:
        state = self._states[spec.name]
        log = self.plan.run_root.service_log(spec.name).open("ab")
        self._logs[spec.name] = log
        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                env={**os.environ, **spec.env},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        except OSError as error:
            state.state = "failed"
            self._write_status()
            raise StackFailure(spec.name, f"could not start: {error}") from error
        self._processes[spec.name] = process
        state.pid = process.pid
        state.state = "starting"
        state.started_at = self._now()
        self._write_status()

    def _await_ready(self, spec: ServiceSpec) -> None:
        state = self._states[spec.name]
        deadline = self._clock() + spec.timeout_seconds
        while True:
            failure = self.poll()
            if failure is not None:
                raise failure
            pending = [probe for probe in spec.readiness if not self._probe(probe)]
            if not pending:
                state.state = "ready"
                state.ready_at = self._now()
                self._write_status()
                return
            if self._clock() >= deadline:
                state.state = "failed"
                self._write_status()
                raise StackFailure(
                    spec.name,
                    f"{pending[0].failure} within {spec.timeout_seconds:g} seconds",
                )
            self._sleep(self._poll_interval)

    def _probe(self, probe: ReadinessProbe) -> bool:
        if probe.kind == "file":
            return Path(probe.target).is_file()
        return self._probe_http(probe.target)

    def _run_step(self, step: PrepStep) -> None:
        log_path = self.plan.run_root.service_log(step.name)
        with log_path.open("ab") as log:
            try:
                completed = subprocess.run(
                    step.argv,
                    cwd=step.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=step.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise StackFailure(
                    step.name, f"did not finish within {step.timeout_seconds:g} seconds"
                ) from error
            except OSError as error:
                raise StackFailure(step.name, f"could not start: {error}") from error
        if completed.returncode != 0:
            detail = _last_line(log_path)
            message = f"exited with status {completed.returncode}"
            raise StackFailure(step.name, f"{message}: {detail}" if detail else message)

    def _write_status(self) -> None:
        path = self.plan.run_root.stack_status
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(self.status_payload(), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)


def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _last_line(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1][:500] if lines else None


__all__ = [
    "CLOSED_LOOP_SERVICE",
    "WORKER_LOG_ARTIFACT_ID",
    "StackFailure",
    "StackSupervisor",
    "http_ready",
    "service_log_artifact_id",
]
