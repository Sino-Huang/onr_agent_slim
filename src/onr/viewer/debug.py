"""Strict, local-only projection of role-scoped runtime debug artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Mapping, cast
from urllib.parse import quote


_MAX_ARTIFACT_BYTES = 1024 * 1024
KNOWN_DEBUG_ROLES = frozenset(
    {"hyper-agent", "maneuver-control", "mission-summary", "runtime"}
)
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
_LLM_FIELDS = {
    "schema_version",
    "request",
    "response_id",
    "model",
    "status_code",
    "finish_reason",
    "content",
    "function_call",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "tool_calls",
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


def _safe_directories(directory: Path | None) -> tuple[Path, ...]:
    if directory is None:
        return ()
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return ()
    directories: list[Path] = []
    for path in entries:
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISDIR(mode):
            directories.append(path)
    return tuple(sorted(directories, key=lambda path: path.name))


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


def _valid_role(value: object) -> bool:
    return isinstance(value, str) and value in KNOWN_DEBUG_ROLES


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


def _llm_conversation(
    raw: Mapping[str, object], *, role: str, sequence: int
) -> dict[str, object] | None:
    if set(raw) != _LLM_FIELDS or raw.get("schema_version") != 1:
        return None
    if type(raw.get("schema_version")) is not int:
        return None
    request = raw["request"]
    if request is not None and not isinstance(request, Mapping):
        return None
    if not all(
        raw[field] is None or _text(raw[field])
        for field in ("response_id", "model", "finish_reason", "content")
    ):
        return None
    status_code = raw["status_code"]
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
    ):
        return None
    function_call = raw["function_call"]
    tool_calls = raw["tool_calls"]
    if function_call is not None and not isinstance(function_call, Mapping):
        return None
    if tool_calls is not None and not isinstance(tool_calls, list):
        return None
    request_value = dict(request) if isinstance(request, Mapping) else None
    return {
        "role": role,
        "sequence": sequence,
        "response_id": raw["response_id"],
        "model": raw["model"],
        "request": request_value,
        "input": request_value.get("messages") if request_value is not None else None,
        "reasoning": raw["reasoning"],
        "reasoning_content": raw["reasoning_content"],
        "reasoning_details": raw["reasoning_details"],
        "output": {
            "content": raw["content"],
            "function_call": dict(function_call)
            if isinstance(function_call, Mapping)
            else None,
            "tool_calls": tool_calls,
        },
        "content": raw["content"],
        "function_call": dict(function_call)
        if isinstance(function_call, Mapping)
        else None,
        "tool_calls": tool_calls,
        "finish_reason": raw["finish_reason"],
        "status_code": status_code,
    }


def _agent_records(
    mission_root: Path | None, role: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    profiles: list[dict[str, object]] = []
    profiles_root = _safe_directory(mission_root, "profiles") if mission_root else None
    for path in _safe_json_files(profiles_root):
        raw = _read_mapping(path)
        profile = _profile(raw) if raw is not None else None
        if profile is not None:
            profiles.append({"role": role, **profile})

    invocations: list[dict[str, object]] = []
    for path in _safe_json_files(mission_root):
        raw = _read_mapping(path)
        invocation = _invocation(raw) if raw is not None else None
        if invocation is not None:
            invocations.append({"role": role, **invocation})
    return profiles, invocations


def load_debug_artifacts(
    storage_root: Path, mission_id: str, *, role: str | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Load exact-valid agent artifacts from canonical and legacy layouts."""

    base = Path(storage_root).parent
    mission_name = quote(mission_id, safe="._-")
    if role is not None and not _valid_role(role):
        return [], []

    agent_root = _safe_directory(base, "debug", "agent")
    profile_index: dict[tuple[str, str], dict[str, object]] = {}
    invocation_index: dict[tuple[str, str], dict[str, object]] = {}
    for role_root in _safe_directories(agent_root):
        scope = role_root.name
        if not _valid_role(scope) or (role is not None and scope != role):
            continue
        canonical_root = _safe_directory(
            base, "debug", "agent", scope, mission_name
        )
        canonical_profiles, canonical_invocations = _agent_records(
            canonical_root, scope
        )
        for profile in canonical_profiles:
            profile_index.setdefault(
                (scope, cast(str, profile["agent_role"])), profile
            )
        for invocation in canonical_invocations:
            invocation_index.setdefault(
                (scope, cast(str, invocation["invocation_id"])), invocation
            )

    legacy_root = _safe_directory(agent_root, mission_name) if agent_root else None
    legacy_profiles_root = _safe_directory(legacy_root, "profiles") if legacy_root else None
    for path in _safe_json_files(legacy_profiles_root):
        raw = _read_mapping(path)
        profile = _profile(raw) if raw is not None else None
        scope = profile.get("agent_role") if profile is not None else None
        if (
            profile is not None
            and _valid_role(scope)
            and (role is None or scope == role)
        ):
            profile_index.setdefault(
                (cast(str, scope), cast(str, profile["agent_role"])),
                {"role": scope, **profile},
            )
    for path in _safe_json_files(legacy_root):
        raw = _read_mapping(path)
        invocation = _invocation(raw) if raw is not None else None
        scope = invocation.get("agent_role") if invocation is not None else None
        if (
            invocation is not None
            and _valid_role(scope)
            and (role is None or scope == role)
        ):
            invocation_index.setdefault(
                (cast(str, scope), cast(str, invocation["invocation_id"])),
                {"role": scope, **invocation},
            )

    profiles = sorted(
        profile_index.values(),
        key=lambda item: (
            cast(str, item["role"]),
            cast(str, item["agent_role"]),
            json.dumps(item, sort_keys=True, separators=(",", ":")),
        ),
    )
    invocations = sorted(
        invocation_index.values(),
        key=lambda item: (
            cast(str, item["role"]),
            cast(int, item["sequence"]),
            cast(str, item["invocation_id"]),
        ),
    )
    return profiles, invocations


def load_llm_conversations(
    storage_root: Path, mission_id: str, *, role: str | None = None
) -> list[dict[str, object]]:
    """Load exact-valid raw LLM records from the role-first debug layout."""

    if role is not None and not _valid_role(role):
        return []
    base = Path(storage_root).parent
    llm_root = _safe_directory(base, "debug", "llm")
    mission_name = quote(mission_id, safe="._-")
    conversations: list[dict[str, object]] = []
    for role_root in _safe_directories(llm_root):
        scope = role_root.name
        if not _valid_role(scope) or (role is not None and scope != role):
            continue
        mission_root = _safe_directory(base, "debug", "llm", scope, mission_name)
        for path in _safe_json_files(mission_root):
            if not path.stem.isdigit() or int(path.stem) < 1:
                continue
            raw = _read_mapping(path)
            conversation = (
                _llm_conversation(raw, role=scope, sequence=int(path.stem))
                if raw is not None
                else None
            )
            if conversation is not None:
                conversations.append(conversation)
    return sorted(
        conversations,
        key=lambda item: (cast(str, item["role"]), cast(int, item["sequence"])),
    )


__all__ = [
    "KNOWN_DEBUG_ROLES",
    "load_debug_artifacts",
    "load_llm_conversations",
]
