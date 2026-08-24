"""Durable public observations and deterministic activity projections."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import quote

from onr.adapters.operational_log import FileOperationalLog
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    TransportEvent,
)
from onr.viewer.trace import TraceViewItem

OBSERVATION_SCHEMA_VERSION = 1
ACTIVITY_MAPPING_VERSION = 1
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

_MAX_EVIDENCE_BYTES = 1024 * 1024
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_MARKER_SUMMARIES = {
    "duplicate": "Duplicate evidence",
    "replayed": "Replayed evidence",
    "stale": "Stale evidence",
    "gap": "Evidence gap",
    "resynchronized": "Resynchronized evidence",
    "conflict": "Conflicting evidence",
    "malformed": "Malformed evidence",
}


@dataclass(frozen=True, slots=True)
class ActivityPartition:
    kind: Literal["marker", "group", "single"]
    key: str
    members: list[Mapping[str, object]]


class InvalidCursorError(ValueError):
    """A paging cursor is malformed or does not belong to the requested run."""


class EvidenceSource(Protocol):
    """Provide raw public evidence records for one Mission."""

    def records(self, mission_id: str) -> Iterable[Mapping[str, object]]: ...


def encode_cursor(mission_run_id: str, sequence: int) -> str:
    """Encode a run-scoped sequence as a compact opaque cursor."""

    payload = json.dumps(
        {"v": 1, "run": mission_run_id, "seq": sequence},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(
    value: str, *, mission_run_id: str, max_sequence: int
) -> int:
    """Decode and validate a run-scoped sequence cursor."""

    try:
        if not isinstance(value, str) or not value or _CURSOR_RE.fullmatch(value) is None:
            raise ValueError
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding, altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
            raise ValueError
        payload = json.loads(
            decoded.decode("utf-8"), object_pairs_hook=_cursor_object
        )
        if not isinstance(payload, dict) or set(payload) != {"v", "run", "seq"}:
            raise ValueError
        version = payload["v"]
        sequence = payload["seq"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != 1
            or payload["run"] != mission_run_id
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or sequence > max_sequence
        ):
            raise ValueError
        return sequence
    except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidCursorError from exc


class FileEvidenceSource:
    """Read public operational and file-transport evidence defensively."""

    def __init__(
        self, storage_root: Path, transport_backend: str, transport_root: Path
    ) -> None:
        self.storage_root = Path(storage_root)
        self.transport_backend = transport_backend
        self.transport_root = Path(transport_root)

    def records(self, mission_id: str) -> Iterable[Mapping[str, object]]:
        records: list[Mapping[str, object]] = []
        try:
            records.extend(
                record.to_dict()
                for record in FileOperationalLog(
                    self.storage_root / "operational-log"
                ).replay(mission_id)
            )
        except Exception:  # noqa: BLE001 - evidence collection fails closed.
            records = []
        try:
            if self.transport_backend == "file":
                records.extend(self._transport_events(mission_id))
                records.extend(self._commands(mission_id))
        except Exception:  # noqa: BLE001 - return all evidence collected so far.
            return records
        return records

    def _transport_events(self, mission_id: str) -> list[Mapping[str, object]]:
        records: list[Mapping[str, object]] = []
        encoded_mission = quote(mission_id, safe="._-")
        for topic_dir in _safe_dirs(self.transport_root / "topics"):
            mission_dir = topic_dir / "missions" / encoded_mission
            if not _safe_directory(mission_dir, topic_dir / "missions"):
                continue
            for path in _safe_json_files(mission_dir):
                prefix = path.name.split("-", 1)[0]
                raw = _read_mapping(path, root=self.transport_root)
                if raw is None or not prefix.isdigit():
                    continue
                try:
                    event = TransportEvent.from_dict(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                if event.sequence != int(prefix) or event.mission_id != mission_id:
                    continue
                records.append(event.to_dict())
        return records

    def _commands(self, mission_id: str) -> list[Mapping[str, object]]:
        records: list[Mapping[str, object]] = []
        commands: dict[str, Command] = {}
        outcomes: list[CommandOutcome] = []
        encoded_mission = quote(mission_id, safe="._-")
        for service_dir in _safe_dirs(self.transport_root / "commands"):
            mission_dir = service_dir / encoded_mission
            if not _safe_directory(mission_dir, service_dir):
                continue
            for path in _safe_json_files(mission_dir):
                prefix = path.name.split("-", 1)[0]
                envelope = _read_mapping(path, root=self.transport_root)
                if envelope is None or not prefix.isdigit():
                    continue
                sequence = int(prefix)
                if envelope.get("sequence") != sequence:
                    continue
                try:
                    if envelope.get("kind") == "command":
                        raw = envelope.get("command")
                        if not isinstance(raw, Mapping):
                            continue
                        command = Command.from_dict(raw)
                        if (
                            command.mission_id != mission_id
                            or quote(command.target_service, safe="._-")
                            != service_dir.name
                            or envelope.get("command_kind") != command.command_kind
                        ):
                            continue
                        commands[command.command_id] = command
                        records.append(command.to_dict())
                    elif envelope.get("kind") == "outcome":
                        raw = envelope.get("outcome")
                        if not isinstance(raw, Mapping):
                            continue
                        outcome = CommandOutcome.from_dict(raw)
                        if outcome.mission_id == mission_id:
                            outcomes.append(outcome)
                except (KeyError, TypeError, ValueError):
                    continue

        seen_receipts: set[str] = set()
        for command_id, command in sorted(commands.items()):
            receipt_path = (
                self.transport_root
                / "receipts"
                / f"{quote(command_id, safe='._-')}.json"
            )
            raw = _read_mapping(receipt_path, root=self.transport_root)
            if raw is None:
                continue
            try:
                receipt = CommandReceipt.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if (
                receipt.command_id != command.command_id
                or receipt.correlation_id != command.correlation_id
                or receipt.mission_id != command.mission_id
                or receipt.target_service != command.target_service
                or receipt.command_id in seen_receipts
            ):
                continue
            seen_receipts.add(receipt.command_id)
            records.append(receipt.to_dict())

        for outcome in outcomes:
            command = commands.get(outcome.command_id)
            if command is not None and command.correlation_id == outcome.correlation_id:
                records.append(outcome.to_dict())
        return records


class ObservationLog:
    """Durable append-and-refresh store for issued Host observations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries = self._load()

    def ingest(self, items: Iterable[TraceViewItem], *, observed_at: str) -> None:
        by_event_id = {
            cast(str, entry["event_id"]): entry for entry in self.entries
        }
        changed = False
        for item in items:
            rendered = item.to_dict()
            existing = by_event_id.get(item.event_id)
            if existing is None:
                entry: dict[str, object] = {
                    "observation_sequence": len(self.entries) + 1,
                    "observed_at": observed_at,
                    "event_id": item.event_id,
                    "item": rendered,
                }
                self.entries.append(entry)
                by_event_id[item.event_id] = entry
                changed = True
            elif existing["item"] != rendered:
                # Deterministic reprojection may truthfully refresh dispositions such
                # as normal to stale without changing the issued sequence or timestamp.
                existing["item"] = rendered
                changed = True
        if changed:
            self._save()

    def _load(self) -> list[dict[str, object]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError("runtime host observation log is invalid") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "entries"}
            or raw["schema_version"] != OBSERVATION_SCHEMA_VERSION
            or not isinstance(raw["entries"], list)
        ):
            raise RuntimeError("runtime host observation log is invalid")
        entries: list[dict[str, object]] = []
        event_ids: set[str] = set()
        for expected_sequence, raw_entry in enumerate(raw["entries"], 1):
            if not isinstance(raw_entry, dict) or set(raw_entry) != {
                "observation_sequence",
                "observed_at",
                "event_id",
                "item",
            }:
                raise RuntimeError("runtime host observation log is invalid")
            sequence = raw_entry["observation_sequence"]
            event_id = raw_entry["event_id"]
            raw_item = raw_entry["item"]
            if (
                isinstance(sequence, bool)
                or sequence != expected_sequence
                or not isinstance(event_id, str)
                or not event_id
                or event_id in event_ids
                or not isinstance(raw_entry["observed_at"], str)
                or not raw_entry["observed_at"].strip()
                or not isinstance(raw_item, dict)
            ):
                raise RuntimeError("runtime host observation log is invalid")
            try:
                item = TraceViewItem.from_dict(raw_item)
            except (TypeError, ValueError, KeyError) as exc:
                raise RuntimeError("runtime host observation log is invalid") from exc
            if item.to_dict() != raw_item or item.event_id != event_id:
                raise RuntimeError("runtime host observation log is invalid")
            event_ids.add(event_id)
            entries.append(dict(raw_entry))
        return entries

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": OBSERVATION_SCHEMA_VERSION,
                    "entries": self.entries,
                },
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def page_entries(
    entries: Iterable[Mapping[str, object]], *, after: int, limit: int
) -> tuple[list[dict[str, object]], int | None]:
    """Return an ascending page after the selected sequence."""

    page: list[dict[str, object]] = []
    for entry in entries:
        sequence = _entry_sequence(entry)
        if sequence > after:
            page.append(dict(entry))
            if len(page) == limit:
                break
    return page, (_entry_sequence(page[-1]) if page else None)


def map_activities(entries: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Map issued observations to deterministic non-authoritative activities."""

    ordered = sorted(entries, key=_entry_sequence)
    groups: dict[str, list[Mapping[str, object]]] = {}
    partitions: list[ActivityPartition] = []
    for entry in ordered:
        item = _item(entry)
        disposition = _text_value(item.get("replay_disposition")) or "normal"
        correlation_id = _text_value(item.get("correlation_id"))
        if disposition != "normal":
            partitions.append(
                ActivityPartition(
                    "marker",
                    _text_value(item.get("event_id")) or "unknown",
                    [entry],
                )
            )
        elif correlation_id:
            if correlation_id not in groups:
                groups[correlation_id] = []
                partitions.append(
                    ActivityPartition("group", correlation_id, groups[correlation_id])
                )
            groups[correlation_id].append(entry)
        else:
            partitions.append(
                ActivityPartition(
                    "single",
                    _text_value(item.get("event_id")) or "unknown",
                    [entry],
                )
            )

    activities: list[dict[str, object]] = []
    for partition in sorted(
        partitions,
        key=lambda value: min(_entry_sequence(item) for item in value.members),
    ):
        partition_kind = partition.kind
        key = partition.key
        members = partition.members
        member_items = [_item(member) for member in members]
        first = member_items[0]
        outcome_member: Mapping[str, object] | None = None
        if partition_kind != "marker":
            for item in member_items:
                if item.get("outcome") is not None:
                    outcome_member = item
        outcome = None if outcome_member is None else outcome_member.get("outcome")
        component = _text_value(first.get("component")) or "unknown"
        event_kind = _text_value(first.get("event_kind")) or "unknown"
        disposition = _text_value(first.get("replay_disposition")) or "normal"
        if partition_kind == "marker":
            kind = "evidence_marker"
            status = disposition
            summary = _MARKER_SUMMARIES.get(disposition, f"{disposition} evidence")
            activity_id = f"marker:{key}"
            correlation_id = None
        elif partition_kind == "group":
            command_item = next(
                (item for item in member_items if item.get("event_kind") == "command"),
                None,
            )
            kind = "maneuver_command" if command_item is not None else "correlated"
            status = cast(str, outcome) if outcome is not None else "active"
            if command_item is not None:
                payload = command_item.get("payload")
                target = (
                    _text_value(payload.get("target_service"))
                    if isinstance(payload, Mapping)
                    else None
                )
                summary = f"Maneuver command {target or 'unknown'}: {status}"
            else:
                summary = f"{component} {event_kind}: {status}"
            activity_id = f"correlation:{key}"
            correlation_id = key
        else:
            authority = _text_value(first.get("authority"))
            kind = "operational" if authority == "operational-log" else "observation"
            status = cast(str, outcome) if outcome is not None else "recorded"
            if kind == "operational":
                summary = f"Operational {event_kind}: {status}"
            else:
                summary = f"{component} {event_kind}"
                if outcome is not None:
                    summary += f": {outcome}"
            activity_id = f"event:{key}"
            correlation_id = None
        activities.append(
            {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "activity_id": activity_id,
                "activity_sequence": len(activities) + 1,
                "mapping_version": ACTIVITY_MAPPING_VERSION,
                "kind": kind,
                "status": status,
                "summary": summary,
                "component": component,
                "event_kind": event_kind,
                "outcome": outcome,
                "correlation_id": correlation_id,
                "started_at": first.get("occurred_at"),
                "finished_at": (
                    None if outcome_member is None else outcome_member.get("occurred_at")
                ),
                "observation_sequences": sorted(
                    _entry_sequence(member) for member in members
                ),
                "replay_disposition": disposition,
                "redacted_fields": _field_union(member_items, "redacted_fields"),
                "missing_fields": _field_union(member_items, "missing_fields"),
            }
        )
    return activities


def _safe_dirs(root: Path) -> tuple[Path, ...]:
    try:
        children = tuple(root.iterdir())
    except OSError:
        return ()
    return tuple(
        sorted(
            (child for child in children if _safe_directory(child, root)),
            key=lambda item: item.name,
        )
    )


def _safe_directory(path: Path, root: Path) -> bool:
    try:
        return (
            not path.is_symlink()
            and path.is_dir()
            and path.resolve().is_relative_to(root.resolve())
        )
    except OSError:
        return False


def _safe_json_files(root: Path) -> tuple[Path, ...]:
    try:
        children = tuple(root.iterdir())
    except OSError:
        return ()
    result: list[Path] = []
    for child in children:
        try:
            if (
                child.suffix == ".json"
                and not child.is_symlink()
                and child.is_file()
                and child.resolve().is_relative_to(root.resolve())
            ):
                result.append(child)
        except OSError:
            continue
    return tuple(sorted(result, key=lambda item: item.name))


def _read_mapping(path: Path, *, root: Path) -> Mapping[str, object] | None:
    descriptor: int | None = None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        if path.is_symlink():
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_EVIDENCE_BYTES:
            return None
        data = os.read(descriptor, _MAX_EVIDENCE_BYTES + 1)
        if len(data) > _MAX_EVIDENCE_BYTES:
            return None
        value = json.loads(data.decode("utf-8"))
        return cast(Mapping[str, object], value) if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _entry_sequence(entry: Mapping[str, object]) -> int:
    value = entry.get("observation_sequence", entry.get("activity_sequence"))
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("runtime host observation entry sequence is invalid")
    return value


def _item(entry: Mapping[str, object]) -> Mapping[str, object]:
    value = entry.get("item")
    if not isinstance(value, Mapping):
        raise TypeError("runtime host observation entry item is invalid")
    return value


def _text_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _field_union(items: Iterable[Mapping[str, object]], field: str) -> list[str]:
    values: set[str] = set()
    for item in items:
        supplied = item.get(field)
        if isinstance(supplied, (list, tuple)):
            values.update(value for value in supplied if isinstance(value, str))
    return sorted(values)


def _cursor_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("cursor JSON contains duplicate keys")
        result[key] = value
    return result


__all__ = [
    "ACTIVITY_MAPPING_VERSION",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "OBSERVATION_SCHEMA_VERSION",
    "EvidenceSource",
    "FileEvidenceSource",
    "InvalidCursorError",
    "ObservationLog",
    "decode_cursor",
    "encode_cursor",
    "map_activities",
    "page_entries",
]
