"""Non-authoritative context contracts for role episodes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


HYPER_AGENT_ROLE = "hyper-agent"
MANEUVER_CONTROL_ROLE = "maneuver-control"


@dataclass(frozen=True, slots=True)
class RoleSkill:
    """An immutable, runtime-selected Role Skill."""

    role: str
    version: str
    path: Path


__all__ = ["HYPER_AGENT_ROLE", "MANEUVER_CONTROL_ROLE", "RoleSkill"]
