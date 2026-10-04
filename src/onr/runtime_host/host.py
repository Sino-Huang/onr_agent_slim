"""Durable single-mission Runtime Host application service."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import signal
import tempfile
import time
import traceback
import weakref
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Event as ProcessEvent
from multiprocessing import Process
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread
from typing import Any, Protocol, cast

from onr.adapters.file_transport import FileTransport
from onr.contracts.hyper_agent import MissionInput
from onr.runtime.cli import MissionRejectedError, run_closed_loop_demo
from onr.runtime.composition import RuntimeComposition
from onr.runtime.config import RuntimeConfig, load_runtime_config
from onr.runtime_host.airsim_overlay import PerceptionAnnotator
from onr.runtime_host.artifacts import (
    RECEIPT_EXPORT_NAME,
    ArtifactNotFoundError,
    PublicArtifactInbox,
    receipt_artifact_references,
    service_log_content,
)
from onr.runtime_host.beliefs import beliefs_section
from onr.runtime_host.context_view import context_section
from onr.runtime_host.narrative import (
    RunNarrativeRecord,
    RunNarrativeSummarizer,
    build_narrative_input,
    sanitize_narrative_text,
)
from onr.runtime_host.observations import (
    ACTIVITY_MAPPING_VERSION,
    DEFAULT_PAGE_SIZE,
    OBSERVATION_SCHEMA_VERSION,
    EvidenceSource,
    EvidenceTailer,
    InvalidCursorError,
    RunObservations,
    decode_cursor,
    encode_cursor,
    page_entries,
)
from onr.runtime_host.operator_projection import (
    OPERATOR_DEFAULT_LIMIT,
    OperatorRunProjection,
    OperatorSection,
    run_wall_seconds,
)
from onr.runtime_host.progress import load_mission_log_summaries
from onr.runtime_host.run_files import JsonFileCache
from onr.runtime_host.run_root import RunRoot
from onr.runtime_host.stack import (
    StackFailure,
    StackPlan,
    StackSupervisor,
    load_stack_catalog,
    plan_mission_run,
    ready_durations,
    run_preflight,
)
from onr.runtime_host.stack.supervisor import (
    WORKER_LOG_ARTIFACT_ID,
    harbor_config_restoration,
)
from onr.runtime_host.world import CameraCapture, WorldView
from onr.viewer.trace import sanitize_payload

Clock = Callable[[], str]
IdGenerator = Callable[[str], str]
WorkerEntrypoint = Callable[["WorkerContext"], None]
_NONTERMINAL_STATUSES = {"queued", "running", "awaiting_human_decision"}
_WORKER_IDENTITY = "runtime_host.closed_loop_demo"
_WORKER_OWNERSHIP_ENV = "ONR_RUNTIME_HOST_WORKER_TOKEN"
_WORKER_START_TIMEOUT_SECONDS = 5.0
_TERMINAL_DETAIL_TEXT_LIMIT = 500
HISTORY_DEFAULT_LIMIT = 20
HISTORY_MAX_LIMIT = 100


class _EventLike(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    pid: int
    process_group_id: int
    process_start_time: str
    process_session_id: int
    ownership_token: str

    @classmethod
    def from_state(cls, run: Mapping[str, object]) -> WorkerIdentity | None:
        values = (
            run.get("worker_pid"),
            run.get("worker_process_group_id"),
            run.get("worker_start_time"),
            run.get("worker_session_id"),
            run.get("worker_ownership_token"),
        )
        pid, process_group_id, start_time, session_id, token = values
        if not (
            isinstance(pid, int)
            and isinstance(process_group_id, int)
            and isinstance(start_time, str)
            and isinstance(session_id, int)
            and isinstance(token, str)
        ):
            return None
        return cls(pid, process_group_id, start_time, session_id, token)

    def persist(self, run: dict[str, object]) -> None:
        run.update(
            {
                "worker_pid": self.pid,
                "worker_process_group_id": self.process_group_id,
                "worker_start_time": self.process_start_time,
                "worker_session_id": self.process_session_id,
                "worker_ownership_token": self.ownership_token,
                "worker_launch_state": "group_ready",
            }
        )

    def is_owned(self) -> bool:
        leader_matches = (
            self.process_group_id == self.pid
            and self.process_session_id == self.pid
            and _process_start_time(self.pid) == self.process_start_time
        )
        return (
            leader_matches
            or (
                self.process_group_id == self.process_session_id
                and _owned_group_member_exists(
                    self.process_group_id,
                    self.process_session_id,
                    self.ownership_token,
                )
            )
            or bool(_owned_detached_groups(self.ownership_token, self.process_group_id))
        )


class WorkerHandle(Protocol):
    @property
    def identity(self) -> WorkerIdentity: ...

    def join(self, timeout: float | None = None) -> None: ...

    def release(self) -> None: ...


WorkerLauncher = Callable[[Callable[[], None]], WorkerHandle | None]


class HostConflictError(Exception):
    """A stable conflict suitable for translation at the HTTP boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class HostAuthorizationError(Exception):
    """An owner-only request could not be authorized."""


class HostNotFoundError(Exception):
    """A public Mission Run lookup did not resolve on this Host."""


class RunRootUnavailableError(HostNotFoundError):
    """The Mission Run is known, but its Run Root is missing on this Host."""


@dataclass(frozen=True, slots=True)
class RuntimeWorkerOptions:
    repo_root: Path
    recursion_limit: int = 120

    def __post_init__(self) -> None:
        if self.recursion_limit < 1:
            raise ValueError("worker recursion limit must be positive")


@dataclass(frozen=True, slots=True)
class WorkerContext:
    config: RuntimeConfig
    mission_id: str
    mission_run_id: str
    activation_request_id: str
    console_session_id: str
    mission_intent: str
    source_authority: str
    options: RuntimeWorkerOptions
    run_root: RunRoot
    stack_options: Mapping[str, object] | None = None
    report_rejection: Callable[[str], None] | None = None


def _camera_capture(plan: StackPlan) -> CameraCapture | None:
    """Read-only, perception-annotated camera capture for scene-clock runs.

    Perception-off AirSim runs need none: the follower service owns the frozen
    engine and publishes its own post-step frames (ADR 0016).
    """
    toggles = plan.request.toggles
    if not toggles.airsim or toggles.perception == "off":
        return None
    engine = plan.request.engine
    return CameraCapture(
        plan.run_root.path,
        cameras={
            "camera_front": engine.airsim_camera,
            "camera_third_person": engine.airsim_third_person_camera,
        },
        vehicle_name=engine.airsim_vehicle,
        rpc_port=engine.rpc_port,
        viewer_port=plan.viewer_port,
        airsim_settings=engine.airsim_settings,
        annotator=PerceptionAnnotator(
            plan.run_root.path,
            perception=toggles.perception,
            mission_id=plan.request.mission_id,
        ),
    )


def runtime_worker(context: WorkerContext) -> None:
    """Launch the owned stack and run the mission using its materialized inputs."""
    options = dict(context.stack_options or {})
    plan = plan_mission_run(
        preset_id=cast(str | None, options.get("preset_id")),
        stack=options,
        run_root=context.run_root,
        mission_id=context.mission_id,
        repo_root=context.options.repo_root,
    )
    mission_input = MissionInput(
        mission_id=context.mission_id,
        mission_text=context.mission_intent,
        source_authority=context.source_authority,
    )
    supervisor = StackSupervisor(plan)
    stop_monitor = Event()
    failures: list[StackFailure] = []
    owner_cancelled = Event()

    def cancelled(_signal: int, _frame: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        owner_cancelled.set()
        raise _WorkerCancelled

    def service_failed(_signal: int, _frame: object) -> None:
        if failures:
            raise failures[0]

    def monitor() -> None:
        while not stop_monitor.wait(0.25):
            failure = supervisor.poll()
            if failure is not None:
                failures.append(failure)
                os.kill(os.getpid(), signal.SIGUSR1)
                return

    previous_term = signal.signal(signal.SIGTERM, cancelled)
    previous_failure = signal.signal(signal.SIGUSR1, service_failed)
    thread: Thread | None = None
    camera: CameraCapture | None = None
    failed = True
    try:
        # Built before the stack so its SDK/encoder imports never delay the
        # mission once services are ready.
        camera = _camera_capture(plan)
        supervisor.start()
        if camera is not None:
            # Started only after the engine is ready; stopped in teardown.
            camera.start()
        # Mission 1 planning inputs are produced by the post-readiness prep steps.
        config = load_runtime_config(
            plan.agent_config, repo_root=context.options.repo_root
        )
        if config.transport.backend != "file":
            raise RuntimeError("runtime Host worker requires transport.backend=file")
        runtime = RuntimeComposition(config, FileTransport(config.transport.root))
        supervisor.begin_closed_loop()
        thread = Thread(target=monitor, name="stack-monitor", daemon=True)
        thread.start()
        with runtime.mission_session(
            context.mission_id, model=runtime.create_chat_model()
        ):
            try:
                result = run_closed_loop_demo(
                    runtime,
                    mission_input,
                    repo_root=context.options.repo_root,
                    planner_artifacts=plan.closed_loop.planner_artifacts,
                    recursion_limit=context.options.recursion_limit,
                    simulation_limit_seconds=plan.closed_loop.simulation_limit_seconds,
                )
            except MissionRejectedError as exc:
                # The authoritative decision need not wait for the final LLM summary.
                if context.report_rejection is not None:
                    context.report_rejection(exc.reason)
                raise
            finally:
                # Session closeout can wait on a final LLM summary. Services
                # terminated during that unwind are expected teardown, not a
                # new failure that can replace rejection or cancellation.
                stop_monitor.set()
                thread.join()
        failure = supervisor.poll()
        if failures:
            raise failures[0]
        if failure is not None:
            raise failure
        context.run_root.closed_loop_result.write_text(
            json.dumps(result.to_dict(), sort_keys=True) + "\n", encoding="utf-8"
        )
        failed = False
    finally:
        # A late monitor signal or repeated cancel must not interrupt teardown.
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop_monitor.set()
        if thread is not None:
            thread.join()
        failed = failed or bool(failures)
        try:
            try:
                try:
                    # Bounded: the capture must end before the engine stops.
                    if camera is not None:
                        camera.stop()
                finally:
                    # Capture even when no client fetched a frame; never skip
                    # teardown.
                    WorldView(context.run_root.path, timeout=0.25).capture_final()
            finally:
                try:
                    # An owner cancellation stops the closed loop; it did not fail.
                    supervisor.end_closed_loop(
                        failed=failed, cancelled=owner_cancelled.is_set()
                    )
                finally:
                    supervisor.stop()
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGUSR1, previous_failure)
    if failures:
        raise failures[0]


class _WorkerCancelled(BaseException):
    """Unwind the worker without misclassifying an owner cancellation."""


@dataclass(frozen=True, slots=True)
class _LaunchedWorker:
    process: Process
    identity: WorkerIdentity
    release_gate: _EventLike

    @property
    def pid(self) -> int | None:
        return self.identity.pid

    def join(self, timeout: float | None = None) -> None:
        self.process.join(timeout)

    def release(self) -> None:
        self.release_gate.set()


def _process_group_entrypoint(
    callback: Callable[[], None],
    ready: _EventLike,
    release: _EventLike,
    ownership_token: str,
    startup_timeout_seconds: float,
) -> None:
    if hasattr(os, "setsid"):
        os.setsid()
    os.environ[_WORKER_OWNERSHIP_ENV] = ownership_token
    ready.set()
    if not release.wait(startup_timeout_seconds):
        return
    callback()


def _launch_process(callback: Callable[[], None]) -> _LaunchedWorker:
    ready = ProcessEvent()
    release = ProcessEvent()
    ownership_token = secrets.token_hex(32)
    process = Process(
        target=_process_group_entrypoint,
        args=(
            callback,
            ready,
            release,
            ownership_token,
            _WORKER_START_TIMEOUT_SECONDS,
        ),
        name="runtime-host-worker",
        daemon=False,
    )
    process.start()
    if not ready.wait(timeout=5):
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError("runtime Host worker process group was not ready")
    pid = process.pid
    if pid is None:
        raise RuntimeError("runtime Host worker did not receive a process ID")
    start_time = _process_start_time(pid)
    identity = _process_identity(pid)
    if start_time is None or identity is None:
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError("runtime Host worker identity was not available")
    process_group_id, process_session_id, _ = identity
    if process_group_id != pid or process_session_id != pid:
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError("runtime Host worker process group identity was invalid")
    return _LaunchedWorker(
        process,
        WorkerIdentity(
            pid,
            process_group_id,
            start_time,
            process_session_id,
            ownership_token,
        ),
        release,
    )


class RuntimeHost:
    """Own durable activation idempotency and one active mission run."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        clock: Clock,
        generate_id: IdGenerator,
        worker_entrypoint: WorkerEntrypoint | None = None,
        launch_worker: WorkerLauncher | None = None,
        worker_options: RuntimeWorkerOptions | None = None,
        evidence_source: EvidenceSource | None = None,
        artifact_inbox_root: Path | None = None,
        runs_root: Path | None = None,
        narrative_summarizer: RunNarrativeSummarizer | None = None,
        narrative_interval_seconds: float = 30.0,
        narrative_poll_seconds: float | None = 2.0,
    ) -> None:
        """Create the Host and recover persisted runs.

        ``evidence_source`` replaces the per-run evidence tailer with a full-rescan
        source. ``runs_root`` holds one Run Root per Mission Run (default
        ``<storage>/runtime-host/runs``). With a summarizer, a background thread
        calls ``narrative_tick`` every ``narrative_poll_seconds``; ``None`` leaves
        ticking to the caller.
        """

        self.config = config
        self.root = config.storage.root / "runtime-host"
        self._state_path = self.root / "state.json"
        self._state_lock_path = self.root / "state.lock"
        self._runs_root = runs_root if runs_root is not None else self.root / "runs"
        self._clock = clock
        self._generate_id = generate_id
        self._worker_entrypoint = worker_entrypoint or runtime_worker
        self._launch_worker = launch_worker or _launch_process
        self._worker_options = worker_options or RuntimeWorkerOptions(
            repo_root=Path.cwd()
        )
        self._evidence_source = evidence_source
        self._artifact_inbox_root = artifact_inbox_root
        self._operator_projection = OperatorRunProjection()
        self._world_views: dict[str, WorldView] = {}
        self._stack_catalog = load_stack_catalog()
        self._stack_status_cache = JsonFileCache[dict[str, Any]](
            dict, max_bytes=1024 * 1024
        )
        self._projection_lock = Lock()
        self._narrative_summarizer = narrative_summarizer
        self._narrative_interval_seconds = narrative_interval_seconds
        self._lock = RLock()
        self._run_observations: dict[str, RunObservations] = {}
        self._evidence_lock = Lock()
        self._narrative_guard = Lock()
        self._narrative_attempt = Lock()
        self._narrative_records: dict[str, RunNarrativeRecord] = {}
        self._narrative_stop = Event()
        self._narrative_watch: set[str] = set()
        self._narrative_thread: Thread | None = None
        self._workers: dict[str, WorkerHandle] = {}
        self._reconcilers: dict[str, Thread] = {}
        _reset_locks_in_forked_worker(self)
        recoveries: list[tuple[str, dict[str, Any]]] = []
        with self._state_guard():
            state = self._load_state()
            changed = False
            now = self._clock()
            for run in state["runs"].values():
                if "run_root" not in run:
                    # Persist the pre-v1.2 root once; per-run readers keep one
                    # required path, without legacy aliases or copying history.
                    historical = RunRoot.for_run(
                        self.root / "runs", str(run["mission_run_id"])
                    )
                    selected = (
                        historical
                        if historical.path.is_dir()
                        else RunRoot.for_run(
                            self._runs_root, str(run["mission_run_id"])
                        )
                    )
                    run["run_root"] = str(selected.path)
                    changed = True
                if run["status"] in _NONTERMINAL_STATUSES:
                    identity = WorkerIdentity.from_state(run)
                    if identity is not None and identity.is_owned():
                        recovery = dict(run)
                        recoveries.append((str(run["mission_run_id"]), recovery))
                    elif (
                        identity is not None
                        and run.get("cancellation_requested") is True
                        and not self._process_group_exists(identity.process_group_id)
                    ):
                        run["status"] = "cancelled"
                        run["finished_at"] = now
                        run["terminal_classification"] = "cancelled_by_owner"
                        changed = True
                    else:
                        run["status"] = "failed"
                        run["finished_at"] = now
                        run["terminal_classification"] = "host_interrupted"
                        changed = True
            if changed:
                self._save_state(state)
            current = self._current_run(state)
            if current is not None and self._narrative_summarizer is not None:
                self._narrative_watch.add(str(current["mission_run_id"]))
        for mission_run_id, run in recoveries:
            cancelled = run.get("cancellation_requested") is True
            exited = self._terminate_owned_worker(
                mission_run_id, None, persisted_run=run, reconcile=False
            )
            if exited:
                self._transition(
                    mission_run_id,
                    "cancelled" if cancelled else "failed",
                    terminal_classification=(
                        "cancelled_by_owner" if cancelled else "host_interrupted"
                    ),
                    cancellation_tree_exited=True,
                )
            else:
                identity = WorkerIdentity.from_state(run)
                if identity is not None:
                    self._start_reconciler(
                        mission_run_id,
                        self._reconcile_recovered_worker,
                        identity.process_group_id,
                        cancelled,
                    )
        if narrative_summarizer is not None and narrative_poll_seconds is not None:
            self._narrative_thread = Thread(
                target=self._narrative_loop,
                args=(narrative_poll_seconds,),
                name="runtime-host-narrative",
                daemon=True,
            )
            self._narrative_thread.start()

    def close(self) -> None:
        """Stop the background Run Narrative thread.

        An in-flight generation is not interrupted; the daemon thread exits after
        it, so shutdown waits only briefly.
        """

        self._narrative_stop.set()
        thread = self._narrative_thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=5.0)

    def activate(
        self,
        *,
        activation_request_id: str,
        console_session_id: str,
        mission_intent: str,
        source_authority: str,
        credential: str,
        stack: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        preset = self._stack_catalog.preset(
            None if stack is None else cast(str | None, stack.get("preset_id"))
        )
        toggles = self._stack_catalog.toggles(preset, stack)
        stack_options = {
            "preset_id": preset.preset_id,
            **toggles.payload(),
            "simulation_limit_seconds": toggles.simulation_limit_seconds,
        }
        request_fields = {
            "activation_request_id": activation_request_id,
            "console_session_id": console_session_id,
            "mission_intent": mission_intent,
            "source_authority": source_authority,
            "stack": stack_options,
        }
        with self._state_guard():
            state = self._load_state()
            session_verifier = state["session_verifiers"].get(console_session_id)
            if session_verifier is not None and not _verify_credential(
                credential, session_verifier
            ):
                raise HostConflictError(
                    "console_session_credential_conflict",
                    "console_session_id was supplied with a different credential",
                )
            existing = state["activations"].get(activation_request_id)
            if existing is not None:
                same_fields = all(
                    existing.get(name) == value
                    for name, value in request_fields.items()
                    if name != "activation_request_id"
                )
                if same_fields and _verify_credential(
                    credential, existing["credential_verifier"]
                ):
                    return dict(existing["response"])
                raise HostConflictError(
                    "activation_request_conflict",
                    "activation_request_id was reused with a different body or credential",
                )

            current = self._current_run(state)
            if current is not None and current["status"] in _NONTERMINAL_STATUSES:
                raise HostConflictError(
                    "mission_run_active", "a non-terminal Mission Run already exists"
                )

            now = self._clock()
            mission_id = self._generate_id("mission")
            mission_run_id = self._generate_id("run")
            run_root = RunRoot.for_run(self._runs_root, mission_run_id).create()
            credential_verifier = session_verifier or _credential_verifier(credential)
            response: dict[str, object] = {
                "activation_request_id": activation_request_id,
                "mission_id": mission_id,
                "mission_run_id": mission_run_id,
                "status": "queued",
                "created_at": now,
            }
            state["activations"][activation_request_id] = {
                "console_session_id": console_session_id,
                "activation_request_id": activation_request_id,
                "mission_intent": mission_intent,
                "source_authority": source_authority,
                "stack": stack_options,
                "request_digest": _digest_json(
                    {**request_fields, "credential_identity": credential_verifier}
                ),
                "credential_verifier": credential_verifier,
                "response": response,
            }
            state["session_verifiers"][console_session_id] = credential_verifier
            state["runs"][mission_run_id] = {
                "mission_id": mission_id,
                "mission_run_id": mission_run_id,
                "activation_request_id": activation_request_id,
                "console_session_id": console_session_id,
                "worker_identity": _WORKER_IDENTITY,
                "status": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "terminal_classification": None,
                "terminal_detail": None,
                "stack": {
                    "preset_id": preset.preset_id,
                    "airsim": toggles.airsim,
                    "perception": toggles.perception,
                },
                "stack_options": stack_options,
                "mission_mode": preset.mission_mode,
                "run_root": str(run_root.path),
                "cancellation_requested": False,
                "worker_launch_state": "launching",
                "previous_ready_seconds": self._previous_ready_seconds(
                    state, preset.preset_id
                ),
            }
            state["latest_run_by_preset"][preset.preset_id] = mission_run_id
            state["current_run_id"] = mission_run_id
            self._save_state(state)

        if self._narrative_summarizer is not None:
            with self._narrative_guard:
                self._narrative_watch.add(mission_run_id)
        self._prune_run_caches()

        context = WorkerContext(
            config=self.config,
            mission_id=mission_id,
            mission_run_id=mission_run_id,
            activation_request_id=activation_request_id,
            console_session_id=console_session_id,
            mission_intent=mission_intent,
            source_authority=source_authority,
            options=self._worker_options,
            run_root=run_root,
            stack_options=stack_options,
            report_rejection=lambda reason: self._report_rejection(
                mission_run_id, reason
            ),
        )
        start_gate = ProcessEvent()
        cancel_after_registration = False

        def gated_worker() -> None:
            if not start_gate.wait(timeout=_WORKER_START_TIMEOUT_SECONDS):
                return
            self._run_worker(context)

        try:
            worker = self._launch_worker(gated_worker)
            with self._state_guard():
                state = self._load_state()
                run = state["runs"].get(mission_run_id)
                if isinstance(run, dict):
                    if worker is not None:
                        self._workers[mission_run_id] = worker
                        worker.identity.persist(run)
                    else:
                        run["worker_launch_state"] = "registered"
                    cancel_after_registration = (
                        run.get("cancellation_requested") is True
                    )
                    self._save_state(state)
            if cancel_after_registration and worker is not None:
                self._terminate_owned_worker(mission_run_id, worker)
            if worker is not None:
                worker.release()
            start_gate.set()
        except Exception:  # noqa: BLE001 - launcher failures become durable run state.
            start_gate.set()
            self._transition(
                mission_run_id,
                "failed",
                terminal_classification="worker_start_failed",
            )
        return dict(response)

    def current_run(self) -> dict[str, object] | None:
        with self._state_guard():
            run = self._current_run(self._load_state())
            return None if run is None else _public_run(run)

    def mission_runs(
        self, *, limit: int = HISTORY_DEFAULT_LIMIT, before: str | None = None
    ) -> dict[str, object]:
        """One page of the run history, newest first.

        Runs are ordered by ``created_at`` then ``mission_run_id``, both
        descending, so ``before`` (the last row's run id) is a stable cursor: a
        newer activation never shifts an older page. Rows are the public run
        record plus toggles, wall duration and whether the Run Root is still on
        disk; they never carry the Mission Intent.
        """

        with self._state_guard():
            state = self._load_state()
        runs = [run for run in state["runs"].values() if isinstance(run, dict)]
        if before is not None:
            anchor = state["runs"].get(before)
            if not isinstance(anchor, dict):
                raise InvalidCursorError("before names no Mission Run on this Host")
            runs = [run for run in runs if _history_key(run) < _history_key(anchor)]
        runs.sort(key=_history_key, reverse=True)
        page = runs[:limit]
        current_id = state.get("current_run_id")
        return {
            "mission_runs": [
                {
                    "mission_run": _public_run(run),
                    "toggles": _history_toggles(run),
                    "wall_seconds": run_wall_seconds(run),
                    "run_root_available": self._run_root(run).path.is_dir(),
                    "current": run["mission_run_id"] == current_id,
                }
                for run in page
            ],
            "next_before": (
                str(page[-1]["mission_run_id"]) if len(runs) > limit else None
            ),
        }

    def stack_presets(self) -> dict[str, object]:
        return self._stack_catalog.payload(self._worker_options.repo_root)

    def stack_preflight(self, options: Mapping[str, object]) -> dict[str, object]:
        def active_run() -> str | None:
            run = self.current_run()
            if run is not None and run["status"] in _NONTERMINAL_STATUSES:
                return str(run["mission_run_id"])
            return None

        return run_preflight(
            self._stack_catalog,
            cast(str | None, options.get("preset_id")),
            options,
            repo_root=self._worker_options.repo_root,
            active_run=active_run,
        )

    def _world_view(self, run: Mapping[str, object]) -> WorldView:
        run_id = str(run["mission_run_id"])
        if run_id not in self._world_views:
            self._prune_run_caches(requested_run_id=run_id)
        with self._lock:
            view = self._world_views.get(run_id)
            if view is None:
                view = WorldView(self._run_root(run).path)
                self._world_views[run_id] = view
            return view

    def world_frame(self, mission_run_id: str, source: str):
        run = self._evidence_run(mission_run_id)
        return self._world_view(run).frame(
            source,
            live=run["status"] in _NONTERMINAL_STATUSES,
        )

    def _evidence_run(self, mission_run_id: str) -> dict[str, Any]:
        """The run record whose Run Root evidence a route is about to read.

        A historical run whose Run Root was removed is reported explicitly
        instead of as an empty run.
        """

        with self._state_guard():
            run = self._load_state()["runs"].get(mission_run_id)
        if not isinstance(run, dict):
            raise HostNotFoundError
        if not self._run_root(run).path.is_dir():
            raise RunRootUnavailableError
        return run

    def _run_root(self, run: Mapping[str, object]) -> RunRoot:
        return RunRoot(Path(str(run["run_root"])))

    def _artifact_inbox_for(self, run: Mapping[str, object]) -> PublicArtifactInbox:
        return PublicArtifactInbox(
            self._artifact_inbox_root
            or self._run_root(run).agent_storage / "artifact-inbox"
        )

    def _stack_section(self, run: Mapping[str, Any]) -> dict[str, object]:
        root = self._run_root(run)
        status = self._stack_status_cache.get(root.stack_status)
        payload: dict[str, Any] = (
            dict(status)
            if isinstance(status, Mapping)
            else {
                "preset_id": (run.get("stack") or {}).get("preset_id"),
                "toggles": {
                    key: (run.get("stack_options") or {}).get(key)
                    for key in ("airsim", "perception", "update_ownership")
                },
                "services": [],
            }
        )
        payload.pop("schema_version", None)
        payload.pop("updated_at", None)
        # Copies: the decoded status is cached and shared across requests.
        payload["services"] = [
            dict(service) if isinstance(service, dict) else service
            for service in payload.get("services", [])
        ]
        history = run.get("previous_ready_seconds")
        for service in payload["services"]:
            if not isinstance(service, dict):
                continue
            path = (
                root.worker_log
                if service.get("log_artifact_id") == "worker-log"
                else root.service_log(str(service["name"]))
            )
            try:
                with path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    handle.seek(max(0, handle.tell() - 4096))
                    lines = handle.read().decode("utf-8", errors="replace").splitlines()
                service["last_line"] = lines[-1][:500] if lines else None
            except OSError:
                service["last_line"] = None
            # v1.5: history from the previous run of the same preset, never an ETA.
            if isinstance(history, Mapping) and service.get("name") in history:
                service["previous_ready_seconds"] = history[service["name"]]
        teardown = payload.get("teardown")
        if isinstance(teardown, Mapping):
            # v1.5 teardown receipt. The Host records a terminal status only
            # after the Run Worker returned from its teardown or the Host
            # verified that its process tree exited.
            payload["teardown"] = {
                **teardown,
                "worker": (
                    "running" if run["status"] in _NONTERMINAL_STATUSES else "stopped"
                ),
                "harbor_config": harbor_config_restoration(root, payload["services"]),
            }
        return payload

    def _previous_ready_seconds(
        self, state: Mapping[str, Any], preset_id: str
    ) -> dict[str, float]:
        """Measured readiness from the previous run of ``preset_id``, if any."""

        previous_id = state["latest_run_by_preset"].get(preset_id)
        previous = (
            state["runs"].get(previous_id) if isinstance(previous_id, str) else None
        )
        if not isinstance(previous, dict) or not isinstance(
            previous.get("run_root"), str
        ):
            return {}
        return ready_durations(
            self._stack_status_cache.get(self._run_root(previous).stack_status)
        )

    def observations(
        self,
        mission_run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        run, entries = self._issued_observations(mission_run_id)
        after = (
            0
            if cursor is None
            else decode_cursor(
                cursor,
                mission_run_id=mission_run_id,
                max_sequence=len(entries),
            )
        )
        page, last_sequence = page_entries(
            entries, after=after, limit=limit or DEFAULT_PAGE_SIZE
        )
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "mission_id": run["mission_id"],
            "mission_run_id": mission_run_id,
            "observations": _observation_envelopes(page),
            "next_cursor": (
                None
                if last_sequence is None
                else encode_cursor(mission_run_id, last_sequence)
            ),
        }

    def narrative(self, mission_run_id: str) -> dict[str, object]:
        """Read the stored Run Narrative; generation happens in ``narrative_tick``."""

        with self._state_guard():
            run = self._load_state()["runs"].get(mission_run_id)
        if not isinstance(run, dict):
            raise HostNotFoundError
        return {
            "schema_version": 1,
            "mission_id": run["mission_id"],
            "mission_run_id": mission_run_id,
            "narrative": self._public_narrative(mission_run_id),
        }

    def narrative_tick(self) -> None:
        """Attempt each due Run Narrative once.

        Covers the current run plus runs this Host saw running, so a run replaced
        by a new activation right after it ended still gets its terminal attempt.
        A non-terminal run is regenerated when its operational log advanced and
        ``narrative_interval_seconds`` passed since the last attempt; a terminal
        run gets exactly one terminal attempt. Overlapping ticks return at once.
        """

        summarizer = self._narrative_summarizer
        if summarizer is None or not self._narrative_attempt.acquire(blocking=False):
            return
        try:
            with self._state_guard():
                state = self._load_state()
                current = self._current_run(state)
                with self._narrative_guard:
                    run_ids = sorted(
                        run_id
                        for run_id in self._narrative_watch
                        if current is None or run_id != current["mission_run_id"]
                    )
                if current is not None:
                    run_ids.append(str(current["mission_run_id"]))
                runs = [
                    dict(state["runs"][run_id])
                    for run_id in dict.fromkeys(run_ids)
                    if isinstance(state["runs"].get(run_id), dict)
                ]
            for run in runs:
                mission_run_id = str(run["mission_run_id"])
                with self._narrative_guard:
                    if (
                        run["status"] not in _NONTERMINAL_STATUSES
                        and self._narrative_record(mission_run_id).terminal_generated
                    ):
                        self._narrative_watch.discard(mission_run_id)
                        continue
                    self._narrative_watch.add(mission_run_id)
                self._attempt_narrative(summarizer, run)
                if run["status"] not in _NONTERMINAL_STATUSES:
                    with self._narrative_guard:
                        self._narrative_watch.discard(mission_run_id)
                    self._prune_run_caches()
        finally:
            self._narrative_attempt.release()

    def _attempt_narrative(
        self, summarizer: RunNarrativeSummarizer, run: Mapping[str, Any]
    ) -> None:
        mission_run_id = str(run["mission_run_id"])
        mission_id = str(run["mission_id"])
        terminal = run["status"] not in _NONTERMINAL_STATUSES
        now = self._clock()
        with self._narrative_guard:
            record = self._narrative_record(mission_run_id)
            if not self._narrative_due(record, terminal=terminal, now=now):
                return
            previous = record.public_narrative().get("text")
        _, observations = self._run_evidence(mission_run_id)
        observations.refresh(observed_at=now)
        narrative_input = build_narrative_input(
            run=_public_run(run),
            records=observations.operational_records(),
            summaries=load_mission_log_summaries(
                self._run_root(run).agent_storage, mission_id
            ),
            stack_status=self._stack_section(run),
            previous_narrative=previous if isinstance(previous, str) else None,
        )
        watermark = narrative_input["source_watermark"]
        if not isinstance(watermark, int) or isinstance(watermark, bool):
            watermark = 0
        with self._narrative_guard:
            record = self._narrative_record(mission_run_id)
            if not terminal and watermark <= record.source_watermark:
                return
            record.begin_attempt(started_at=now, terminal=terminal)
        try:
            text = sanitize_narrative_text(
                summarizer.summarize_narrative(
                    mission_id=mission_id,
                    mission_run_id=mission_run_id,
                    terminal=terminal,
                    narrative_input=narrative_input,
                )
            )
        except Exception:  # noqa: BLE001 - failures publish only typed evidence.
            text = None
        with self._narrative_guard:
            record = self._narrative_record(mission_run_id)
            if text is None:
                record.publish_unavailable(generated_at=now, terminal=terminal)
            else:
                record.publish_available(
                    text=text,
                    generated_at=now,
                    source_watermark=watermark,
                    terminal=terminal,
                )

    def activities(
        self,
        mission_run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        run, observations = self._run_evidence(mission_run_id)
        observations.refresh(observed_at=self._clock(), wait=False)
        activities = observations.activities()
        after = (
            0
            if cursor is None
            else decode_cursor(
                cursor,
                mission_run_id=mission_run_id,
                max_sequence=len(activities),
            )
        )
        page, last_sequence = page_entries(
            activities, after=after, limit=limit or DEFAULT_PAGE_SIZE
        )
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "mission_id": run["mission_id"],
            "mission_run_id": mission_run_id,
            "mapping_version": ACTIVITY_MAPPING_VERSION,
            "activities": page,
            "next_cursor": (
                None
                if last_sequence is None
                else encode_cursor(mission_run_id, last_sequence)
            ),
        }

    def artifacts(
        self,
        mission_run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Return one page from the Mission Run's Public Artifact Inbox."""

        run = self._evidence_run(mission_run_id)
        return self._artifact_inbox_for(run).artifacts(
            str(run["mission_id"]),
            mission_run_id,
            cursor=cursor,
            limit=limit,
        )

    def artifact_content(
        self,
        mission_run_id: str,
        artifact_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Read one public Artifact content preview."""

        run = self._evidence_run(mission_run_id)
        mission_id = str(run["mission_id"])
        if artifact_id == "worker-log" or artifact_id.startswith("service-log-"):
            return service_log_content(
                self._run_root(run).path,
                mission_id,
                mission_run_id,
                artifact_id,
                offset=offset,
                limit=limit,
            )
        try:
            return self._artifact_inbox_for(run).artifact_content(
                mission_id,
                mission_run_id,
                artifact_id,
                offset=offset,
                limit=limit,
            )
        except ArtifactNotFoundError:
            planner_root = self._run_root(run).planner_artifacts
            # The projection owns a per-run planner inventory cache.
            with self._projection_lock:
                return self._operator_projection.planner_artifact_content(
                    mission_id=mission_id,
                    mission_run_id=mission_run_id,
                    planner_root=planner_root,
                    artifact_id=artifact_id,
                    offset=offset,
                    limit=limit,
                )

    def conversation_entries(
        self,
        mission_run_id: str,
        artifact_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Return one public Conversation Artifact entry page."""

        run = self._evidence_run(mission_run_id)
        return self._artifact_inbox_for(run).conversation_entries(
            str(run["mission_id"]),
            mission_run_id,
            artifact_id,
            cursor=cursor,
            limit=limit,
        )

    def operator_view(
        self,
        mission_run_id: str,
        *,
        section: OperatorSection,
        limit: int = OPERATOR_DEFAULT_LIMIT,
        cursor: str | None = None,
        before: str | None = None,
        raw: bool = False,
    ) -> dict[str, object]:
        """Return one incremental operator-facing section for a Mission Run."""

        run, observations = self._run_evidence(mission_run_id)
        entries = observations.refresh(observed_at=self._clock(), wait=False)
        narrative = self._public_narrative(mission_run_id)
        root = self._run_root(run)
        stack = self._stack_section(run)
        extra = None
        if section == "beliefs":
            extra = beliefs_section(
                root.path, str(run["mission_id"]), run.get("mission_mode")
            )
        elif section == "context":
            extra = context_section(root.transport, str(run["mission_id"]))
        elif section == "world":
            extra = self._world_view(run).section(
                live=run["status"] in _NONTERMINAL_STATUSES,
            )
        elif section == "stack":
            extra = stack
        summaries = load_mission_log_summaries(
            root.agent_storage, str(run["mission_id"])
        )
        # The projection keeps per-run paging state; it was serialized by the state
        # lock before evidence reading moved out of it.
        with self._projection_lock:
            return self._operator_projection.view(
                run=run,
                observations=entries,
                storage_root=root.agent_storage,
                environment_root=root.environment_artifacts,
                planner_root=root.planner_artifacts,
                artifact_inbox=self._artifact_inbox_for(run),
                narrative=narrative,
                debug=self.config.debug,
                section=section,
                limit=limit,
                cursor=cursor,
                before=before,
                raw=raw,
                run_root=root.path,
                operational_records=observations.operational_records(),
                summaries=summaries,
                stack=stack,
                extra=extra,
            )

    def mission_intent(self, mission_run_id: str, credential: str) -> dict[str, object]:
        with self._state_guard():
            state = self._load_state()
            run = self._authorize_run(state, mission_run_id, credential)
            activation = state["activations"].get(run.get("activation_request_id"))
            if not isinstance(activation, dict):
                raise HostAuthorizationError
            return {
                "mission_run_id": mission_run_id,
                "mission_intent": activation["mission_intent"],
                "source_authority": activation["source_authority"],
            }

    def export_receipt(self, mission_run_id: str, credential: str) -> dict[str, object]:
        """Write the terminal receipt to ``<run root>/mission-run-receipt.json``.

        Owner only, terminal runs only. Each export atomically replaces the
        previous file with the receipt as the Host projects it now (metadata,
        teardown and Run Root artifact references), so repeating it is safe.
        """

        with self._state_guard():
            run = dict(
                self._authorize_run(self._load_state(), mission_run_id, credential)
            )
        if run["status"] in _NONTERMINAL_STATUSES:
            raise HostConflictError(
                "mission_run_not_terminal",
                "a receipt can be exported only after the Mission Run ended",
            )
        overview = cast(
            dict[str, Any],
            self.operator_view(mission_run_id, section="overview", limit=1)[
                "overview"
            ],
        )
        receipt = dict(overview["receipt"])
        del receipt["export"]
        root = self._run_root(run)
        stack = self._stack_section(run)
        exported_at = self._clock()
        document = {
            "schema_version": 1,
            "kind": "mission_run_receipt",
            "exported_at": exported_at,
            "mission_id": run["mission_id"],
            "mission_run_id": mission_run_id,
            "run_root": str(root.path),
            "receipt": receipt,
            "terminal_detail": run.get("terminal_detail"),
            "stack": {
                "preset_id": stack.get("preset_id"),
                "teardown": stack.get("teardown"),
                "services": [
                    {
                        key: service.get(key)
                        for key in ("name", "state", "stop_mode", "stopped_at")
                    }
                    for service in cast(list[object], stack.get("services", []))
                    if isinstance(service, Mapping)
                ],
            },
            "artifacts": receipt_artifact_references(root.path),
        }
        encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        path = root.path / RECEIPT_EXPORT_NAME
        replaced = path.exists()
        # A unique temporary name: concurrent exports each replace atomically.
        descriptor, temporary = tempfile.mkstemp(
            dir=root.path, prefix=f".{RECEIPT_EXPORT_NAME}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return {
            "mission_run_id": mission_run_id,
            "path": str(path),
            "exported_at": exported_at,
            "byte_size": len(encoded),
            "replaced": replaced,
        }

    def cancel(
        self,
        *,
        mission_run_id: str,
        cancellation_request_id: str,
        credential: str,
    ) -> dict[str, object]:
        should_terminate = False
        with self._state_guard():
            state = self._load_state()
            run = self._authorize_run(state, mission_run_id, credential)
            existing = state["cancellations"].get(cancellation_request_id)
            if existing is not None:
                if existing.get("mission_run_id") != mission_run_id:
                    raise HostConflictError(
                        "cancellation_request_conflict",
                        "cancellation_request_id was reused for a different cancellation request",
                    )
                response = dict(existing["response"])
                should_terminate = run["status"] in _NONTERMINAL_STATUSES
            else:
                now = self._clock()
                response = {
                    "mission_run_id": mission_run_id,
                    "cancellation_request_id": cancellation_request_id,
                    "disposition": "cancellation_requested",
                    "status": run["status"],
                    "requested_at": now,
                }
                state["cancellations"][cancellation_request_id] = {
                    "mission_run_id": mission_run_id,
                    "response": response,
                }
                run["cancellation_requested"] = True
                run["cancellation_requested_at"] = now
                should_terminate = run["status"] in _NONTERMINAL_STATUSES
                self._save_state(state)

        if should_terminate:
            worker = self._workers.get(mission_run_id)
            if worker is not None:
                self._terminate_owned_worker(mission_run_id, worker)
            else:
                with self._state_guard():
                    state = self._load_state()
                    persisted = state["runs"].get(mission_run_id)
                    persisted_run = (
                        dict(persisted) if isinstance(persisted, dict) else None
                    )
                identity = (
                    WorkerIdentity.from_state(persisted_run)
                    if persisted_run is not None
                    else None
                )
                if identity is not None and identity.is_owned():
                    self._terminate_owned_worker(
                        mission_run_id, None, persisted_run=persisted_run
                    )
        return response

    def _run_worker(self, context: WorkerContext) -> None:
        cancellation_requested = False
        with self._state_guard():
            state = self._load_state()
            run = state["runs"].get(context.mission_run_id)
            if run is None or run["status"] not in _NONTERMINAL_STATUSES:
                return
            cancellation_requested = run.get("cancellation_requested") is True
        if cancellation_requested:
            with self._state_guard():
                state = self._load_state()
                run = state["runs"].get(context.mission_run_id)
                group_ready = (
                    isinstance(run, dict)
                    and run.get("worker_launch_state") == "group_ready"
                )
            if not group_ready:
                self._transition(
                    context.mission_run_id,
                    "cancelled",
                    terminal_classification="cancelled_by_owner",
                )
            return
        if os.environ.get(_WORKER_OWNERSHIP_ENV) and os.getpgrp() == os.getpid():
            with context.run_root.worker_log.open("ab", buffering=0) as log:
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
        self._transition(context.mission_run_id, "running")
        try:
            self._worker_entrypoint(context)
        except _WorkerCancelled:
            return
        except StackFailure as exc:
            _append_worker_log(context.run_root, self._clock(), traceback.format_exc())
            self._transition(
                context.mission_run_id,
                "failed",
                terminal_classification="stack_failed",
                # v1.5 ``log_artifact_id``: the failed service's or step's log.
                terminal_detail={
                    **exc.terminal_detail(),
                    "message": _terminal_detail_text(exc.message),
                },
            )
        except MissionRejectedError as exc:
            # This runs inside the Run Worker process; the state file under the
            # cross-process lock carries the detail back to the Host process.
            reason = _terminal_detail_text(exc.reason)
            _append_worker_log(
                context.run_root,
                self._clock(),
                f"mission rejected at intent: {reason}\n",
            )
            self._transition(
                context.mission_run_id,
                "failed",
                terminal_classification="mission_rejected",
                terminal_detail={
                    "kind": "mission_rejected",
                    "stage": "intent",
                    "reason": reason,
                },
            )
        except Exception as exc:  # noqa: BLE001 - worker failures become durable run state.
            stage = _failure_stage(exc)
            _append_worker_log(
                context.run_root,
                self._clock(),
                f"worker failed at {stage}\n{''.join(traceback.format_exception(exc))}",
            )
            self._transition(
                context.mission_run_id,
                "failed",
                terminal_classification="worker_failed",
                terminal_detail={
                    "kind": "worker_failed",
                    "stage": stage,
                    "error_type": type(exc).__name__,
                    "message": _terminal_detail_text(str(exc)),
                    # v1.5: the traceback is in the Run Worker log.
                    "log_artifact_id": WORKER_LOG_ARTIFACT_ID,
                },
            )
        else:
            self._transition(context.mission_run_id, "succeeded")

    def _report_rejection(self, mission_run_id: str, reason: str) -> None:
        """Publish the decision now; lifecycle stays active until owned teardown."""
        with self._state_guard():
            state = self._load_state()
            run = state["runs"].get(mission_run_id)
            if (
                not isinstance(run, dict)
                or run["status"] not in _NONTERMINAL_STATUSES
                or run.get("cancellation_requested") is True
            ):
                return
            run["terminal_detail"] = {
                "kind": "mission_rejected",
                "stage": "intent",
                "reason": _terminal_detail_text(reason),
            }
            self._save_state(state)

    def _prune_run_caches(self, *, requested_run_id: str | None = None) -> None:
        """Keep the current run, pending narratives and one requested old run.

        Observations, frames and narratives remain durable under each Run Root.
        Prune only at lifecycle boundaries or a cache miss, never on steady polls.
        """
        with self._state_guard():
            current = self._current_run(self._load_state())
        retained = set() if current is None else {str(current["mission_run_id"])}
        if requested_run_id is not None:
            retained.add(requested_run_id)
        with self._narrative_guard:
            retained.update(self._narrative_watch)
            for run_id in self._narrative_records.keys() - retained:
                del self._narrative_records[run_id]
        with self._evidence_lock:
            for run_id in self._run_observations.keys() - retained:
                del self._run_observations[run_id]
        with self._projection_lock:
            self._operator_projection.discard_runs_except(retained)
        with self._lock:
            for run_id in self._world_views.keys() - retained:
                del self._world_views[run_id]

    def _run_evidence(
        self, mission_run_id: str
    ) -> tuple[dict[str, Any], RunObservations]:
        """Return the run record and its long-lived observations, without refreshing."""

        run = self._evidence_run(mission_run_id)
        if mission_run_id not in self._run_observations:
            self._prune_run_caches(requested_run_id=mission_run_id)
        with self._evidence_lock:
            observations = self._run_observations.get(mission_run_id)
            if observations is None:
                mission_id = str(run["mission_id"])
                log_path = self._run_root(run).path / "observations.json"
                if self._evidence_source is not None:
                    observations = RunObservations(
                        log_path, mission_id, source=self._evidence_source
                    )
                else:
                    observations = RunObservations(
                        log_path, mission_id, tailer=self._evidence_tailer(run)
                    )
                self._run_observations[mission_run_id] = observations
        return run, observations

    def _evidence_tailer(self, run: Mapping[str, object]) -> EvidenceTailer:
        root = self._run_root(run)
        return EvidenceTailer(
            str(run["mission_id"]),
            operational_log_root=root.operational_log,
            transport_root=root.transport,
        )

    def _issued_observations(
        self, mission_run_id: str
    ) -> tuple[dict[str, Any], tuple[dict[str, object], ...]]:
        """Refresh outside the state lock; a concurrent refresh serves its last entries."""

        run, observations = self._run_evidence(mission_run_id)
        return run, observations.refresh(observed_at=self._clock(), wait=False)

    def _narrative_loop(self, poll_seconds: float) -> None:
        while not self._narrative_stop.wait(poll_seconds):
            try:
                self.narrative_tick()
            except Exception:  # noqa: BLE001, S112 - the next tick retries.
                continue

    def _narrative_record(self, mission_run_id: str) -> RunNarrativeRecord:
        record = self._narrative_records.get(mission_run_id)
        if record is None:
            run = self._load_state()["runs"][mission_run_id]
            record = RunNarrativeRecord(
                self._run_root(run).path / "narrative.json",
                mission_run_id,
            )
            self._narrative_records[mission_run_id] = record
        return record

    def _public_narrative(self, mission_run_id: str) -> dict[str, object]:
        terminal_without_model = False
        if self._narrative_summarizer is None:
            with self._state_guard():
                run = self._load_state()["runs"].get(mission_run_id)
            terminal_without_model = (
                isinstance(run, dict) and run["status"] not in _NONTERMINAL_STATUSES
            )
        if mission_run_id not in self._narrative_records:
            self._prune_run_caches(requested_run_id=mission_run_id)
        with self._narrative_guard:
            record = self._narrative_record(mission_run_id)
            if terminal_without_model and not record.terminal_generated:
                record.publish_unavailable(generated_at=self._clock(), terminal=True)
            return record.public_narrative()

    def _narrative_due(
        self, record: RunNarrativeRecord, *, terminal: bool, now: str
    ) -> bool:
        if terminal:
            return not record.terminal_generated
        last_attempt_at = record.last_attempt_at
        if last_attempt_at is None:
            return True
        return (
            datetime.fromisoformat(now) - datetime.fromisoformat(last_attempt_at)
        ).total_seconds() >= self._narrative_interval_seconds

    def _transition(
        self,
        mission_run_id: str,
        status: str,
        *,
        terminal_classification: str | None = None,
        terminal_detail: Mapping[str, object] | None = None,
        cancellation_tree_exited: bool = False,
    ) -> None:
        if cancellation_tree_exited:
            with self._state_guard():
                completed_run = self._load_state()["runs"].get(mission_run_id)
            if isinstance(completed_run, dict):
                self._mark_stack_stopped(self._run_root(completed_run))
        with self._state_guard():
            state = self._load_state()
            run = state["runs"].get(mission_run_id)
            if run is None or run["status"] not in _NONTERMINAL_STATUSES:
                return
            if (
                status != "running"
                and run.get("cancellation_requested") is True
                and run.get("worker_launch_state") == "group_ready"
                and not cancellation_tree_exited
            ):
                return
            if status != "running" and run.get("cancellation_requested") is True:
                status = "cancelled"
                terminal_classification = "cancelled_by_owner"
                terminal_detail = None
            run["status"] = status
            now = self._clock()
            if status == "running":
                run["started_at"] = now
            else:
                run["finished_at"] = now
                run["terminal_classification"] = terminal_classification
                run["terminal_detail"] = (
                    None if terminal_detail is None else dict(terminal_detail)
                )
            self._save_state(state)

    def _mark_stack_stopped(self, root: RunRoot) -> None:
        """Record a forcibly reaped stack truthfully.

        Services the Run Worker had not finished stopping were killed with its
        process tree, so they end ``stopped`` with ``stop_mode`` ``forced``;
        services it had already stopped keep their recorded mode. The
        teardown's ``finished_at`` becomes the time the Host verified the exit.
        """
        status = self._stack_status_cache.get(root.stack_status)
        if status is None:
            return
        now = self._clock()
        services = [
            {
                **{
                    key: value for key, value in service.items() if key != "waiting_for"
                },
                "state": "stopped",
                "importance": "routine",
                "stop_mode": "forced",
                "stopped_at": now,
            }
            if service.get("state") in {"ready", "starting", "stopping"}
            else dict(service)
            for service in status.get("services", [])
        ]
        # A step reaped mid-run ends `stopped`; its end time was not observed.
        steps = [
            {**step, "state": "stopped"}
            if isinstance(step, dict) and step.get("state") == "running"
            else step
            for step in status.get("steps", [])
        ]
        recorded = status.get("teardown")
        teardown = (
            dict(recorded)
            if isinstance(recorded, Mapping)
            else {"started_at": None, "finished_at": None, "stop_order": []}
        )
        if teardown.get("finished_at") is None:
            teardown["finished_at"] = now
        if (
            services == status.get("services")
            and steps == status.get("steps", [])
            and teardown == recorded
            and "step" not in status
        ):
            return
        path = root.stack_status.with_name(".stack-status.host.tmp")
        try:
            path.write_text(
                json.dumps(
                    {
                        **{
                            key: value
                            for key, value in status.items()
                            if key not in {"step", "steps"}
                        },
                        "services": services,
                        **({"steps": steps} if steps else {}),
                        "teardown": teardown,
                        "updated_at": now,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(path, root.stack_status)
        except OSError:
            # Lifecycle authority still records the verified process-tree exit.
            pass

    def _terminate_owned_worker(
        self,
        mission_run_id: str,
        worker: WorkerHandle | None,
        *,
        persisted_run: Mapping[str, object] | None = None,
        reconcile: bool = True,
    ) -> bool:
        identity = worker.identity if worker is not None else None
        if identity is None and persisted_run is not None:
            identity = WorkerIdentity.from_state(persisted_run)
        if identity is None:
            return False
        process_group_id = identity.process_group_id
        if not identity.is_owned():
            return self._finish_unowned_cancellation(mission_run_id, process_group_id)
        # Let the worker unwind through reverse supervisor teardown first.
        if _process_start_time(identity.pid) == identity.process_start_time:
            try:
                os.kill(identity.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._wait_for_owned_tree(identity, worker, timeout=7.0)
        for selected_signal in (signal.SIGTERM, signal.SIGKILL):
            if not identity.is_owned() and self._process_group_exists(process_group_id):
                return self._finish_unowned_cancellation(
                    mission_run_id, process_group_id
                )
            owned_worker_group = _process_start_time(
                identity.pid
            ) == identity.process_start_time or _owned_group_member_exists(
                process_group_id,
                identity.process_session_id,
                identity.ownership_token,
            )
            if owned_worker_group and self._process_group_exists(process_group_id):
                self._signal_process_group(process_group_id, selected_signal)
            for group in _owned_detached_groups(
                identity.ownership_token, process_group_id
            ):
                self._signal_process_group(group, selected_signal)
            self._wait_for_owned_tree(identity, worker, timeout=1.0)
        exited = not self._process_group_exists(
            process_group_id
        ) and not _owned_detached_groups(identity.ownership_token, process_group_id)
        if exited and reconcile:
            self._transition(
                mission_run_id,
                "cancelled",
                terminal_classification="cancelled_by_owner",
                cancellation_tree_exited=True,
            )
            self._workers.pop(mission_run_id, None)
        elif not exited and reconcile:
            self._start_reconciler(
                mission_run_id,
                self._reconcile_cancelled_worker,
                process_group_id,
                worker,
            )
        return exited

    def _wait_for_owned_tree(
        self,
        identity: WorkerIdentity,
        worker: WorkerHandle | None,
        *,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if worker is not None:
                worker.join(timeout=0.02)
            if not self._process_group_exists(
                identity.process_group_id
            ) and not _owned_detached_groups(
                identity.ownership_token, identity.process_group_id
            ):
                return
            time.sleep(0.05)

    def _finish_unowned_cancellation(
        self, mission_run_id: str, process_group_id: int
    ) -> bool:
        exited = not self._process_group_exists(process_group_id)
        if exited:
            self._transition(
                mission_run_id,
                "cancelled",
                terminal_classification="cancelled_by_owner",
                cancellation_tree_exited=True,
            )
        else:
            with self._state_guard():
                state = self._load_state()
                run = state["runs"].get(mission_run_id)
                if isinstance(run, dict) and run["status"] in _NONTERMINAL_STATUSES:
                    run["status"] = "failed"
                    run["finished_at"] = self._clock()
                    run["terminal_classification"] = "host_interrupted"
                    self._save_state(state)
        self._workers.pop(mission_run_id, None)
        return exited

    def _start_reconciler(
        self,
        mission_run_id: str,
        target: Callable[..., None],
        *args: object,
    ) -> None:
        with self._lock:
            existing = self._reconcilers.get(mission_run_id)
            if existing is not None and existing.is_alive():
                return

            def reconcile() -> None:
                try:
                    target(mission_run_id, *args)
                finally:
                    with self._lock:
                        current = self._reconcilers.get(mission_run_id)
                        if current is current_thread():
                            self._reconcilers.pop(mission_run_id, None)

            thread = Thread(
                target=reconcile,
                name=f"runtime-host-reconcile-{mission_run_id}",
                daemon=True,
            )
            self._reconcilers[mission_run_id] = thread
            thread.start()

    def _reconcile_cancelled_worker(
        self, mission_run_id: str, process_group_id: int, worker: WorkerHandle | None
    ) -> None:
        with self._state_guard():
            run = self._load_state()["runs"].get(mission_run_id, {})
            token = str(run.get("worker_ownership_token", ""))
        while self._process_group_exists(process_group_id) or (
            token and _owned_detached_groups(token, process_group_id)
        ):
            self._wait_for_process_group_exit(process_group_id, worker, timeout=0.25)
            time.sleep(0.05)
        self._transition(
            mission_run_id,
            "cancelled",
            terminal_classification="cancelled_by_owner",
            cancellation_tree_exited=True,
        )
        self._workers.pop(mission_run_id, None)

    def _reconcile_recovered_worker(
        self, mission_run_id: str, process_group_id: int, cancelled: bool
    ) -> None:
        with self._state_guard():
            run = self._load_state()["runs"].get(mission_run_id, {})
            token = str(run.get("worker_ownership_token", ""))
        while self._process_group_exists(process_group_id) or (
            token and _owned_detached_groups(token, process_group_id)
        ):
            time.sleep(0.25)
        self._transition(
            mission_run_id,
            "cancelled" if cancelled else "failed",
            terminal_classification=(
                "cancelled_by_owner" if cancelled else "host_interrupted"
            ),
            cancellation_tree_exited=True,
        )

    @staticmethod
    def _signal_process_group(process_group_id: int, selected_signal: int) -> None:
        try:
            os.killpg(process_group_id, selected_signal)
        except ProcessLookupError:
            pass

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        # killpg(0) also succeeds for zombie-only groups. A reconstructed Host
        # has no ChildHandle to reap them, but they cannot retain live services.
        try:
            for path in Path("/proc").iterdir():
                if not path.name.isdigit():
                    continue
                identity = _process_identity(int(path.name))
                if identity is not None and identity[0] == process_group_id:
                    return True
        except OSError:
            return True  # An unreadable procfs cannot prove that the group died.
        return False

    @classmethod
    def _wait_for_process_group_exit(
        cls, process_group_id: int, worker: WorkerHandle | None, *, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        while (
            cls._process_group_exists(process_group_id) and time.monotonic() < deadline
        ):
            if worker is not None:
                worker.join(timeout=0.02)
            else:
                time.sleep(0.02)

    @staticmethod
    def _persisted_worker_is_owned(run: Mapping[str, object]) -> bool:
        if run.get("worker_launch_state") != "group_ready":
            return False
        pid = run.get("worker_pid")
        process_group_id = run.get("worker_process_group_id")
        process_session_id = run.get("worker_session_id")
        start_time = run.get("worker_start_time")
        ownership_token = run.get("worker_ownership_token")
        leader_matches = (
            isinstance(pid, int)
            and process_group_id == pid
            and process_session_id == pid
            and isinstance(start_time, str)
            and _process_start_time(pid) == start_time
        )
        if leader_matches:
            return True
        if not (
            isinstance(process_group_id, int)
            and isinstance(process_session_id, int)
            and process_group_id == process_session_id
            and isinstance(ownership_token, str)
        ):
            return False
        return _owned_group_member_exists(
            process_group_id, process_session_id, ownership_token
        )

    @contextmanager
    def _state_guard(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock, self._state_lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "version": 1,
                "activations": {},
                "session_verifiers": {},
                "cancellations": {},
                "runs": {},
                "latest_run_by_preset": {},
                "current_run_id": None,
            }
        if (
            not isinstance(raw, dict)
            or raw.get("version") != 1
            or not isinstance(raw.get("activations"), dict)
            or not isinstance(raw.get("session_verifiers"), dict)
            or not isinstance(raw.get("runs"), dict)
        ):
            raise RuntimeError("runtime host state is invalid")
        raw.setdefault("cancellations", {})
        # Absent in state written before API v1.5: no readiness history yet.
        raw.setdefault("latest_run_by_preset", {})
        if not isinstance(raw["cancellations"], dict) or not isinstance(
            raw["latest_run_by_preset"], dict
        ):
            raise RuntimeError("runtime host state is invalid")  # noqa: TRY004
        return raw

    def _save_state(self, state: Mapping[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, allow_nan=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self._state_path)

    @staticmethod
    def _current_run(state: Mapping[str, Any]) -> dict[str, Any] | None:
        run_id = state.get("current_run_id")
        if not isinstance(run_id, str):
            return None
        run = state["runs"].get(run_id)
        return run if isinstance(run, dict) else None

    @staticmethod
    def _authorize_run(
        state: Mapping[str, Any], mission_run_id: str, credential: str
    ) -> dict[str, Any]:
        run = state["runs"].get(mission_run_id)
        if not isinstance(run, dict):
            raise HostAuthorizationError
        verifier = state["session_verifiers"].get(run.get("console_session_id"))
        if not _verify_credential(credential, verifier):
            raise HostAuthorizationError
        return run


def _digest_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _observation_envelopes(
    entries: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "observation_sequence": entry["observation_sequence"],
            "observed_at": entry["observed_at"],
            "item": entry["item"],
        }
        for entry in entries
    ]


def _process_start_time(pid: int) -> str | None:
    identity = _process_identity(pid)
    return None if identity is None else identity[2]


def _process_identity(pid: int) -> tuple[int, int, str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    if len(fields) <= 19 or fields[0] in {"Z", "X"}:
        return None
    try:
        return int(fields[2]), int(fields[3]), fields[19]
    except ValueError:
        return None


def _owned_group_member_exists(
    process_group_id: int, process_session_id: int, ownership_token: str
) -> bool:
    expected = f"{_WORKER_OWNERSHIP_ENV}={ownership_token}".encode()
    try:
        process_paths = tuple(Path("/proc").iterdir())
    except OSError:
        return False
    for process_path in process_paths:
        if not process_path.name.isdigit():
            continue
        identity = _process_identity(int(process_path.name))
        if identity is None or identity[:2] != (
            process_group_id,
            process_session_id,
        ):
            continue
        try:
            environment = (process_path / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if expected in environment:
            return True
    return False


def _owned_detached_groups(ownership_token: str, worker_group: int) -> set[int]:
    """Find owned detached Harbor groups, leaving the restoration guardian alone."""
    expected = f"{_WORKER_OWNERSHIP_ENV}={ownership_token}".encode()
    groups: set[int] = set()
    try:
        paths = tuple(Path("/proc").iterdir())
    except OSError:
        return groups
    for path in paths:
        if not path.name.isdecimal():
            continue
        identity = _process_identity(int(path.name))
        if identity is None or identity[0] == worker_group:
            continue
        try:
            if expected not in (path / "environ").read_bytes().split(b"\0"):
                continue
            if b"--guard-parent" in (path / "cmdline").read_bytes().split(b"\0"):
                continue
        except OSError:
            continue
        # A detached session is private to this stack, unlike external services.
        if identity[0] == identity[1]:
            groups.add(identity[0])
    return groups


def _credential_verifier(credential: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(credential.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_credential(credential: str, verifier: object) -> bool:
    if not isinstance(verifier, str):
        return False
    try:
        algorithm, salt_hex, expected_hex = verifier.split("$", 2)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            credential.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=2**14,
            r=8,
            p=1,
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (ValueError, TypeError):
        return False


def _public_run(run: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "mission_id",
        "mission_run_id",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "terminal_classification",
    )
    public = {key: run[key] for key in keys if key in run}
    # v1.2 fields are always present; runs recorded before v1.2 report null.
    public["stack"] = run.get("stack")
    public["terminal_detail"] = run.get("terminal_detail")
    return public


def _history_key(run: Mapping[str, object]) -> tuple[str, str]:
    """Run history order: the Host's ISO-8601 UTC ``created_at``, then run id."""

    created_at = run.get("created_at")
    return (
        created_at if isinstance(created_at, str) else "",
        str(run["mission_run_id"]),
    )


def _history_toggles(run: Mapping[str, object]) -> dict[str, object] | None:
    """The launch toggles ``stack`` omits; ``None`` for runs before v1.2."""

    options = run.get("stack_options")
    if not isinstance(options, Mapping):
        return None
    return {
        "update_ownership": options.get("update_ownership"),
        "simulation_limit_seconds": options.get("simulation_limit_seconds"),
    }


def _terminal_detail_text(value: object) -> str:
    """Redact credential-shaped text, drop control characters, cap the length."""

    text = value if isinstance(value, str) else str(value)
    safe, _ = sanitize_payload({"value": text})
    redacted = safe.get("value")
    text = redacted if isinstance(redacted, str) else ""
    printable = "".join(
        character if character.isprintable() else " " for character in text
    )
    return " ".join(printable.split())[:_TERMINAL_DETAIL_TEXT_LIMIT]


def _failure_stage(exc: BaseException) -> str:
    """Best-effort closed-loop stage at which a worker exception was raised."""

    frames = {frame.name for frame in traceback.extract_tb(exc.__traceback__)}
    if "_run_hyper_revision" in frames:
        return "hyper"
    if "run_closed_loop_demo" in frames:
        return "closed_loop"
    return "worker"


def _append_worker_log(run_root: RunRoot, now: str, text: str) -> None:
    try:
        run_root.worker_log.parent.mkdir(parents=True, exist_ok=True)
        with run_root.worker_log.open("a", encoding="utf-8") as handle:
            handle.write(f"{now} {text}")
    except OSError:
        pass


def _reset_locks_in_forked_worker(host: RuntimeHost) -> None:
    """Give a forked Run Worker fresh in-process locks.

    A Host thread may hold the state lock at fork time; the child's copy would
    then stay locked forever. The cross-process ``flock`` still serializes state.
    """

    reference = weakref.ref(host)

    def reset() -> None:
        selected = reference()
        if selected is not None:
            selected._lock = RLock()
            selected._narrative_guard = Lock()
            selected._narrative_attempt = Lock()
            selected._projection_lock = Lock()
            selected._evidence_lock = Lock()
            selected._stack_status_cache = JsonFileCache(dict, max_bytes=1024 * 1024)

    os.register_at_fork(after_in_child=reset)
