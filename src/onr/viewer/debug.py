"""Strict, local-only projection of role-scoped runtime debug artifacts."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

_MAX_ARTIFACT_BYTES = 1024 * 1024
KNOWN_DEBUG_ROLES = frozenset(
    {"hyper-agent", "maneuver-control", "mission-summary", "runtime"}
)
_PROFILE_FIELDS = {"schema_version", "agent_role", "skills", "tools"}
_SKILL_FIELDS = {"name", "version", "path"}
_INVOCATION_V1_FIELDS = {
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
_INVOCATION_V2_FIELDS = _INVOCATION_V1_FIELDS | {
    "completion_state",
    "updated_at",
    "revision",
}
_LLM_V1_FIELDS = {
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
_LLM_V2_FIELDS = _LLM_V1_FIELDS | {
    "sequence",
    "invocation_id",
    "error",
    "started_at",
    "updated_at",
    "finished_at",
    "completion_state",
    "revision",
}
_COMPLETION_STATES = {"live", "complete", "partial", "error"}


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


def _safe_json_files(
    directory: Path | None,
) -> tuple[tuple[Path, os.stat_result], ...]:
    if directory is None:
        return ()
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return ()
    files: list[tuple[Path, os.stat_result]] = []
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            files.append((path, metadata))
    return tuple(sorted(files, key=lambda item: item[0].name))


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
    version = raw.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        return None
    fields = _INVOCATION_V1_FIELDS if version == 1 else _INVOCATION_V2_FIELDS
    if set(raw) != fields:
        return None
    sequence = raw["sequence"]
    parent_id = raw["parent_id"]
    if type(sequence) is not int or sequence < 1:
        return None
    if parent_id is not None and not _text(parent_id):
        return None
    required_text = ("invocation_id", "agent_role", "name", "started_at")
    if not all(_text(raw[field], nonempty=True) for field in required_text):
        return None
    if raw["kind"] not in {"llm", "tool"}:
        return None
    if version == 1:
        if not _text(raw["finished_at"], nonempty=True):
            return None
        return dict(raw)
    completion_state = raw["completion_state"]
    revision = raw["revision"]
    finished_at = raw["finished_at"]
    if completion_state not in _COMPLETION_STATES:
        return None
    if type(revision) is not int or revision < 1:
        return None
    if not _text(raw["updated_at"], nonempty=True):
        return None
    if completion_state == "live":
        if finished_at is not None:
            return None
    elif not _text(finished_at, nonempty=True):
        return None
    return dict(raw)


def _llm_conversation(
    raw: Mapping[str, object], *, role: str, sequence: int
) -> dict[str, object] | None:
    version = raw.get("schema_version")
    if type(version) is not int:
        return None
    fields = _LLM_V1_FIELDS if version == 1 else _LLM_V2_FIELDS
    if version not in {1, 2} or set(raw) != fields:
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
    if version == 1 and status_code is None:
        return None
    if status_code is not None and (
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
    if isinstance(tool_calls, list) and not all(
        isinstance(item, Mapping) for item in tool_calls
    ):
        return None
    if version == 2:
        if raw["sequence"] != sequence:
            return None
        invocation_id = raw["invocation_id"]
        completion_state = raw["completion_state"]
        revision = raw["revision"]
        finished_at = raw["finished_at"]
        if invocation_id is not None and not _text(invocation_id, nonempty=True):
            return None
        if completion_state not in _COMPLETION_STATES:
            return None
        if type(revision) is not int or revision < 1:
            return None
        if not all(
            _text(raw[field], nonempty=True) for field in ("started_at", "updated_at")
        ):
            return None
        if completion_state == "live":
            if finished_at is not None:
                return None
        elif not _text(finished_at, nonempty=True):
            return None
        if raw["error"] is not None and not isinstance(raw["error"], Mapping):
            return None
    request_value = dict(request) if isinstance(request, Mapping) else None
    conversation = {
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
    if version == 2:
        conversation.update(
            {
                "invocation_id": raw["invocation_id"],
                "error": raw["error"],
                "started_at": raw["started_at"],
                "updated_at": raw["updated_at"],
                "finished_at": raw["finished_at"],
                "completion_state": raw["completion_state"],
                "revision": raw["revision"],
            }
        )
    return conversation


_FileKind = Literal["profile", "invocation", "llm"]


@dataclass(frozen=True, slots=True)
class _DebugFile:
    """One discovered regular JSON file and the metadata identifying its content.

    ``scope`` is the role directory the file was found under, or ``None`` for the
    legacy layout whose role comes from the record's own ``agent_role``.
    """

    kind: _FileKind
    scope: str | None
    path: Path
    identity: tuple[int, int, int, int, int]


def _debug_files(
    kind: _FileKind, scope: str | None, directory: Path | None
) -> list[_DebugFile]:
    return [
        _DebugFile(
            kind,
            scope,
            path,
            (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ),
        )
        for path, metadata in _safe_json_files(directory)
    ]


def _agent_files(base: Path, mission_name: str, role: str | None) -> list[_DebugFile]:
    """Discover agent files, canonical role-first layout before legacy."""

    agent_root = _safe_directory(base, "debug", "agent")
    files: list[_DebugFile] = []
    for role_root in _safe_directories(agent_root):
        scope = role_root.name
        if not _valid_role(scope) or (role is not None and scope != role):
            continue
        canonical_root = _safe_directory(base, "debug", "agent", scope, mission_name)
        profiles_root = (
            _safe_directory(canonical_root, "profiles") if canonical_root else None
        )
        files.extend(_debug_files("profile", scope, profiles_root))
        files.extend(_debug_files("invocation", scope, canonical_root))

    legacy_root = _safe_directory(agent_root, mission_name) if agent_root else None
    legacy_profiles_root = (
        _safe_directory(legacy_root, "profiles") if legacy_root else None
    )
    files.extend(_debug_files("profile", None, legacy_profiles_root))
    files.extend(_debug_files("invocation", None, legacy_root))
    return files


def _llm_files(base: Path, mission_name: str, role: str | None) -> list[_DebugFile]:
    llm_root = _safe_directory(base, "debug", "llm")
    files: list[_DebugFile] = []
    for role_root in _safe_directories(llm_root):
        scope = role_root.name
        if not _valid_role(scope) or (role is not None and scope != role):
            continue
        mission_root = _safe_directory(base, "debug", "llm", scope, mission_name)
        files.extend(
            file
            for file in _debug_files("llm", scope, mission_root)
            if file.path.stem.isdigit() and int(file.path.stem) >= 1
        )
    return files


_Reader = Callable[[_DebugFile], Mapping[str, object] | None]


def _read_file(file: _DebugFile) -> Mapping[str, object] | None:
    return _read_mapping(file.path)


def _agent_artifacts(
    files: Iterable[_DebugFile], read: _Reader, role: str | None
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Validate agent files; the first valid record per identity wins."""

    profile_index: dict[tuple[str, str], dict[str, object]] = {}
    invocation_index: dict[tuple[str, str], dict[str, object]] = {}
    for file in files:
        raw = read(file)
        if raw is None:
            continue
        if file.kind == "profile":
            record = _profile(raw)
            index, identity_field = profile_index, "agent_role"
        else:
            record = _invocation(raw)
            index, identity_field = invocation_index, "invocation_id"
        if record is None:
            continue
        scope = file.scope if file.scope is not None else record.get("agent_role")
        if file.scope is None and (
            not _valid_role(scope) or (role is not None and scope != role)
        ):
            continue
        index.setdefault(
            (cast(str, scope), cast(str, record[identity_field])),
            {"role": scope, **record},
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


def _llm_artifacts(
    files: Iterable[_DebugFile], read: _Reader
) -> list[dict[str, object]]:
    conversations: list[dict[str, object]] = []
    for file in files:
        raw = read(file)
        conversation = (
            _llm_conversation(
                raw, role=cast(str, file.scope), sequence=int(file.path.stem)
            )
            if raw is not None
            else None
        )
        if conversation is not None:
            conversations.append(conversation)
    return sorted(
        conversations,
        key=lambda item: (cast(str, item["role"]), cast(int, item["sequence"])),
    )


def load_debug_artifacts(
    storage_root: Path, mission_id: str, *, role: str | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Load exact-valid agent artifacts from canonical and legacy layouts."""

    if role is not None and not _valid_role(role):
        return [], []
    files = _agent_files(
        Path(storage_root).parent, quote(mission_id, safe="._-"), role
    )
    return _agent_artifacts(files, _read_file, role)


def load_llm_conversations(
    storage_root: Path, mission_id: str, *, role: str | None = None
) -> list[dict[str, object]]:
    """Load exact-valid raw LLM records from the role-first debug layout."""

    if role is not None and not _valid_role(role):
        return []
    files = _llm_files(Path(storage_root).parent, quote(mission_id, safe="._-"), role)
    return _llm_artifacts(files, _read_file)


@dataclass(frozen=True, slots=True)
class DebugArtifactSnapshot:
    """All-role debug records of one Mission, as the loaders would return them.

    ``version`` changes only when a discovered file appears, disappears, or
    changes device, inode, size, mtime, or ctime; unchanged snapshots are one
    object.
    """

    version: int
    profiles: tuple[dict[str, object], ...]
    invocations: tuple[dict[str, object], ...]
    conversations: tuple[dict[str, object], ...]


class DebugArtifactCatalog:
    """Incrementally track one Mission's agent and raw LLM debug records.

    Each snapshot rediscovers the layout with the loaders' symlink-refusing
    rules and reparses only files whose metadata identity changed. The parse
    cache holds only the currently discovered files.
    """

    def __init__(self, storage_root: Path, mission_id: str) -> None:
        self.storage_root = storage_root
        self.mission_id = mission_id
        self._base = Path(storage_root).parent
        self._mission_name = quote(mission_id, safe="._-")
        self._files: tuple[_DebugFile, ...] = ()
        self._parsed: dict[Path, tuple[_DebugFile, Mapping[str, object] | None]] = {}
        self._snapshot = DebugArtifactSnapshot(0, (), (), ())

    def snapshot(self) -> DebugArtifactSnapshot:
        agent_files = _agent_files(self._base, self._mission_name, None)
        llm_files = _llm_files(self._base, self._mission_name, None)
        files = (*agent_files, *llm_files)
        if files == self._files:
            return self._snapshot

        previous = self._parsed
        parsed: dict[Path, tuple[_DebugFile, Mapping[str, object] | None]] = {}

        def read(file: _DebugFile) -> Mapping[str, object] | None:
            cached = previous.get(file.path)
            if cached is not None and cached[0] == file:
                value = cached[1]
            else:
                value = _read_file(file)
            parsed[file.path] = (file, value)
            return value

        profiles, invocations = _agent_artifacts(agent_files, read, None)
        conversations = _llm_artifacts(llm_files, read)
        self._files = files
        self._parsed = parsed
        self._snapshot = DebugArtifactSnapshot(
            self._snapshot.version + 1,
            tuple(profiles),
            tuple(invocations),
            tuple(conversations),
        )
        return self._snapshot


__all__ = [
    "KNOWN_DEBUG_ROLES",
    "DebugArtifactCatalog",
    "DebugArtifactSnapshot",
    "load_debug_artifacts",
    "load_llm_conversations",
]
