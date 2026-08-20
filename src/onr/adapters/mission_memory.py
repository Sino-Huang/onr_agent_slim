"""Filesystem adapter for Mission/role-scoped Mission Memory."""

from __future__ import annotations

import os
from pathlib import Path


def _component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    candidate = value.strip()
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise ValueError(f"{label} must be one path component")
    return candidate


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


class FileMissionMemoryStore:
    """Persist one ``AGENTS.md`` file per Mission and role.

    The adapter deliberately has no unscoped read or write operation.  The
    application can therefore use it for context without making it a source
    of Mission Snapshot or planning authority.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def agent_root(self, mission_id: str, role: str) -> Path:
        mission = _component(mission_id, "mission ID")
        selected_role = _component(role, "role")
        return self.root / mission / selected_role

    def memory_path(self, mission_id: str, role: str) -> Path:
        return self.agent_root(mission_id, role) / "memory" / "AGENTS.md"

    def read(self, mission_id: str, role: str) -> str | None:
        path = self.memory_path(mission_id, role)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Mission Memory cannot be read: {path}") from exc

    def write(self, mission_id: str, role: str, contents: str) -> None:
        if not isinstance(contents, str):
            raise TypeError("Mission Memory contents must be text")
        _atomic_write(self.memory_path(mission_id, role), contents)


__all__ = ["FileMissionMemoryStore"]
