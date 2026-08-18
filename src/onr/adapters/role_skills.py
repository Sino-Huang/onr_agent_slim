"""Filesystem adapter for immutable, versioned Role Skills."""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from onr.contracts.role_context import RoleSkill


_VERSION = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?$")


class FilesystemRoleSkillCatalog:
    """Select skills from ``root/<role>/<version>/SKILL.md`` directories.

    This adapter exposes no write method.  The selected directory is passed to
    DeepAgents as a source and is protected there by filesystem permissions.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def select(self, role: str, version: str | None = None) -> RoleSkill:
        if not isinstance(role, str) or not role.strip() or role in {".", ".."} or "/" in role or "\\" in role:
            raise ValueError("role must be one path component")
        if version is not None and (
            not isinstance(version, str)
            or not version.strip()
            or version in {".", ".."}
            or "/" in version
            or "\\" in version
        ):
            raise ValueError("Role Skill version must be one path component")
        role_root = self.root / role
        if not role_root.is_dir():
            raise ValueError(f"no Role Skills are installed for role: {role}")
        candidates = [path for path in role_root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()]
        if version is not None:
            candidates = [path for path in candidates if path.name == version]
        if not candidates:
            requested = version or "latest"
            raise ValueError(f"Role Skill version is unavailable: {role}/{requested}")
        selected = max(candidates, key=lambda path: _version_key(path.name))
        metadata = _skill_metadata(selected / "SKILL.md")
        declared_version = metadata.get("version")
        if not isinstance(declared_version, str) or declared_version != selected.name:
            raise ValueError(f"Role Skill version metadata does not match its directory: {selected}")
        declared_name = metadata.get("name", role)
        if not isinstance(declared_name, str) or not declared_name.strip():
            raise ValueError(f"Role Skill name is invalid: {selected}")
        return RoleSkill(declared_name, declared_version, selected)


def _version_key(value: str) -> tuple[object, ...]:
    if not _VERSION.match(value):
        return (0, value)
    parts = value.removeprefix("v").split(".")
    return (1, *(int(part.split("-", 1)[0].split("+", 1)[0]) for part in parts))


def _skill_metadata(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"Role Skill is missing SKILL.md front matter: {path}")
    _, front_matter, _ = text.split("---", 2)
    metadata = yaml.safe_load(front_matter)
    if not isinstance(metadata, dict):
        raise ValueError(f"Role Skill metadata is invalid: {path}")
    return metadata


__all__ = ["FilesystemRoleSkillCatalog"]
