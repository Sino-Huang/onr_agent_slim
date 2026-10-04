"""Discover and read public Mission Run Artifacts.

Publishers write dot-prefixed temporary names and atomically rename committed
content first. They rename the envelope to ``artifact.json`` last; that
committed envelope is the publication point. Committed content is immutable.
Discovery ignores temporary names, incomplete publications, and anything that
fails validation. Every scan and read revalidates committed files and fails
closed if they changed or became unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from onr.runtime_host.observations import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    decode_cursor,
    encode_cursor,
)
from onr.runtime_host.run_files import JsonFileCache

ARTIFACT_SCHEMA_VERSION = 1
MAX_ENVELOPE_BYTES = 65536
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
DEFAULT_PREVIEW_BYTES = 4096
MAX_PREVIEW_BYTES = 16384
MAX_ENTRY_BYTES = 65536
MAX_ENTRY_CONTENT_BYTES = 16384

_ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENTRY_FILENAME_RE = re.compile(r"[0-9]+\.json\Z")
_ENVELOPE_FIELDS = {
    "schema_version",
    "artifact_id",
    "mission_id",
    "mission_run_id",
    "kind",
    "media_type",
    "display",
    "published_at",
    "content",
}
_DISPLAY_FIELDS = {"title", "summary"}
_CONTENT_FIELDS = {"path", "byte_size", "content_digest"}
_ENTRY_FIELDS = {"sequence", "author", "time", "audience", "kind"}
_CONTENT_REF_FIELDS = {"path", "media_type", "byte_size", "content_digest"}
_READ_CHUNK_BYTES = 64 * 1024


class ArtifactNotFoundError(Exception):
    """A requested public Artifact does not exist for the Mission Run."""


class ArtifactUnavailableError(Exception):
    """A committed Artifact's content failed validation at read time."""


@dataclass(frozen=True, slots=True)
class _Artifact:
    directory: Path
    artifact_id: str
    kind: str
    media_type: str
    display: dict[str, object]
    published_at: str
    content: dict[str, object] | None

    @property
    def classification(self) -> str:
        if self.kind == "conversation":
            return "conversation"
        if self.media_type.startswith("text/") or self.media_type == "application/json":
            return "text"
        return "binary"

    def descriptor(self) -> dict[str, object]:
        content = self.content
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "byte_size": None if content is None else content["byte_size"],
            "content_digest": None if content is None else content["content_digest"],
            "display": dict(self.display),
            "published_at": self.published_at,
            "classification": self.classification,
        }


class PublicArtifactInbox:
    """Discover and read atomically published public Artifacts."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def artifacts(
        self,
        mission_id: str,
        mission_run_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Return one Artifact descriptor page for a Mission Run."""

        artifacts = self._discover(mission_id, mission_run_id)
        after = (
            0
            if cursor is None
            else decode_cursor(
                cursor,
                mission_run_id=mission_run_id,
                max_sequence=len(artifacts),
            )
        )
        page_limit = DEFAULT_PAGE_SIZE if limit is None else limit
        page = artifacts[after : after + page_limit]
        position = after + len(page)
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "mission_id": mission_id,
            "mission_run_id": mission_run_id,
            "artifacts": [artifact.descriptor() for artifact in page],
            "next_cursor": (
                None if not page else encode_cursor(mission_run_id, position)
            ),
        }

    def artifact_content(
        self,
        mission_id: str,
        mission_run_id: str,
        artifact_id: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Read one validated text preview or binary Artifact metadata."""

        artifact = self._artifact(mission_id, mission_run_id, artifact_id)
        if artifact is None or artifact.classification == "conversation":
            raise ArtifactNotFoundError
        requested_offset = 0 if offset is None else offset
        requested_limit = DEFAULT_PREVIEW_BYTES if limit is None else limit
        if requested_offset < 0 or not 1 <= requested_limit <= MAX_PREVIEW_BYTES:
            raise ValueError("invalid Artifact content window")

        descriptor: int | None = None
        try:
            descriptor, metadata = _open_validated_content(
                artifact.directory,
                cast(dict[str, object], artifact.content),
                maximum=MAX_ARTIFACT_BYTES,
            )
            byte_size = metadata.st_size
            if requested_offset > byte_size:
                raise ValueError("Artifact offset exceeds content size")
            if artifact.classification == "binary":
                if requested_offset != 0:
                    raise ValueError("binary Artifact offset must be zero")
                return _content_response(
                    mission_id,
                    mission_run_id,
                    artifact,
                    byte_size=byte_size,
                    offset=0,
                    next_offset=None,
                    eof=True,
                    truncated=False,
                    content=None,
                )

            adjusted_offset = _snap_start_forward(
                descriptor, requested_offset, byte_size
            )
            requested_end = min(byte_size, requested_offset + requested_limit)
            adjusted_end = max(
                adjusted_offset,
                _snap_end_backward(descriptor, requested_end, adjusted_offset),
            )
            window = os.pread(
                descriptor, adjusted_end - adjusted_offset, adjusted_offset
            )
            if len(window) != adjusted_end - adjusted_offset:
                raise OSError("Artifact content changed during read")
            try:
                text = window.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ArtifactUnavailableError from exc
            if not _same_file_state(metadata, os.fstat(descriptor)):
                raise OSError("Artifact content changed during read")
            eof = adjusted_end == byte_size
            return _content_response(
                mission_id,
                mission_run_id,
                artifact,
                byte_size=byte_size,
                offset=adjusted_offset,
                next_offset=None if eof else adjusted_end,
                eof=eof,
                truncated=adjusted_end < requested_end,
                content=text,
            )
        except ValueError:
            raise
        except ArtifactUnavailableError:
            raise
        except (OSError, TypeError, KeyError) as exc:
            raise ArtifactUnavailableError from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def conversation_entries(
        self,
        mission_id: str,
        mission_run_id: str,
        artifact_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Return one page from a Conversation Artifact."""

        artifact = self._artifact(mission_id, mission_run_id, artifact_id)
        if artifact is None or artifact.classification != "conversation":
            raise ArtifactNotFoundError
        entries = _conversation_entries(artifact.directory)
        highest = entries[-1]["sequence"] if entries else 0
        if isinstance(highest, bool) or not isinstance(highest, int):
            highest = 0
        after = (
            0
            if cursor is None
            else decode_cursor(
                cursor,
                mission_run_id=mission_run_id,
                max_sequence=highest,
            )
        )
        page_limit = DEFAULT_PAGE_SIZE if limit is None else limit
        page = [entry for entry in entries if cast(int, entry["sequence"]) > after][
            :page_limit
        ]
        last_sequence = None if not page else cast(int, page[-1]["sequence"])
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "mission_id": mission_id,
            "mission_run_id": mission_run_id,
            "artifact_id": artifact_id,
            "entries": page,
            "next_cursor": (
                None
                if last_sequence is None
                else encode_cursor(mission_run_id, last_sequence)
            ),
        }

    def _discover(self, mission_id: str, mission_run_id: str) -> list[_Artifact]:
        run_directory = _safe_directory(self.root / mission_run_id)
        if run_directory is None:
            return []
        try:
            children = tuple(run_directory.iterdir())
        except OSError:
            return []
        artifacts: list[_Artifact] = []
        for child in sorted(children, key=lambda path: path.name):
            if child.name.startswith(".") or not _valid_artifact_id(child.name):
                continue
            artifact_directory = _safe_directory(child)
            if artifact_directory is None:
                continue
            artifact = _load_artifact(
                artifact_directory,
                mission_id=mission_id,
                mission_run_id=mission_run_id,
                artifact_id=child.name,
            )
            if artifact is None:
                continue
            if artifact.content is not None:
                descriptor: int | None = None
                try:
                    descriptor, _ = _open_validated_content(
                        artifact.directory,
                        artifact.content,
                        maximum=MAX_ARTIFACT_BYTES,
                    )
                except (OSError, TypeError, KeyError):
                    continue
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            artifacts.append(artifact)
        return artifacts

    def _artifact(
        self, mission_id: str, mission_run_id: str, artifact_id: str
    ) -> _Artifact | None:
        if not _valid_artifact_id(artifact_id):
            return None
        run_directory = _safe_directory(self.root / mission_run_id)
        if run_directory is None:
            return None
        artifact_directory = _safe_directory(run_directory / artifact_id)
        if artifact_directory is None:
            return None
        return _load_artifact(
            artifact_directory,
            mission_id=mission_id,
            mission_run_id=mission_run_id,
            artifact_id=artifact_id,
        )


def _load_artifact(
    directory: Path, *, mission_id: str, mission_run_id: str, artifact_id: str
) -> _Artifact | None:
    value = _read_json_mapping(
        directory / "artifact.json", root=directory, maximum=MAX_ENVELOPE_BYTES
    )
    if value is None or set(value) != _ENVELOPE_FIELDS:
        return None
    schema_version = value.get("schema_version")
    display = value.get("display")
    kind = value.get("kind")
    media_type = value.get("media_type")
    published_at = value.get("published_at")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != ARTIFACT_SCHEMA_VERSION
        or value.get("artifact_id") != artifact_id
        or value.get("mission_id") != mission_id
        or value.get("mission_run_id") != mission_run_id
        or not _nonempty_text(kind)
        or not _nonempty_text(media_type)
        or not _nonempty_text(published_at)
        or not isinstance(display, dict)
        or set(display) != _DISPLAY_FIELDS
        or not _nonempty_text(display.get("title"))
        or not (
            display.get("summary") is None or isinstance(display.get("summary"), str)
        )
    ):
        return None
    content = value.get("content")
    if kind == "conversation":
        if content is not None:
            return None
        normalized_content = None
    else:
        normalized_content = _content_metadata(content)
        if normalized_content is None:
            return None
    return _Artifact(
        directory=directory,
        artifact_id=artifact_id,
        kind=cast(str, kind),
        media_type=cast(str, media_type),
        display={"title": display["title"], "summary": display["summary"]},
        published_at=cast(str, published_at),
        content=normalized_content,
    )


def _content_metadata(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != _CONTENT_FIELDS:
        return None
    path = value.get("path")
    byte_size = value.get("byte_size")
    content_digest = value.get("content_digest")
    if (
        not _valid_relative_path(path)
        or isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or byte_size < 0
        or not isinstance(content_digest, str)
        or _DIGEST_RE.fullmatch(content_digest) is None
    ):
        return None
    return {
        "path": path,
        "byte_size": byte_size,
        "content_digest": content_digest,
    }


def _conversation_entries(artifact_directory: Path) -> list[dict[str, object]]:
    entries_directory = _safe_directory(artifact_directory / "entries")
    if entries_directory is None:
        return []
    try:
        candidates = tuple(entries_directory.iterdir())
    except OSError:
        return []
    valid: list[tuple[int, str, dict[str, object]]] = []
    for path in candidates:
        if path.name.startswith(".") or _ENTRY_FILENAME_RE.fullmatch(path.name) is None:
            continue
        stem = path.stem
        try:
            filename_sequence = int(stem)
        except (ValueError, OverflowError):
            continue
        value = _read_json_mapping(
            path, root=entries_directory, maximum=MAX_ENTRY_BYTES
        )
        entry = _validated_entry(value, artifact_directory)
        if entry is None or filename_sequence != entry["sequence"]:
            continue
        valid.append((cast(int, entry["sequence"]), path.name, entry))
    valid.sort(key=lambda item: (item[0], item[1]))
    result: list[dict[str, object]] = []
    seen: set[int] = set()
    for sequence, _filename, entry in valid:
        if sequence in seen:
            continue
        seen.add(sequence)
        result.append(entry)
    return result


def _validated_entry(
    value: dict[str, object] | None, artifact_directory: Path
) -> dict[str, object] | None:
    if value is None:
        return None
    has_content = "content" in value
    has_reference = "content_ref" in value
    expected = _ENTRY_FIELDS | ({"content"} if has_content else {"content_ref"})
    if has_content == has_reference or set(value) != expected:
        return None
    sequence = value.get("sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or any(
            not _nonempty_text(value.get(field))
            for field in ("author", "time", "audience", "kind")
        )
    ):
        return None
    if has_content:
        content = value.get("content")
        if not isinstance(content, str):
            return None
        try:
            if len(content.encode("utf-8")) > MAX_ENTRY_CONTENT_BYTES:
                return None
        except UnicodeEncodeError:
            return None
        content_ref = None
    else:
        content = None
        content_ref = _validated_content_ref(
            value.get("content_ref"), artifact_directory
        )
        if content_ref is None:
            return None
    return {
        "sequence": sequence,
        "author": value["author"],
        "time": value["time"],
        "audience": value["audience"],
        "kind": value["kind"],
        "content": content,
        "content_ref": content_ref,
    }


def _validated_content_ref(
    value: object, artifact_directory: Path
) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != _CONTENT_REF_FIELDS:
        return None
    media_type = value.get("media_type")
    metadata = _content_metadata(
        {
            "path": value.get("path"),
            "byte_size": value.get("byte_size"),
            "content_digest": value.get("content_digest"),
        }
    )
    if metadata is None or not _nonempty_text(media_type):
        return None
    descriptor: int | None = None
    try:
        descriptor, _ = _open_validated_content(
            artifact_directory, metadata, maximum=MAX_ENTRY_CONTENT_BYTES
        )
    except (OSError, TypeError, KeyError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {
        "path": metadata["path"],
        "media_type": media_type,
        "byte_size": metadata["byte_size"],
        "content_digest": metadata["content_digest"],
    }


def _open_validated_content(
    root: Path, metadata: dict[str, object], *, maximum: int
) -> tuple[int, os.stat_result]:
    path = cast(str, metadata["path"])
    descriptor = _open_confined(root / Path(*path.split("/")), root)
    try:
        before = os.fstat(descriptor)
        declared_size = metadata["byte_size"]
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum
            or before.st_size != declared_size
        ):
            raise OSError("Artifact content metadata mismatch")
        digest = hashlib.sha256()
        total = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > maximum:
                raise OSError("Artifact content exceeds maximum size")
        after = os.fstat(descriptor)
        if (
            total != declared_size
            or not _same_file_state(before, after)
            or f"sha256:{digest.hexdigest()}" != metadata["content_digest"]
        ):
            raise OSError("Artifact content failed validation")
        return descriptor, after
    except Exception:
        os.close(descriptor)
        raise


def _open_confined(path: Path, root: Path) -> int:
    """Open one file beneath a non-symlink root using directory descriptors."""

    absolute_root = Path(os.path.abspath(root))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise OSError("Artifact path escapes its directory") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("Artifact path is not confined")

    directory_fd = os.open(
        absolute_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)


def _read_json_mapping(
    path: Path, *, root: Path, maximum: int
) -> dict[str, object] | None:
    descriptor: int | None = None
    try:
        descriptor = _open_confined(path, root)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            return None
        data = os.read(descriptor, maximum + 1)
        if len(data) > maximum:
            return None
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        return cast(dict[str, object], value) if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_directory(path: Path) -> Path | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    return (
        path
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
        else None
    )


def _valid_artifact_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and _ARTIFACT_ID_RE.fullmatch(value) is not None
        and value != ".."
    )


def _valid_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    if "\\" in value or "\x00" in value:
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _same_file_state(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _snap_start_forward(descriptor: int, offset: int, byte_size: int) -> int:
    adjusted = offset
    while adjusted < byte_size:
        value = os.pread(descriptor, 1, adjusted)
        if len(value) != 1:
            raise OSError("Artifact content changed during read")
        if value[0] & 0xC0 != 0x80:
            break
        adjusted += 1
    return adjusted


def _snap_end_backward(descriptor: int, end: int, start: int) -> int:
    adjusted = end
    while adjusted > start:
        value = os.pread(descriptor, 1, adjusted)
        if not value or value[0] & 0xC0 != 0x80:
            break
        adjusted -= 1
    return adjusted


def _content_response(
    mission_id: str,
    mission_run_id: str,
    artifact: _Artifact,
    *,
    byte_size: int,
    offset: int,
    next_offset: int | None,
    eof: bool,
    truncated: bool,
    content: str | None,
) -> dict[str, object]:
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "mission_id": mission_id,
        "mission_run_id": mission_run_id,
        "artifact_id": artifact.artifact_id,
        "classification": artifact.classification,
        "media_type": artifact.media_type,
        "byte_size": byte_size,
        "offset": offset,
        "next_offset": next_offset,
        "eof": eof,
        "truncated": truncated,
        "content": content,
    }


def service_log_artifacts(run_root: Path) -> list[dict[str, object]]:
    """Allowlist regular run-local service logs, never arbitrary caller paths."""
    if _safe_directory(run_root) is None:
        return []
    paths = [("worker-log", run_root / "worker.log")]
    try:
        for path in (
            (run_root / "services").glob("*.log")
            if _safe_directory(run_root / "services") is not None
            else ()
        ):
            if _ARTIFACT_ID_RE.fullmatch(f"service-log-{path.stem}"):
                paths.append((f"service-log-{path.stem}", path))
    except OSError:
        pass
    result: list[dict[str, object]] = []
    for artifact_id, path in paths:
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        result.append(
            {
                "artifact_id": artifact_id,
                "classification": "service_log",
                "media_type": "text/plain",
                "byte_size": metadata.st_size,
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "kind": "service_log",
                "content_digest": None,
                "display": {
                    "title": f"{path.stem} log",
                    "summary": str(path.relative_to(run_root)),
                },
                "published_at": datetime.fromtimestamp(
                    metadata.st_mtime, UTC
                ).isoformat(),
                "source": "service_log",
                "ref": str(path.relative_to(run_root)),
            }
        )
    return sorted(result, key=lambda item: cast(str, item["artifact_id"]))


def service_log_content(
    run_root: Path,
    mission_id: str,
    mission_run_id: str,
    artifact_id: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    descriptors = {
        item["artifact_id"]: item for item in service_log_artifacts(run_root)
    }
    descriptor = descriptors.get(artifact_id)
    if descriptor is None:
        raise ArtifactNotFoundError
    start = 0 if offset is None else offset
    size = DEFAULT_PREVIEW_BYTES if limit is None else limit
    if start < 0 or not 1 <= size <= MAX_PREVIEW_BYTES:
        raise ValueError("invalid service log window")
    fd: int | None = None
    try:
        fd = _open_confined(run_root / cast(str, descriptor["ref"]), run_root)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("not a regular service log")
        if start > metadata.st_size:
            raise ValueError("Artifact offset exceeds content size")
        # Byte offsets are exact, including when a window splits UTF-8 characters.
        window = os.pread(fd, size, start)
        end = start + len(window)
        eof = end >= metadata.st_size
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "mission_id": mission_id,
            "mission_run_id": mission_run_id,
            "artifact_id": artifact_id,
            "classification": "service_log",
            "media_type": "text/plain",
            "byte_size": metadata.st_size,
            "offset": start,
            "next_offset": None if eof else end,
            "eof": eof,
            "truncated": False,
            "content": window.decode("utf-8", errors="replace"),
        }
    except ValueError:
        raise
    except OSError as exc:
        raise ArtifactUnavailableError from exc
    finally:
        if fd is not None:
            os.close(fd)


# The live demo acceptance audit (``scripts/audit_live_demo.py``) writes its
# verdict here; the Host only reads it and never runs the audit itself.
AUDIT_ARTIFACT_NAME = "live-acceptance.json"
# Where the owner's ``x`` writes the terminal receipt (overwritten each time).
RECEIPT_EXPORT_NAME = "mission-run-receipt.json"
_AUDIT_MAX_FAILURES = 20


def _decode_audit(document: Mapping[str, object]) -> dict[str, object]:
    status = document.get("status")
    failures = document.get("failures", [])
    mode = document.get("mission_mode")
    if (
        status not in {"PASS", "FAIL"}
        or not isinstance(failures, list)
        or not all(isinstance(item, str) for item in failures)
        or (mode is not None and not isinstance(mode, str))
    ):
        raise ValueError("not a live demo audit")
    return {
        "status": cast(str, status).lower(),
        "mission_mode": mode,
        "failures": list(failures[:_AUDIT_MAX_FAILURES]),
    }


_AUDITS: JsonFileCache[dict[str, object]] = JsonFileCache(
    _decode_audit, max_bytes=MAX_ARTIFACT_BYTES, max_entries=16
)


def live_demo_audit(run_root: Path) -> dict[str, object]:
    """The run's live demo audit verdict, only as its audit artifact records it.

    ``not_recorded`` when no audit artifact exists and ``unreadable`` when one
    exists but is not a PASS/FAIL audit; never derived from lifecycle status.
    """

    path = run_root / AUDIT_ARTIFACT_NAME
    try:
        metadata = path.lstat()
    except OSError:
        return {
            "status": "not_recorded",
            "path": None,
            "recorded_at": None,
            "mission_mode": None,
            "failures": [],
        }
    decoded = _AUDITS.get(path) if stat.S_ISREG(metadata.st_mode) else None
    recorded = {
        "path": AUDIT_ARTIFACT_NAME,
        "recorded_at": datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat(),
    }
    if decoded is None:
        return {
            "status": "unreadable",
            **recorded,
            "mission_mode": None,
            "failures": [],
        }
    return {
        "status": decoded["status"],
        **recorded,
        "mission_mode": decoded["mission_mode"],
        "failures": decoded["failures"],
    }


def _decode_export_time(document: Mapping[str, object]) -> str:
    value = document["exported_at"]
    if not isinstance(value, str):
        raise TypeError("exported_at must be text")
    return value


_EXPORTS: JsonFileCache[str] = JsonFileCache(
    _decode_export_time, max_bytes=MAX_ARTIFACT_BYTES, max_entries=16
)


def receipt_export(run_root: Path) -> dict[str, object]:
    """Where ``x`` writes the receipt, and when it last did (``null``: never)."""

    path = run_root / RECEIPT_EXPORT_NAME
    return {"path": str(path), "exported_at": _EXPORTS.get(path)}


# Run Root files a receipt export references, by role, when they exist.
_RECEIPT_REFERENCES = (
    ("closed_loop_result", "closed-loop-result.json"),
    ("live_demo_audit", AUDIT_ARTIFACT_NAME),
    ("stack_plan", "stack.json"),
    ("stack_status", "stack-status.json"),
    ("observations", "observations.json"),
    ("narrative", "narrative.json"),
    ("world_frame", "world-frames/latest.png"),
)


def receipt_artifact_references(run_root: Path) -> list[dict[str, object]]:
    """Regular Run Root files behind a receipt: role, relative path, size.

    Logs carry the ``artifact_id`` the console's Artifacts tab serves them as.
    """

    references: list[dict[str, object]] = []
    for role, relative in _RECEIPT_REFERENCES:
        try:
            metadata = (run_root / relative).lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            references.append(
                {
                    "role": role,
                    "path": relative,
                    "byte_size": metadata.st_size,
                    "artifact_id": None,
                }
            )
    references.extend(
        {
            "role": "log",
            "path": log["ref"],
            "byte_size": log["byte_size"],
            "artifact_id": log["artifact_id"],
        }
        for log in service_log_artifacts(run_root)
    )
    return references


# Directory timestamps advance in kernel clock ticks, so a change landing in the
# same tick as a listing leaves them unchanged. Listings of directories changed
# this recently are not trusted and are taken again on the next snapshot.
_LISTING_SETTLE_NS = 2_000_000_000


@dataclass(frozen=True, slots=True)
class PlannerArtifactSnapshot:
    """Allowlisted regular files beneath one planner root, ordered by ref.

    ``version`` changes only when the set of files or one file's identity,
    type, size, mtime, or ctime changes; unchanged snapshots are one object.
    """

    version: int
    files: tuple[tuple[Path, os.stat_result], ...]


@dataclass(frozen=True, slots=True)
class _DirectoryListing:
    identity: tuple[int, int, int, int]
    settled: bool
    subdirectories: tuple[tuple[tuple[str, ...], Path], ...]
    files: tuple[tuple[str, Path], ...]


class PlannerArtifactInventory:
    """Track allowlisted regular files beneath a planner root.

    Every snapshot lstats each directory from the root down and each candidate
    file, never following a symlinked root, directory, or file. Directory
    listings are reused while the directory's device, inode, mtime, and ctime
    are unchanged. File contents are never read.
    """

    def __init__(self, root: Path, allowed_names: Collection[str]) -> None:
        self._root = root
        self._allowed_names = frozenset(allowed_names)
        self._listings: dict[tuple[str, ...], _DirectoryListing] = {}
        self._identities: tuple[tuple[object, ...], ...] = ()
        self._snapshot = PlannerArtifactSnapshot(version=0, files=())

    def snapshot(self) -> PlannerArtifactSnapshot:
        files = self._scan()
        identities = tuple(
            (
                path,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            for path, metadata in files
        )
        if identities != self._identities:
            self._identities = identities
            self._snapshot = PlannerArtifactSnapshot(
                version=self._snapshot.version + 1, files=files
            )
        return self._snapshot

    def _scan(self) -> tuple[tuple[Path, os.stat_result], ...]:
        listed_after_ns = time.time_ns()
        visited: list[tuple[tuple[str, ...], Path, _DirectoryListing]] = []
        found: list[tuple[tuple[str, ...], str, Path, os.stat_result]] = []
        pending: list[tuple[tuple[str, ...], Path]] = [((), self._root)]
        while pending:
            parts, directory = pending.pop()
            listing = self._listing(parts, directory, listed_after_ns)
            if listing is None:
                continue
            visited.append((parts, directory, listing))
            for ref, path in listing.files:
                try:
                    metadata = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    found.append((parts, ref, path, metadata))
            pending.extend(listing.subdirectories)
        # Paths resolve through every ancestor, so a directory renamed or
        # replaced by a symlink mid-scan could have led later lstats outside
        # the tree. Drop every subtree whose directory is no longer the one read.
        replaced = {
            parts
            for parts, directory, listing in visited
            if not _same_directory(directory, listing.identity)
        }
        if replaced:
            visited = [item for item in visited if not _beneath(item[0], replaced)]
            found = [item for item in found if not _beneath(item[0], replaced)]
        self._listings = {parts: listing for parts, _, listing in visited}
        found.sort(key=lambda item: item[1])
        return tuple((path, metadata) for _, _, path, metadata in found)

    def _listing(
        self, parts: tuple[str, ...], directory: Path, listed_after_ns: int
    ) -> _DirectoryListing | None:
        try:
            metadata = os.lstat(directory)
        except OSError:
            return None
        if not stat.S_ISDIR(metadata.st_mode):
            return None
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        cached = self._listings.get(parts)
        if cached is not None and cached.settled and cached.identity == identity:
            return cached
        subdirectories: list[str] = []
        files: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_directory:
                        subdirectories.append(entry.name)
                    elif entry.name in self._allowed_names:
                        files.append(entry.name)
        except OSError:
            return None
        prefix = "".join(f"{part}/" for part in parts)
        return _DirectoryListing(
            identity=identity,
            settled=max(metadata.st_mtime_ns, metadata.st_ctime_ns)
            + _LISTING_SETTLE_NS
            < listed_after_ns,
            subdirectories=tuple(
                ((*parts, name), directory / name) for name in sorted(subdirectories)
            ),
            files=tuple(
                (f"{prefix}{name}", directory / name) for name in sorted(files)
            ),
        )


def _same_directory(path: Path, identity: tuple[int, int, int, int]) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and (
        metadata.st_dev,
        metadata.st_ino,
    ) == identity[:2]


def _beneath(parts: tuple[str, ...], directories: set[tuple[str, ...]]) -> bool:
    return any(parts[:depth] in directories for depth in range(len(parts) + 1))


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "AUDIT_ARTIFACT_NAME",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_PREVIEW_BYTES",
    "MAX_ARTIFACT_BYTES",
    "MAX_ENTRY_BYTES",
    "MAX_ENTRY_CONTENT_BYTES",
    "MAX_ENVELOPE_BYTES",
    "MAX_PAGE_SIZE",
    "MAX_PREVIEW_BYTES",
    "RECEIPT_EXPORT_NAME",
    "ArtifactNotFoundError",
    "ArtifactUnavailableError",
    "PlannerArtifactInventory",
    "PlannerArtifactSnapshot",
    "PublicArtifactInbox",
    "live_demo_audit",
    "receipt_artifact_references",
    "receipt_export",
]
