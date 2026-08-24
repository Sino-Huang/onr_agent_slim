"""Durable single-mission Runtime Host application service."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from multiprocessing import Process
from pathlib import Path
from threading import RLock
from typing import Any

from onr.adapters.file_transport import FileTransport
from onr.contracts.hyper_agent import MissionInput
from onr.runtime.cli import run_closed_loop_demo
from onr.runtime.composition import RuntimeComposition
from onr.runtime.config import RuntimeConfig

Clock = Callable[[], str]
IdGenerator = Callable[[str], str]
WorkerEntrypoint = Callable[["WorkerContext"], None]
WorkerLauncher = Callable[[Callable[[], None]], None]
_NONTERMINAL_STATUSES = {"queued", "running", "awaiting_human_decision"}
_WORKER_IDENTITY = "runtime_host.closed_loop_demo"


class HostConflictError(Exception):
    """A stable conflict suitable for translation at the HTTP boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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


def _launch_process(callback: Callable[[], None]) -> None:
    Process(target=callback, name="runtime-host-worker", daemon=False).start()


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
    ) -> None:
        self.config = config
        self.root = config.storage.root / "runtime-host"
        self._state_path = self.root / "state.json"
        self._clock = clock
        self._generate_id = generate_id
        self._worker_entrypoint = worker_entrypoint or runtime_worker
        self._launch_worker = launch_worker or _launch_process
        self._worker_options = worker_options or RuntimeWorkerOptions(repo_root=Path.cwd())
        self._lock = RLock()
        with self._lock:
            state = self._load_state()
            changed = False
            now = self._clock()
            for run in state["runs"].values():
                if run["status"] in _NONTERMINAL_STATUSES:
                    run["status"] = "failed"
                    run["finished_at"] = now
                    run["terminal_classification"] = "host_interrupted"
                    changed = True
            if changed:
                self._save_state(state)

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
        with self._lock:
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
                "console_session_id": console_session_id,
                "worker_identity": _WORKER_IDENTITY,
                "status": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "terminal_classification": None,
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
        try:
            self._launch_worker(lambda: self._run_worker(context))
        except Exception:
            self._transition(
                mission_run_id,
                "failed",
                terminal_classification="worker_start_failed",
            )
        return dict(response)

    def current_run(self) -> dict[str, object] | None:
        with self._lock:
            run = self._current_run(self._load_state())
            return None if run is None else _public_run(run)

    def _run_worker(self, context: WorkerContext) -> None:
        self._transition(context.mission_run_id, "running")
        try:
            self._worker_entrypoint(context)
        except Exception:
            self._transition(
                context.mission_run_id,
                "failed",
                terminal_classification="worker_failed",
            )
        else:
            self._transition(context.mission_run_id, "succeeded")

    def _transition(
        self,
        mission_run_id: str,
        status: str,
        *,
        terminal_classification: str | None = None,
    ) -> None:
        with self._lock:
            state = self._load_state()
            run = state["runs"].get(mission_run_id)
            if run is None or run["status"] not in _NONTERMINAL_STATUSES:
                return
            run["status"] = status
            now = self._clock()
            if status == "running":
                run["started_at"] = now
            else:
                run["finished_at"] = now
                run["terminal_classification"] = terminal_classification
            self._save_state(state)

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "version": 1,
                "activations": {},
                "session_verifiers": {},
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


def _digest_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
