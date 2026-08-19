"""Filesystem adapter for immutable, versioned Role Skills."""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from onr.contracts.role_context import RoleSkill


_VERSION = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?$")
_ROLE_FOLDERS = {"hyper-agent": "hyper", "maneuver-control": "maneuver-control"}
_ROLE_SKILL_ORDER = {
    "hyper-agent": (
        "mission-parsing",
        "planner-selection",
        "creating-minizinc-problem-files",
        "detect-and-replan",
    ),
    "maneuver-control": (
        "decision-cycle",
        "physical-maneuver-selection",
        "hyper-coordination",
    ),
}


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

    def select_all(
        self, role: str, version: str | None = None
    ) -> tuple[RoleSkill, ...]:
        """Select every flat skill for a role, with legacy layout fallback."""

        _validate_component(role, "role")
        if version is not None:
            _validate_component(version, "Role Skill version")
        folder = _ROLE_FOLDERS.get(role, role)
        role_root = self.root / folder
        candidates = (
            [
                path
                for path in role_root.iterdir()
                if path.is_dir() and (path / "SKILL.md").is_file()
            ]
            if role_root.is_dir()
            else []
        )
        flat: list[RoleSkill] = []
        has_flat_layout = False
        for path in candidates:
            metadata = _skill_metadata(path / "SKILL.md")
            declared_name = metadata.get("name")
            declared_version = metadata.get("version")
            if declared_version == path.name:
                continue
            has_flat_layout = True
            if not isinstance(declared_name, str) or declared_name != path.name:
                raise ValueError(f"Role Skill name metadata does not match its directory: {path}")
            if not isinstance(declared_version, str) or not _VERSION.fullmatch(
                declared_version
            ):
                raise ValueError(f"Role Skill version metadata is invalid: {path}")
            if version is None or declared_version == version:
                flat.append(RoleSkill(declared_name, declared_version, path))
        if has_flat_layout:
            if not flat:
                requested = version or "latest"
                raise ValueError(f"Role Skill version is unavailable: {role}/{requested}")
            order = {
                name: index
                for index, name in enumerate(_ROLE_SKILL_ORDER.get(role, ()))
            }
            flat.sort(key=lambda skill: (order.get(skill.role, len(order)), skill.role))
            return tuple(flat)
        return (self.select(role, version),)


def _validate_component(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"{label} must be one path component")


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
