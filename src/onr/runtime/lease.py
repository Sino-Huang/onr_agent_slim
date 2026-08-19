"""Crash-tolerant, owner-aware runtime session lease."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
from typing import cast
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class RuntimeLease:
    session_id: str
    pid: int
    started_at: str
    last_seen: str
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("runtime lease session ID must be non-empty")
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("runtime lease PID must be positive")
        if self.status not in {"active", "stopped", "stale"}:
            raise ValueError("runtime lease status is invalid")
        for value in (self.started_at, self.last_seen):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("runtime lease timestamps must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id, "pid": self.pid,
            "started_at": self.started_at, "last_seen": self.last_seen,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "RuntimeLease":
        if set(value) != {"session_id", "pid", "started_at", "last_seen", "status"}:
            raise ValueError("runtime lease contains unknown or missing fields")
        pid = value["pid"]
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise ValueError("runtime lease PID must be an integer")
        for key in ("session_id", "started_at", "last_seen", "status"):
            if not isinstance(value[key], str):
                raise ValueError(f"runtime lease {key} must be a string")
        for key in ("started_at", "last_seen"):
            datetime.fromisoformat(cast(str, value[key]))
        return cls(
            str(value["session_id"]), cast(int, pid), str(value["started_at"]),
            str(value["last_seen"]), str(value["status"]),
        )


class RuntimeLeaseStore:
    """Atomically publish one lease without taking ownership from another session."""

    def __init__(
        self,
        root: Path | str = Path("var/runtime"),
        *,
        timeout: float = 30.0,
        heartbeat_interval: float | None = None,
    ) -> None:
        if timeout <= 0 or not math.isfinite(float(timeout)):
            raise ValueError("runtime lease timeout must be finite and positive")
        interval = min(float(timeout) / 3.0, 5.0) if heartbeat_interval is None else float(heartbeat_interval)
        if interval <= 0 or not math.isfinite(interval) or interval >= float(timeout):
            raise ValueError("runtime lease heartbeat interval must be finite, positive, and below timeout")
        self.root = Path(root)
        self.path = self.root / "lease.json"
        self.lock_path = self.root / "lease.lock"
        self.timeout = float(timeout)
        self.heartbeat_interval = interval
        self._owner_session_id: str | None = None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _with_staleness(self, lease: RuntimeLease | None) -> RuntimeLease | None:
        if lease is None or lease.status != "active":
            return lease
        try:
            age = time.time() - datetime.fromisoformat(lease.last_seen).timestamp()
        except (TypeError, ValueError):
            age = math.inf
        if age > self.timeout:
            return RuntimeLease(lease.session_id, lease.pid, lease.started_at, lease.last_seen, "stale")
        return lease

    def _write(self, lease: RuntimeLease) -> RuntimeLease:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".lease.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(lease.to_dict(), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            try:
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return lease

    def _read_serialized(self) -> RuntimeLease | None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                value = json.load(handle)
            if not isinstance(value, dict):
                return None
            return RuntimeLease.from_dict(value)
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def start(self, *, session_id: str | None = None) -> RuntimeLease:
        selected_id = session_id or uuid4().hex
        if not isinstance(selected_id, str) or not selected_id.strip():
            raise ValueError("runtime lease session ID must be non-empty")
        with self._locked():
            current = self._with_staleness(self._read_serialized())
            if current is not None and current.status == "active" and current.session_id != selected_id:
                raise RuntimeError("runtime lease is owned by another active session")
            now = _now()
            lease = RuntimeLease(selected_id, os.getpid(), now, now, "active")
            self._owner_session_id = selected_id
            return self._write(lease)

    def touch(self) -> RuntimeLease | None:
        owner = self._owner_session_id
        if owner is None:
            return None
        with self._locked():
            lease = self._with_staleness(self._read_serialized())
            if lease is None or lease.status != "active" or lease.session_id != owner:
                return None
            return self._write(RuntimeLease(lease.session_id, lease.pid, lease.started_at, _now(), "active"))

    def stop(self) -> RuntimeLease | None:
        owner = self._owner_session_id
        if owner is None:
            return None
        with self._locked():
            lease = self._with_staleness(self._read_serialized())
            if lease is None or lease.session_id != owner or lease.status != "active":
                self._owner_session_id = None
                return None
            stopped = RuntimeLease(lease.session_id, lease.pid, lease.started_at, _now(), "stopped")
            self._owner_session_id = None
            return self._write(stopped)

    def read(self) -> RuntimeLease | None:
        with self._locked():
            return self._with_staleness(self._read_serialized())

    def inspect(self) -> RuntimeLease | None:
        """Inspect liveness without creating the lease directory or lock file."""

        return self._with_staleness(self._read_serialized())

    def is_active(self) -> bool:
        lease = self.read()
        return lease is not None and lease.status == "active"

    @contextmanager
    def session(self) -> Iterator[RuntimeLease]:
        lease = self.start()
        stopped = Event()

        def heartbeat() -> None:
            while not stopped.wait(self.heartbeat_interval):
                if self.touch() is None:
                    return

        thread = Thread(target=heartbeat, name=f"runtime-lease-{lease.session_id[:8]}", daemon=True)
        thread.start()
        try:
            yield lease
        finally:
            stopped.set()
            thread.join(timeout=max(self.heartbeat_interval * 2, 0.1))
            self.stop()


__all__ = ["RuntimeLease", "RuntimeLeaseStore"]
