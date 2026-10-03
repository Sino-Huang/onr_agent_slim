"""Bounded, cached reads of JSON evidence files under a Mission Run root.

Operator-view projections poll these files every few hundred milliseconds.
Evidence files are written atomically (write-then-rename), so a file's
identity plus size and modification time pins its content. The cache keeps
only the decoded projection, never the raw document, so large inputs such as
the reporting-reliability state file do not stay resident.
"""

from __future__ import annotations

import json
import os
import stat
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Lock
from typing import Generic, TypeVar

T = TypeVar("T")

_FileKey = tuple[str, int, int, int, int]


class JsonFileCache(Generic[T]):
    """Decode JSON objects once per file version; return ``None`` when unusable."""

    def __init__(
        self,
        decode: Callable[[Mapping[str, object]], T],
        *,
        max_bytes: int,
        max_entries: int = 256,
    ) -> None:
        self._decode = decode
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._entries: OrderedDict[_FileKey, T | None] = OrderedDict()
        self._lock = Lock()

    def get(self, path: Path) -> T | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > self._max_bytes:
                return None
            key = (
                str(path),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_size,
            )
            with self._lock:
                if key in self._entries:
                    self._entries.move_to_end(key)
                    return self._entries[key]
            value = self._read(descriptor, metadata.st_size)
        finally:
            os.close(descriptor)
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return value

    def _read(self, descriptor: int, size: int) -> T | None:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            document = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            return None
        if not isinstance(document, dict):
            return None
        try:
            return self._decode(document)
        except (KeyError, TypeError, ValueError):
            return None


def json_file_names(directory: Path) -> list[str]:
    """Return visible regular ``*.json`` file names in ``directory``, unsorted."""

    try:
        entries = os.scandir(directory)
    except OSError:
        return []
    names: list[str] = []
    with entries:
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not name.endswith(".json"):
                continue
            try:
                if entry.is_file(follow_symlinks=False):
                    names.append(name)
            except OSError:
                continue
    return names


__all__ = ["JsonFileCache", "json_file_names"]
