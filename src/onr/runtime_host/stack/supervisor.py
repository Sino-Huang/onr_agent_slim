"""Run one Mission Run's Environment Stack inside the Run Worker (decision D1).

Services start in plan order as children in the caller's process group (so
the Host's process-group cancellation reaps them), each logging to
``services/<name>.log``. A service must pass its readiness probes before the
next starts. ``stack-status.json`` is rewritten atomically on every state
transition with the ``services[]`` entries of the operator-view ``stack``
section; while a service is ``starting`` its entry names the readiness probe
still pending (``waiting_for``), and ``step`` names a running prep step.
``steps[]`` records every prep step that started (state, ``started_at``,
``finished_at``, log Artifact), so finished steps stay visible after a Host
restart.
Teardown runs in reverse start order and is written as it happens: each
running service turns ``stopping`` (``stop_requested_at``,
``stop_grace_seconds``) when it gets SIGTERM, then ``stopped`` with
``stop_mode`` ``graceful`` (it exited within its grace period) or ``forced``
(SIGKILL after the grace period) and ``stopped_at``. ``teardown`` records
when the reverse walk started and finished and its ``stop_order``.
``live_engine`` uses its grace to stop Harbor and restore ``environment.json``
and ``object_ids.txt``; :func:`harbor_config_restoration` reads whether the
engine or its guardian reported that restoration.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Self

from onr.runtime_host.importance import service_importance
from onr.runtime_host.run_root import RunRoot
from onr.runtime_host.stack.builder import (
    PrepStep,
    ReadinessProbe,
    ServiceSpec,
    StackPlan,
)

CLOSED_LOOP_SERVICE = "closed-loop"
WORKER_LOG_ARTIFACT_ID = "worker-log"
ENGINE_SERVICE = "airsim-engine"
_TAIL_BYTES = 4096
_RESTORATION_TAIL_BYTES = 16384
# What ``live_engine`` and its guardian print once the Harbor configuration is
# back. The engine prints its line on every exit path, so a restoration
# failure (``EngineConfigSwap`` raises after comparing the bytes) voids it.
_ENGINE_RESTORED = "Engine stopped; configuration restored"
_ENGINE_RESTORE_FAILED = "Engine configuration restoration failed"
_GUARDIAN_RESTORED = "Guardian: engine stopped; configuration restored"


def service_log_artifact_id(name: str) -> str:
    return f"service-log-{name}"


class StackFailure(RuntimeError):
    """A required service crashed, timed out, or a prep step failed."""

    def __init__(self, service: str, message: str) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.message = message

    def terminal_detail(self) -> dict[str, str]:
        """The ``terminal_detail`` object for ``/mission-runs/current``.

        ``log_artifact_id`` (v1.5) names the failed service's or prep step's
        allowlisted log, ``services/<name>.log``.
        """

        return {
            "kind": "stack_failed",
            "service": self.service,
            "message": self.message,
            "log_artifact_id": service_log_artifact_id(self.service),
        }


def http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def ready_durations(status: object) -> dict[str, float]:
    """Measured start-to-ready seconds per service in a ``stack-status.json``.

    Only services with both ``started_at`` and a later-or-equal ``ready_at``
    count; the closed loop has no readiness probe, so it never does.
    """

    services = status.get("services") if isinstance(status, dict) else None
    durations: dict[str, float] = {}
    for service in services if isinstance(services, list) else ():
        if not isinstance(service, dict):
            continue
        name = service.get("name")
        if not isinstance(name, str) or name == CLOSED_LOOP_SERVICE:
            continue
        started = _parse_utc(service.get("started_at"))
        ready = _parse_utc(service.get("ready_at"))
        if started is None or ready is None or ready < started:
            continue
        durations[name] = (ready - started).total_seconds()
    return durations


def harbor_config_restoration(
    run_root: RunRoot, services: Sequence[object]
) -> dict[str, object]:
    """Whether the Harbor engine configuration was reported restored.

    ``confirmed`` only when ``live_engine`` logged its restoration (it compares
    the restored bytes with the originals and raises otherwise) or its
    detached guardian logged one after the engine was killed;
    ``not_applicable`` when the stack has no ``airsim-engine`` or it never
    started (the configuration was never staged); ``unknown`` otherwise -
    including while the guardian has not finished yet.
    """

    engine = next(
        (
            service
            for service in services
            if isinstance(service, Mapping) and service.get("name") == ENGINE_SERVICE
        ),
        None,
    )
    if engine is None or engine.get("started_at") is None:
        return {"state": "not_applicable", "reported_by": None}
    engine_log = _tail(run_root.service_log(ENGINE_SERVICE), _RESTORATION_TAIL_BYTES)
    if _ENGINE_RESTORED in engine_log and _ENGINE_RESTORE_FAILED not in engine_log:
        return {"state": "confirmed", "reported_by": "engine"}
    guardian_log = _tail(run_root.engine / "guardian.log", _RESTORATION_TAIL_BYTES)
    if _GUARDIAN_RESTORED in guardian_log:
        return {"state": "confirmed", "reported_by": "guardian"}
    return {"state": "unknown", "reported_by": None}


@dataclass(slots=True)
class _ServiceState:
    name: str
    required: bool
    completes: bool
    port: int | None
    log_artifact_id: str
    log_path: Path | None
    ready_timeout_seconds: float | None
    state: str = "pending"
    pid: int | None = None
    started_at: str | None = None
    ready_at: str | None = None
    exit_code: int | None = None
    waiting_for: str | None = None
    stop_requested_at: str | None = None
    stop_grace_seconds: float | None = None
    stopped_at: str | None = None
    stop_mode: str | None = None

    def entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
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
                self.state,
                required=self.required,
                exit_code=self.exit_code,
                completes=self.completes,
            ),
        }
        # v1.4 optional fields: present only when they carry a value.
        if self.state == "starting" and self.waiting_for is not None:
            entry["waiting_for"] = self.waiting_for
        if self.ready_timeout_seconds is not None:
            entry["ready_timeout_seconds"] = self.ready_timeout_seconds
        # v1.5 teardown fields: present once teardown signalled the service.
        if self.stop_requested_at is not None:
            entry["stop_requested_at"] = self.stop_requested_at
            entry["stop_grace_seconds"] = self.stop_grace_seconds
        if self.stop_mode is not None:
            entry["stop_mode"] = self.stop_mode
            entry["stopped_at"] = self.stopped_at
        return entry


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
                completes=spec.completes,
                port=spec.port,
                log_artifact_id=service_log_artifact_id(spec.name),
                log_path=layout.service_log(spec.name),
                ready_timeout_seconds=spec.timeout_seconds,
            )
            for spec in plan.services
        }
        self._states[CLOSED_LOOP_SERVICE] = _ServiceState(
            name=CLOSED_LOOP_SERVICE,
            required=True,
            completes=False,
            port=None,
            log_artifact_id=WORKER_LOG_ARTIFACT_ID,
            log_path=layout.worker_log,
            ready_timeout_seconds=None,
        )
        self._step: dict[str, object] | None = None
        self._steps: list[dict[str, object]] = []
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._logs: dict[str, IO[bytes]] = {}
        self._teardown: dict[str, object] | None = None

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
                self._run_step(step, stage="prepare")
            for spec in self.plan.services:
                self._start_service(spec)
                self._await_ready(spec)
            for step in self.plan.post_ready:
                self._run_step(step, stage="post_ready")
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
            if code is None or state.state in {
                "exited",
                "failed",
                "stopping",
                "stopped",
            }:
                continue
            state.exit_code = code
            state.state = "failed" if code != 0 else "exited"
            changed = True
            if state.required and failure is None and not (state.completes and code == 0):
                failure = StackFailure(name, f"exited with status {code}")
        if changed:
            self._write_status()
        return failure

    def begin_closed_loop(self) -> None:
        state = self._states[CLOSED_LOOP_SERVICE]
        state.state = "ready"
        state.started_at = state.ready_at = self._now()
        self._write_status()

    def end_closed_loop(self, *, failed: bool, cancelled: bool = False) -> None:
        """Record the closed loop's end; an owner cancellation is ``stopped``."""

        state = self._states[CLOSED_LOOP_SERVICE]
        state.state = "stopped" if cancelled else "failed" if failed else "exited"
        self._write_status()

    def stop(self) -> None:
        """Tear down running services in reverse start order (idempotent).

        The status is written as each service turns ``stopping`` and again
        once it is ``stopped`` with its ``stop_mode``.
        """

        services = list(reversed(self.plan.services))
        if self._teardown is None:
            self._teardown = {
                "started_at": self._now(),
                "finished_at": None,
                "stop_order": [
                    spec.name
                    for spec in services
                    if spec.name in self._processes
                    and self._processes[spec.name].poll() is None
                ],
            }
            self._write_status()
        for spec in services:
            process = self._processes.get(spec.name)
            if process is None:
                continue
            state = self._states[spec.name]
            if process.poll() is None:
                # A service that already failed (readiness timeout) keeps
                # that state; only a running one shows its teardown.
                visible = state.state not in {"failed", "exited"}
                if visible:
                    state.state = "stopping"
                    state.waiting_for = None
                    state.stop_requested_at = self._now()
                    state.stop_grace_seconds = spec.stop_grace_seconds
                    self._write_status()
                forced = _terminate(process, spec.stop_grace_seconds)
                if visible:
                    state.state = "stopped"
                    state.stop_mode = "forced" if forced else "graceful"
                    state.stopped_at = self._now()
            elif state.state not in {"failed", "exited", "stopped"}:
                state.state = "failed" if process.returncode != 0 else "exited"
            state.exit_code = process.returncode
            log = self._logs.pop(spec.name, None)
            if log is not None:
                log.close()
            self._write_status()
        loop = self._states[CLOSED_LOOP_SERVICE]
        if loop.state in {"starting", "ready"}:
            loop.state = "stopped"
        if self._teardown["finished_at"] is None:
            self._teardown["finished_at"] = self._now()
        self._write_status()

    def status_payload(self) -> dict[str, object]:
        """The ``stack-status.json`` document."""

        request = self.plan.request
        payload: dict[str, object] = {
            "schema_version": 1,
            "preset_id": request.preset_id,
            "toggles": request.toggles.payload(),
            "updated_at": self._now(),
            "services": [state.entry() for state in self._states.values()],
        }
        if self._step is not None:
            payload["step"] = self._step
        if self._steps:
            payload["steps"] = [dict(step) for step in self._steps]
        if self._teardown is not None:
            # ``stop_order`` is fixed when teardown starts; only times change.
            payload["teardown"] = dict(self._teardown)
        return payload

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
        state.waiting_for = spec.readiness[0].waiting if spec.readiness else None
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
                state.waiting_for = None
                self._write_status()
                return
            if pending[0].waiting != state.waiting_for:
                state.waiting_for = pending[0].waiting
                self._write_status()
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

    def _run_step(self, step: PrepStep, *, stage: str) -> None:
        log_path = self.plan.run_root.service_log(step.name)
        started_at = self._now()
        self._step = {
            "name": step.name,
            "stage": stage,
            "started_at": started_at,
            "timeout_seconds": step.timeout_seconds,
        }
        record: dict[str, object] = {
            "name": step.name,
            "stage": stage,
            "state": "running",
            "started_at": started_at,
            "finished_at": None,
            "log_artifact_id": service_log_artifact_id(step.name),
            "timeout_seconds": step.timeout_seconds,
        }
        self._steps.append(record)
        self._write_status()
        try:
            self._execute_step(step, log_path)
            record["state"] = "done"
        except StackFailure:
            record["state"] = "failed"
            raise
        except BaseException:
            record["state"] = "stopped"
            raise
        finally:
            record["finished_at"] = self._now()
            self._step = None
            self._write_status()

    def _execute_step(self, step: PrepStep, log_path: Path) -> None:
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


def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> bool:
    """SIGTERM, then SIGKILL after ``grace_seconds``; ``True`` when killed."""

    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return False
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return True
    return False


def _tail(path: Path, size: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _last_line(path: Path | None) -> str | None:
    if path is None:
        return None
    tail = _tail(path, _TAIL_BYTES)
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1][:500] if lines else None


__all__ = [
    "CLOSED_LOOP_SERVICE",
    "ENGINE_SERVICE",
    "WORKER_LOG_ARTIFACT_ID",
    "StackFailure",
    "StackSupervisor",
    "harbor_config_restoration",
    "http_ready",
    "ready_durations",
    "service_log_artifact_id",
]
