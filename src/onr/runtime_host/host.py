"""Durable single-mission Runtime Host application service."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Event as ProcessEvent
from multiprocessing import Process
from pathlib import Path
from threading import Lock, RLock, Thread, current_thread
from typing import Any, Protocol

from onr.adapters.file_transport import FileTransport
from onr.contracts.hyper_agent import MissionInput
from onr.runtime.cli import run_closed_loop_demo
from onr.runtime.composition import RuntimeComposition
from onr.runtime.config import RuntimeConfig
from onr.runtime_host.artifacts import PublicArtifactInbox
from onr.runtime_host.narrative import (
    RunNarrativeRecord,
    RunNarrativeSummarizer,
    sanitize_narrative_text,
)
from onr.runtime_host.observations import (
    ACTIVITY_MAPPING_VERSION,
    DEFAULT_PAGE_SIZE,
    OBSERVATION_SCHEMA_VERSION,
    EvidenceSource,
    FileEvidenceSource,
    ObservationLog,
    decode_cursor,
    encode_cursor,
    map_activities,
    page_entries,
)
from onr.viewer.trace import TraceProjection, TraceViewItem

Clock = Callable[[], str]
IdGenerator = Callable[[str], str]
WorkerEntrypoint = Callable[["WorkerContext"], None]
_NONTERMINAL_STATUSES = {"queued", "running", "awaiting_human_decision"}
_WORKER_IDENTITY = "runtime_host.closed_loop_demo"
_WORKER_OWNERSHIP_ENV = "ONR_RUNTIME_HOST_WORKER_TOKEN"
_WORKER_START_TIMEOUT_SECONDS = 5.0


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
        return leader_matches or (
            self.process_group_id == self.process_session_id
            and _owned_group_member_exists(
                self.process_group_id,
                self.process_session_id,
                self.ownership_token,
            )
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


@dataclass(frozen=True, slots=True)
class RuntimeWorkerOptions:
    repo_root: Path
    planner_artifacts: Path | None = None
    recursion_limit: int = 120
    simulation_limit_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.recursion_limit < 1:
            raise ValueError("worker recursion limit must be positive")
        if self.simulation_limit_seconds <= 0:
            raise ValueError("worker simulation limit must be positive")


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


def runtime_worker(context: WorkerContext) -> None:
    """Run an operator Mission through the current closed-loop runtime seam."""

    if context.config.transport.backend != "file":
        raise RuntimeError("runtime Host worker requires transport.backend=file")
    mission_input = MissionInput(
        mission_id=context.mission_id,
        mission_text=context.mission_intent,
        source_authority=context.source_authority,
    )
    runtime = RuntimeComposition(
        context.config,
        FileTransport(context.config.transport.root),
    )
    planner_artifacts = context.options.planner_artifacts
    if planner_artifacts is None:
        planner_artifacts = (
            context.config.storage.root
            / "runtime-host"
            / "planner-artifacts"
            / context.mission_run_id
        )
    with runtime.runtime_session():
        run_closed_loop_demo(
            runtime,
            mission_input,
            repo_root=context.options.repo_root,
            planner_artifacts=planner_artifacts,
            recursion_limit=context.options.recursion_limit,
            simulation_limit_seconds=context.options.simulation_limit_seconds,
        )


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
        narrative_summarizer: RunNarrativeSummarizer | None = None,
        narrative_interval_seconds: float = 30.0,
    ) -> None:
        self.config = config
        self.root = config.storage.root / "runtime-host"
        self._state_path = self.root / "state.json"
        self._state_lock_path = self.root / "state.lock"
        self._clock = clock
        self._generate_id = generate_id
        self._worker_entrypoint = worker_entrypoint or runtime_worker
        self._launch_worker = launch_worker or _launch_process
        self._worker_options = worker_options or RuntimeWorkerOptions(repo_root=Path.cwd())
        self._evidence_source = evidence_source or FileEvidenceSource(
            storage_root=config.storage.root,
            transport_backend=config.transport.backend,
            transport_root=config.transport.root,
        )
        self._artifact_inbox = PublicArtifactInbox(
            artifact_inbox_root or config.storage.root / "artifact-inbox"
        )
        self._narrative_summarizer = narrative_summarizer
        self._narrative_interval_seconds = narrative_interval_seconds
        self._lock = RLock()
        self._narrative_locks: dict[str, Lock] = {}
        self._narrative_records: dict[str, RunNarrativeRecord] = {}
        self._workers: dict[str, WorkerHandle] = {}
        self._reconcilers: dict[str, Thread] = {}
        recoveries: list[tuple[str, dict[str, Any]]] = []
        with self._state_guard():
            state = self._load_state()
            changed = False
            now = self._clock()
            for run in state["runs"].values():
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

    def activate(
        self,
        *,
        activation_request_id: str,
        console_session_id: str,
        mission_intent: str,
        source_authority: str,
        credential: str,
    ) -> dict[str, object]:
        request_fields = {
            "activation_request_id": activation_request_id,
            "console_session_id": console_session_id,
            "mission_intent": mission_intent,
            "source_authority": source_authority,
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
                "cancellation_requested": False,
                "worker_launch_state": "launching",
            }
            state["current_run_id"] = mission_run_id
            self._save_state(state)

        context = WorkerContext(
            config=self.config,
            mission_id=mission_id,
            mission_run_id=mission_run_id,
            activation_request_id=activation_request_id,
            console_session_id=console_session_id,
            mission_intent=mission_intent,
            source_authority=source_authority,
            options=self._worker_options,
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

    def observations(
        self,
        mission_run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        with self._state_guard():
            run, log = self._refresh_observations(mission_run_id)
            max_sequence = len(log.entries)
            after = (
                0
                if cursor is None
                else decode_cursor(
                    cursor,
                    mission_run_id=mission_run_id,
                    max_sequence=max_sequence,
                )
            )
            page, last_sequence = page_entries(
                log.entries, after=after, limit=limit or DEFAULT_PAGE_SIZE
            )
            observations = _observation_envelopes(page)
            return {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "mission_id": run["mission_id"],
                "mission_run_id": mission_run_id,
                "observations": observations,
                "next_cursor": (
                    None
                    if last_sequence is None
                    else encode_cursor(mission_run_id, last_sequence)
                ),
            }

    def narrative(self, mission_run_id: str) -> dict[str, object]:
        with self._state_guard():
            run, log = self._refresh_observations(mission_run_id)
            record = self._narrative_record(mission_run_id)
            attempt_lock = self._narrative_locks.setdefault(mission_run_id, Lock())
            should_attempt = self._should_attempt_narrative(
                run, record, len(log.entries), self._clock()
            )
            response = self._narrative_response(run, mission_run_id, record)
        if self._narrative_summarizer is None or not should_attempt:
            return response
        if not attempt_lock.acquire(blocking=False):
            with self._state_guard():
                current = self._narrative_record(mission_run_id)
                return self._narrative_response(run, mission_run_id, current)
        try:
            with self._state_guard():
                run, log = self._refresh_observations(mission_run_id)
                record = self._narrative_record(mission_run_id)
                now = self._clock()
                max_sequence = len(log.entries)
                if not self._should_attempt_narrative(
                    run, record, max_sequence, now
                ):
                    return self._narrative_response(run, mission_run_id, record)
                terminal = run["status"] not in _NONTERMINAL_STATUSES
                observations = _observation_envelopes(log.entries)
                mission_id = str(run["mission_id"])
                record.begin_attempt(started_at=now, terminal=terminal)
            try:
                generated = self._narrative_summarizer.summarize_narrative(
                    mission_id=mission_id,
                    mission_run_id=mission_run_id,
                    terminal=terminal,
                    observations=observations,
                )
                text = sanitize_narrative_text(generated)
            except Exception:  # noqa: BLE001 - failures publish only typed evidence.
                text = None
            with self._state_guard():
                record = self._narrative_record(mission_run_id)
                if text is None:
                    record.publish_unavailable(generated_at=now, terminal=terminal)
                else:
                    record.publish_available(
                        text=text,
                        generated_at=now,
                        source_watermark=max_sequence,
                        terminal=terminal,
                    )
                return self._narrative_response(run, mission_run_id, record)
        finally:
            attempt_lock.release()

    def activities(
        self,
        mission_run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        with self._state_guard():
            run, log = self._refresh_observations(mission_run_id)
            activities = map_activities(log.entries)
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

        with self._state_guard():
            state = self._load_state()
            run = state["runs"].get(mission_run_id)
            if not isinstance(run, dict):
                raise HostNotFoundError
            return self._artifact_inbox.artifacts(
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

        with self._state_guard():
            state = self._load_state()
            run = state["runs"].get(mission_run_id)
            if not isinstance(run, dict):
                raise HostNotFoundError
            return self._artifact_inbox.artifact_content(
                str(run["mission_id"]),
                mission_run_id,
                artifact_id,
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

        with self._state_guard():
            state = self._load_state()
            run = state["runs"].get(mission_run_id)
            if not isinstance(run, dict):
                raise HostNotFoundError
            return self._artifact_inbox.conversation_entries(
                str(run["mission_id"]),
                mission_run_id,
                artifact_id,
                cursor=cursor,
                limit=limit,
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
                    persisted_run = dict(persisted) if isinstance(persisted, dict) else None
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
        self._transition(context.mission_run_id, "running")
        try:
            self._worker_entrypoint(context)
        except Exception:  # noqa: BLE001 - worker failures become durable run state.
            self._transition(
                context.mission_run_id,
                "failed",
                terminal_classification="worker_failed",
            )
        else:
            self._transition(context.mission_run_id, "succeeded")

    def _refresh_observations(
        self, mission_run_id: str
    ) -> tuple[dict[str, Any], ObservationLog]:
        state = self._load_state()
        run = state["runs"].get(mission_run_id)
        if not isinstance(run, dict):
            raise HostNotFoundError
        try:
            records = list(self._evidence_source.records(str(run["mission_id"])))
        except Exception:  # noqa: BLE001 - retain the last committed evidence.
            records = []
        items = _project_evidence(records)
        log = ObservationLog(
            self.root / "observations" / f"{mission_run_id}.json"
        )
        log.ingest(items, observed_at=self._clock())
        return run, log

    def _narrative_record(self, mission_run_id: str) -> RunNarrativeRecord:
        record = self._narrative_records.get(mission_run_id)
        if record is None:
            record = RunNarrativeRecord(
                self.root / "narratives" / f"{mission_run_id}.json", mission_run_id
            )
            self._narrative_records[mission_run_id] = record
        return record

    def _should_attempt_narrative(
        self,
        run: Mapping[str, object],
        record: RunNarrativeRecord,
        max_sequence: int,
        now: str,
    ) -> bool:
        if self._narrative_summarizer is None:
            return False
        if run["status"] not in _NONTERMINAL_STATUSES:
            return not record.terminal_generated
        if max_sequence <= record.source_watermark:
            return False
        last_attempt_at = record.last_attempt_at
        if last_attempt_at is None:
            return True
        return (
            datetime.fromisoformat(now) - datetime.fromisoformat(last_attempt_at)
        ).total_seconds() >= self._narrative_interval_seconds

    @staticmethod
    def _narrative_response(
        run: Mapping[str, object],
        mission_run_id: str,
        record: RunNarrativeRecord,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "mission_id": run["mission_id"],
            "mission_run_id": mission_run_id,
            "narrative": record.public_narrative(),
        }

    def _transition(
        self,
        mission_run_id: str,
        status: str,
        *,
        terminal_classification: str | None = None,
        cancellation_tree_exited: bool = False,
    ) -> None:
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
            run["status"] = status
            now = self._clock()
            if status == "running":
                run["started_at"] = now
            else:
                run["finished_at"] = now
                run["terminal_classification"] = terminal_classification
            self._save_state(state)

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
        self._signal_process_group(process_group_id, signal.SIGTERM)
        self._wait_for_process_group_exit(process_group_id, worker, timeout=1.0)
        if self._process_group_exists(process_group_id):
            if not identity.is_owned():
                return self._finish_unowned_cancellation(
                    mission_run_id, process_group_id
                )
            self._signal_process_group(process_group_id, signal.SIGKILL)
            self._wait_for_process_group_exit(process_group_id, worker, timeout=3.0)
        exited = not self._process_group_exists(process_group_id)
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
        while self._process_group_exists(process_group_id):
            self._wait_for_process_group_exit(process_group_id, worker, timeout=0.25)
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
        while self._process_group_exists(process_group_id):
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
        return True

    @classmethod
    def _wait_for_process_group_exit(
        cls, process_group_id: int, worker: WorkerHandle | None, *, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        while cls._process_group_exists(process_group_id) and time.monotonic() < deadline:
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
        if not isinstance(raw["cancellations"], dict):
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


def _project_evidence(records: Sequence[object]) -> tuple[TraceViewItem, ...]:
    """Project heterogeneous public records without bypassing the redaction seam."""

    groups: dict[str, tuple[int, list[Any]]] = {}
    for index, record in enumerate(records):
        key = _projection_batch_key(record)
        group = groups.get(key)
        if group is None:
            group = (index, [])
            groups[key] = group
        group[1].append(record)
    projection = TraceProjection()
    return tuple(
        item
        for _, batch in sorted(groups.values(), key=lambda group: group[0])
        for item in projection.project(batch)
    )


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


def _projection_batch_key(record: object) -> str:
    if isinstance(record, str):
        try:
            decoded = json.loads(record)
        except (TypeError, ValueError):
            return "malformed"
        record = decoded
    if not isinstance(record, Mapping):
        return "malformed"
    keys = set(record)
    if "entry_state" in keys or "transitions" in keys and "states" in keys:
        return "statechart"
    if "record_id" in keys:
        return "operational_log"
    if "summary_id" in keys:
        return "summary"
    if "feedback_id" in keys:
        return "maneuver_feedback"
    if "request_id" in keys and "requester" in keys:
        return "replan_request"
    if "command_id" in keys and "command_kind" in keys:
        return "command"
    if "command_id" in keys and "target_service" in keys:
        return "receipt"
    if "command_id" in keys:
        return "outcome"
    if "event_id" in keys:
        return "transport_event"
    if "version" in keys or "source_references" in keys:
        return "snapshot"
    if "record_revision" in keys or "active_configuration" in keys:
        return "fsm_execution"
    if "transition_candidates" in keys:
        return "fsm_status"
    return "malformed"


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
    if len(fields) <= 19:
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


def _credential_verifier(credential: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        credential.encode("utf-8"), salt=salt, n=2**14, r=8, p=1
    )
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
    return {key: run[key] for key in keys if key in run}
