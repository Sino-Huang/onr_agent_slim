"""Read-only runtime selection seam for versioned role skills."""

from __future__ import annotations

from typing import Protocol

from onr.contracts.role_context import RoleSkill


class RoleSkillCatalog(Protocol):
    """Select an immutable skill source for a role episode."""

    def select(self, role: str, version: str | None = None) -> RoleSkill: ...


__all__ = ["RoleSkillCatalog"]
