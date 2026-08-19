"""Strict, local-only projection of raw agent debug artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Mapping, cast
from urllib.parse import quote


_MAX_ARTIFACT_BYTES = 1024 * 1024
_PROFILE_FIELDS = {"schema_version", "agent_role", "skills", "tools"}
_SKILL_FIELDS = {"name", "version", "path"}
_INVOCATION_FIELDS = {
    "schema_version",
    "sequence",
    "invocation_id",
    "parent_id",
    "agent_role",
    "kind",
    "name",
    "input",
    "output",
    "error",
    "started_at",
    "finished_at",
}


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _safe_directory(base: Path, *components: str) -> Path | None:
    current = base
    try:
        for component in components:
            current = current / component
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                return None
    except OSError:
        return None
    return current


def _safe_json_files(directory: Path | None) -> tuple[Path, ...]:
    if directory is None:
        return ()
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return ()
    files: list[Path] = []
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode):
            files.append(path)
    return tuple(sorted(files, key=lambda path: path.name))


def _read_mapping(path: Path) -> Mapping[str, object] | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ARTIFACT_BYTES:
            return None
        data = os.read(descriptor, _MAX_ARTIFACT_BYTES + 1)
        if len(data) > _MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return cast(Mapping[str, object], value) if isinstance(value, dict) else None


def _text(value: object, *, nonempty: bool = False) -> bool:
    return isinstance(value, str) and (not nonempty or bool(value.strip()))


def _profile(raw: Mapping[str, object]) -> dict[str, object] | None:
    if set(raw) != _PROFILE_FIELDS or type(raw.get("schema_version")) is not int:
        return None
    if raw["schema_version"] != 1 or not _text(raw["agent_role"], nonempty=True):
        return None
    skills = raw["skills"]
    tools = raw["tools"]
    if not isinstance(skills, list) or not isinstance(tools, list):
        return None
    for skill in skills:
        if not isinstance(skill, dict) or set(skill) != _SKILL_FIELDS:
            return None
        if not all(_text(skill[field]) for field in _SKILL_FIELDS):
            return None
    if not all(_text(tool) for tool in tools):
        return None
    return dict(raw)


def _invocation(raw: Mapping[str, object]) -> dict[str, object] | None:
    if set(raw) != _INVOCATION_FIELDS or type(raw.get("schema_version")) is not int:
        return None
    sequence = raw["sequence"]
    parent_id = raw["parent_id"]
    if raw["schema_version"] != 1 or type(sequence) is not int or sequence < 1:
        return None
    if parent_id is not None and not _text(parent_id):
        return None
    if not all(
        _text(raw[field], nonempty=True)
        for field in ("invocation_id", "agent_role", "name", "started_at", "finished_at")
    ):
        return None
    if raw["kind"] not in {"llm", "tool"}:
        return None
    return dict(raw)


def load_debug_artifacts(
    storage_root: Path, mission_id: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Load valid v1 artifacts for one encoded mission directory."""

    base = Path(storage_root).parent
    mission_name = quote(mission_id, safe="._-")
    mission_root = _safe_directory(base, "debug", "agent", mission_name)
    if mission_root is None:
        return [], []

    profiles: list[dict[str, object]] = []
    profiles_root = _safe_directory(mission_root, "profiles")
    for path in _safe_json_files(profiles_root):
        raw = _read_mapping(path)
        profile = _profile(raw) if raw is not None else None
        if profile is not None:
            profiles.append(profile)

    invocations: list[dict[str, object]] = []
    for path in _safe_json_files(mission_root):
        raw = _read_mapping(path)
        invocation = _invocation(raw) if raw is not None else None
        if invocation is not None:
            invocations.append(invocation)

    profiles.sort(
        key=lambda item: (
            cast(str, item["agent_role"]),
            json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    )
    invocations.sort(
        key=lambda item: (cast(int, item["sequence"]), cast(str, item["invocation_id"]))
    )
    return profiles, invocations


__all__ = ["load_debug_artifacts"]
