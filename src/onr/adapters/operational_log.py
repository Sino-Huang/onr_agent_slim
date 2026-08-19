"""Durable and in-process operational log adapters."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from collections.abc import Mapping
import os

from onr.ports.operational_log import OperationalLogRecord


class FileOperationalLog:
    """One immutable JSON file per record, grouped by mission."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = RLock()

    @staticmethod
    def _mission_dir(root: Path, mission_id: str) -> Path:
        if not mission_id or Path(mission_id).name != mission_id or mission_id in {".", ".."}:
            raise ValueError("mission ID must be one path component")
        return root / mission_id / "events"

    def append(self, record: OperationalLogRecord) -> OperationalLogRecord:
        if not isinstance(record, OperationalLogRecord):
            raise TypeError("operational log append requires an OperationalLogRecord")
        with self._lock:
            existing = self.replay(record.mission_id)
            for existing_record in existing:
                if existing_record.record_id == record.record_id:
                    if existing_record == record:
                        return existing_record
                    raise ValueError("operational log record ID already contains another record")
            next_sequence = existing[-1].sequence + 1 if existing else 1
            if record.sequence != next_sequence:
                raise ValueError("operational log sequence must be the next mission sequence")
            mission_dir = self._mission_dir(self.root, record.mission_id)
            mission_dir.mkdir(parents=True, exist_ok=True)
            target = mission_dir / f"{record.sequence:020d}.json"
            if target.exists():
                with target.open(encoding="utf-8") as handle:
                    existing_record = OperationalLogRecord.from_dict(json.load(handle))
                if existing_record == record:
                    return existing_record
                raise ValueError("operational log sequence already contains another record")
            temporary = mission_dir / f".{record.sequence:020d}.tmp"
            payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                directory_fd = os.open(mission_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
            return record

    def emit(
        self,
        mission_id: str,
        source: str,
        event_kind: str,
        outcome: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> OperationalLogRecord:
        with self._lock:
            records = self.replay(mission_id)
            return self.append(
                OperationalLogRecord.create(
                    mission_id, source, event_kind, outcome, details=details,
                    sequence=(records[-1].sequence + 1 if records else 1),
                )
            )

    def replay(self, mission_id: str, *, after_sequence: int = 0) -> tuple[OperationalLogRecord, ...]:
        mission_dir = self._mission_dir(self.root, mission_id)
        if not mission_dir.exists():
            return ()
        records = []
        for path in sorted(mission_dir.glob("[0-9]*.json")):
            with path.open(encoding="utf-8") as handle:
                record = OperationalLogRecord.from_dict(json.load(handle))
            if record.mission_id != mission_id:
                raise ValueError("operational log mission directory contains another mission")
            records.append(record)
        records.sort(key=lambda item: item.sequence)
        if records and tuple(item.sequence for item in records) != tuple(
            range(1, records[-1].sequence + 1)
        ):
            raise ValueError("operational log contains a sequence gap")
        return tuple(record for record in records if record.sequence > after_sequence)

    def read_after_sequence(self, mission_id: str, sequence: int) -> tuple[OperationalLogRecord, ...]:
        return self.replay(mission_id, after_sequence=sequence)


class InProcessOperationalLog:
    """Small injectable logger for direct service tests."""

    def __init__(self) -> None:
        self._records: dict[str, list[OperationalLogRecord]] = {}
        self._lock = RLock()

    def append(self, record: OperationalLogRecord) -> OperationalLogRecord:
        with self._lock:
            records = self._records.setdefault(record.mission_id, [])
            for existing in records:
                if existing.record_id == record.record_id:
                    if existing == record:
                        return existing
                    raise ValueError("operational log record ID already contains another record")
            expected = len(records) + 1
            if record.sequence != expected:
                raise ValueError("operational log sequence must be the next mission sequence")
            records.append(record)
            return record

    def emit(self, mission_id: str, source: str, event_kind: str, outcome: str, *, details: Mapping[str, object] | None = None) -> OperationalLogRecord:
        with self._lock:
            records = self._records.get(mission_id, [])
            return self.append(OperationalLogRecord.create(mission_id, source, event_kind, outcome, details=details, sequence=len(records) + 1))

    def replay(self, mission_id: str, *, after_sequence: int = 0) -> tuple[OperationalLogRecord, ...]:
        with self._lock:
            return tuple(record for record in self._records.get(mission_id, ()) if record.sequence > after_sequence)

    def read_after_sequence(self, mission_id: str, sequence: int) -> tuple[OperationalLogRecord, ...]:
        return self.replay(mission_id, after_sequence=sequence)


__all__ = ["FileOperationalLog", "InProcessOperationalLog"]
