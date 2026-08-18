"""Persistence seam for non-authoritative Mission Memory."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class MissionMemoryStore(Protocol):
    """Read and write memory only within one Mission/role scope."""

    def read(self, mission_id: str, role: str) -> str | None: ...

    def write(self, mission_id: str, role: str, contents: str) -> None: ...

    def agent_root(self, mission_id: str, role: str) -> Path: ...


__all__ = ["MissionMemoryStore"]
