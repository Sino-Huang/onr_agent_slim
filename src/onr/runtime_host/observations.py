"""Durable public observations and deterministic activity projections."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    TransportEvent,
)
from onr.ports.operational_log import OperationalLogRecord
from onr.viewer.trace import (
    PROJECTED_TRANSPORT_PAYLOAD_FIELDS,
    MemoizedTraceProjection,
    TraceProjection,
    TraceViewItem,
)

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


class EvidenceTailer:
    """Incrementally read one Mission's public operational and transport evidence.

    The tailer keeps high-water marks so each poll reads only evidence files
    written since the previous poll: the operational-log sequence, the file names
    already seen in every topic and command stream, the commands still waiting
    for a receipt, and the outcomes still waiting for their command. Evidence
    files are written atomically and never rewritten, so a file is read once.

    Transport event payloads are reduced to the keys the trace projection reads
    (``PROJECTED_TRANSPORT_PAYLOAD_FIELDS``) as soon as an event is validated, so
    bulky payloads such as ``environment-planning`` snapshots are parsed once and
    never retained or re-projected.
    """

    def __init__(
        self,
        mission_id: str,
        *,
        operational_log_root: Path,
        transport_root: Path | None,
    ) -> None:
        self.mission_id = mission_id
        self.operational_log_root = Path(operational_log_root)
        self.transport_root = None if transport_root is None else Path(transport_root)
        self._encoded_mission = quote(mission_id, safe="._-")
        self._operational: list[Mapping[str, object]] = []
        self._operational_sequence = 0
        self._events: list[Mapping[str, object]] = []
        self._topic_files: dict[str, set[str]] = {}
        self._command_records: list[Mapping[str, object]] = []
        self._command_files: dict[str, set[str]] = {}
        self._commands: dict[str, Command] = {}
        self._awaiting_receipt: set[str] = set()
        self._receipts: list[Mapping[str, object]] = []
        self._pending_outcomes: list[CommandOutcome] = []
        self._outcomes: list[Mapping[str, object]] = []

    def poll(self) -> list[Mapping[str, object]]:
        """Read evidence written since the previous poll and return only it."""

        new = self._poll_operational()
        if self.transport_root is None:
            return new
        try:
            new.extend(self._poll_topics(self.transport_root))
            new.extend(self._poll_commands(self.transport_root))
            new.extend(self._poll_receipts(self.transport_root))
            new.extend(self._resolve_outcomes())
        except Exception:  # noqa: BLE001 - keep evidence collected so far.
            return new
        return new

    def records(self) -> list[Mapping[str, object]]:
        """Return every record read so far, operational log first."""

        return [
            *self._operational,
            *self._events,
            *self._command_records,
            *self._receipts,
            *self._outcomes,
        ]

    def operational_records(self) -> tuple[Mapping[str, object], ...]:
        """Return the contiguous operational-log prefix read so far."""

        return tuple(self._operational)

    def _poll_operational(self) -> list[Mapping[str, object]]:
        mission_id = self.mission_id
        if mission_id in {"", ".", ".."} or Path(mission_id).name != mission_id:
            return []
        mission_dir = self.operational_log_root / mission_id / "events"
        candidates: dict[int, str] = {}
        for name in _json_names(mission_dir):
            stem = name[: -len(".json")]
            if stem.isdigit():
                sequence = int(stem)
                if sequence > self._operational_sequence:
                    candidates[sequence] = name
        new: list[Mapping[str, object]] = []
        for sequence in sorted(candidates):
            if sequence != self._operational_sequence + 1:
                break  # A gap: wait until the missing record is published.
            try:
                raw = json.loads(
                    (mission_dir / candidates[sequence]).read_text(encoding="utf-8")
                )
                record = OperationalLogRecord.from_dict(raw)
            except (OSError, UnicodeError, ValueError, TypeError, KeyError):
                break
            if record.mission_id != mission_id or record.sequence != sequence:
                break
            rendered = record.to_dict()
            self._operational.append(rendered)
            new.append(rendered)
            self._operational_sequence = sequence
        return new

    def _poll_topics(self, transport_root: Path) -> list[Mapping[str, object]]:
        new: list[Mapping[str, object]] = []
        for topic_dir in _safe_dirs(transport_root / "topics"):
            missions = topic_dir / "missions"
            mission_dir = missions / self._encoded_mission
            seen = self._topic_files.setdefault(topic_dir.name, set())
            names = sorted(set(_json_names(mission_dir)) - seen)
            if not names or not _safe_directory(mission_dir, missions):
                continue
            for name in names:
                seen.add(name)
                prefix = name.split("-", 1)[0]
                if not prefix.isdigit():
                    continue
                raw = _read_mapping(mission_dir / name, root=transport_root)
                if raw is None:
                    continue
                try:
                    event = TransportEvent.from_dict(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                if event.sequence != int(prefix) or event.mission_id != self.mission_id:
                    continue
                record = event.to_dict()
                payload = cast(Mapping[str, object], record["payload"])
                record["payload"] = {
                    key: value
                    for key, value in payload.items()
                    if key in PROJECTED_TRANSPORT_PAYLOAD_FIELDS
                }
                self._events.append(record)
                new.append(record)
        return new

    def _poll_commands(self, transport_root: Path) -> list[Mapping[str, object]]:
        new: list[Mapping[str, object]] = []
        for service_dir in _safe_dirs(transport_root / "commands"):
            mission_dir = service_dir / self._encoded_mission
            seen = self._command_files.setdefault(service_dir.name, set())
            names = sorted(set(_json_names(mission_dir)) - seen)
            if not names or not _safe_directory(mission_dir, service_dir):
                continue
            for name in names:
                seen.add(name)
                prefix = name.split("-", 1)[0]
                envelope = _read_mapping(mission_dir / name, root=transport_root)
                if envelope is None or not prefix.isdigit():
                    continue
                if envelope.get("sequence") != int(prefix):
                    continue
                try:
                    if envelope.get("kind") == "command":
                        raw = envelope.get("command")
                        if not isinstance(raw, Mapping):
                            continue
                        command = Command.from_dict(raw)
                        if (
                            command.mission_id != self.mission_id
                            or quote(command.target_service, safe="._-")
                            != service_dir.name
                            or envelope.get("command_kind") != command.command_kind
                        ):
                            continue
                        self._commands[command.command_id] = command
                        self._awaiting_receipt.add(command.command_id)
                        rendered = command.to_dict()
                        self._command_records.append(rendered)
                        new.append(rendered)
                    elif envelope.get("kind") == "outcome":
                        raw = envelope.get("outcome")
                        if not isinstance(raw, Mapping):
                            continue
                        outcome = CommandOutcome.from_dict(raw)
                        if outcome.mission_id == self.mission_id:
                            self._pending_outcomes.append(outcome)
                except (KeyError, TypeError, ValueError):
                    continue
        return new

    def _poll_receipts(self, transport_root: Path) -> list[Mapping[str, object]]:
        new: list[Mapping[str, object]] = []
        for command_id in sorted(self._awaiting_receipt):
            command = self._commands[command_id]
            raw = _read_mapping(
                transport_root / "receipts" / f"{quote(command_id, safe='._-')}.json",
                root=transport_root,
            )
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
            ):
                continue
            self._awaiting_receipt.discard(command_id)
            rendered = receipt.to_dict()
            self._receipts.append(rendered)
            new.append(rendered)
        return new

    def _resolve_outcomes(self) -> list[Mapping[str, object]]:
        new: list[Mapping[str, object]] = []
        waiting: list[CommandOutcome] = []
        for outcome in self._pending_outcomes:
            command = self._commands.get(outcome.command_id)
            if command is None:
                waiting.append(outcome)
            elif command.correlation_id == outcome.correlation_id:
                rendered = outcome.to_dict()
                self._outcomes.append(rendered)
                new.append(rendered)
        self._pending_outcomes = waiting
        return new


class ObservationLog:
    """Durable append-and-refresh store for issued Host observations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries = self._load()
        self._positions = {
            cast(str, entry["event_id"]): index
            for index, entry in enumerate(self.entries)
        }

    def ingest(self, items: Iterable[TraceViewItem], *, observed_at: str) -> bool:
        return self.ingest_rendered(
            ((item.event_id, item.to_dict()) for item in items), observed_at=observed_at
        )

    def ingest_rendered(
        self,
        items: Iterable[tuple[str, dict[str, object]]],
        *,
        observed_at: str,
    ) -> bool:
        """Issue new items and refresh changed ones; return whether anything changed.

        Entries are replaced rather than mutated, so a tuple of ``entries`` taken
        earlier stays a consistent snapshot.
        """

        changed = False
        for event_id, rendered in items:
            position = self._positions.get(event_id)
            if position is None:
                self._positions[event_id] = len(self.entries)
                self.entries.append(
                    {
                        "observation_sequence": len(self.entries) + 1,
                        "observed_at": observed_at,
                        "event_id": event_id,
                        "item": rendered,
                    }
                )
                changed = True
                continue
            existing = self.entries[position]
            if existing["item"] is not rendered and existing["item"] != rendered:
                # Deterministic reprojection may truthfully refresh dispositions such
                # as normal to stale without changing the issued sequence or timestamp.
                self.entries[position] = {**existing, "item": rendered}
                changed = True
        if changed:
            self._save()
        return changed

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


_TAILED_GROUP_ORDER = (
    "operational_log",
    "transport_event",
    "command",
    "receipt",
    "outcome",
)
# A projected item kept alive with its rendered dict, keyed by ``id(item)``.
_Render = tuple[TraceViewItem, dict[str, object]]


class RunObservations:
    """One Mission Run's issued observations, refreshed from its evidence.

    With an ``EvidenceTailer`` only projection batches that received new records
    are re-projected, and unchanged batches reuse their rendered items. With a
    plain ``EvidenceSource`` every refresh rescans and re-projects everything.
    Refreshes are serialized by a per-run lock that is independent of the Host
    state lock; readers use ``entries()`` snapshots.
    """

    def __init__(
        self,
        log_path: Path,
        mission_id: str,
        *,
        tailer: EvidenceTailer | None = None,
        source: EvidenceSource | None = None,
    ) -> None:
        if (tailer is None) == (source is None):
            raise ValueError("run observations need exactly one evidence reader")
        self.mission_id = mission_id
        self.log = ObservationLog(log_path)
        self._tailer = tailer
        self._source = source
        self._lock = Lock()
        self._batches: dict[str, list[Mapping[str, object]]] = {}
        self._rendered: dict[str, list[tuple[str, dict[str, object]]]] = {}
        self._renders: dict[str, dict[int, _Render]] = {}
        self._projection = MemoizedTraceProjection()
        self._operational: tuple[Mapping[str, object], ...] = ()
        self._revision = 0
        self._activities: tuple[int, list[dict[str, object]]] | None = None

    def refresh(
        self, *, observed_at: str, wait: bool = True
    ) -> tuple[dict[str, object], ...]:
        """Ingest new evidence and return the issued entries.

        With ``wait=False`` a caller that finds another refresh in progress returns
        the latest published entries instead of blocking.
        """

        if not self._lock.acquire(blocking=wait):
            return self.entries()
        try:
            if self._tailer is not None:
                changed = self._refresh_tailed(self._tailer, observed_at)
            else:
                source = cast(EvidenceSource, self._source)
                changed = self._refresh_rescanned(source, observed_at)
            if changed:
                self._revision += 1
        finally:
            self._lock.release()
        return self.entries()

    def entries(self) -> tuple[dict[str, object], ...]:
        return tuple(self.log.entries)

    def operational_records(self) -> tuple[Mapping[str, object], ...]:
        """Return the raw operational-log records behind the issued observations."""

        if self._tailer is not None:
            return self._tailer.operational_records()
        return self._operational

    def activities(self) -> list[dict[str, object]]:
        """Return ``map_activities`` over the entries, cached per log revision."""

        revision = self._revision
        cached = self._activities
        if cached is None or cached[0] != revision:
            cached = (revision, map_activities(self.entries()))
            self._activities = cached
        return cached[1]

    def _refresh_tailed(self, tailer: EvidenceTailer, observed_at: str) -> bool:
        changed: set[str] = set()
        for record in tailer.poll():
            key = _projection_batch_key(record)
            self._batches.setdefault(key, []).append(record)
            changed.add(key)
        if not changed:
            return False
        for key in changed:
            previous = self._renders.get(key, {})
            renders: dict[int, _Render] = {}
            rendered: list[tuple[str, dict[str, object]]] = []
            for item in self._projection.project(self._batches[key]):
                hit = previous.get(id(item))
                if hit is None or hit[0] is not item:
                    hit = (item, item.to_dict())
                renders[id(item)] = hit
                rendered.append((item.event_id, hit[1]))
            self._renders[key] = renders
            self._rendered[key] = rendered
        ordered = sorted(self._rendered, key=_tailed_group_rank)
        return self.log.ingest_rendered(
            (pair for key in ordered for pair in self._rendered[key]),
            observed_at=observed_at,
        )

    def _refresh_rescanned(self, source: EvidenceSource, observed_at: str) -> bool:
        try:
            records = list(source.records(self.mission_id))
        except Exception:  # noqa: BLE001 - retain the last committed evidence.
            records = []
        self._operational = tuple(
            record
            for record in records
            if _projection_batch_key(record) == "operational_log"
        )
        return self.log.ingest(project_evidence(records), observed_at=observed_at)


def _tailed_group_rank(key: str) -> tuple[int, str]:
    try:
        return _TAILED_GROUP_ORDER.index(key), key
    except ValueError:
        return len(_TAILED_GROUP_ORDER), key


def project_evidence(records: Sequence[object]) -> tuple[TraceViewItem, ...]:
    """Project heterogeneous public records without bypassing the redaction seam.

    Records are projected in batches of one record shape, in order of each
    shape's first appearance.
    """

    groups: dict[str, tuple[int, list[Any]]] = {}
    for index, record in enumerate(records):
        key = _projection_batch_key(record)
        group = groups.get(key)
        if group is None:
            group = (index, [])
            groups[key] = group
        group[1].append(record)
    projection = TraceProjection()
    return tuple(
        item
        for _, batch in sorted(groups.values(), key=lambda group: group[0])
        for item in projection.project(batch)
    )


def _projection_batch_key(record: object) -> str:
    if isinstance(record, str):
        try:
            decoded = json.loads(record)
        except (TypeError, ValueError):
            return "malformed"
        record = decoded
    if not isinstance(record, Mapping):
        return "malformed"
    keys = set(record)
    if "entry_state" in keys or "transitions" in keys and "states" in keys:
        return "statechart"
    if "record_id" in keys:
        return "operational_log"
    if "summary_id" in keys:
        return "summary"
    if "feedback_id" in keys:
        return "maneuver_feedback"
    if "request_id" in keys and "requester" in keys:
        return "replan_request"
    if "command_id" in keys and "command_kind" in keys:
        return "command"
    if "command_id" in keys and "target_service" in keys:
        return "receipt"
    if "command_id" in keys:
        return "outcome"
    if "event_id" in keys:
        return "transport_event"
    if "version" in keys or "source_references" in keys:
        return "snapshot"
    if "record_revision" in keys or "active_configuration" in keys:
        return "fsm_execution"
    if "transition_candidates" in keys:
        return "fsm_status"
    return "malformed"


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


def _json_names(directory: Path) -> list[str]:
    """Return the ``*.json`` entry names of a directory; empty when unreadable."""

    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [name for name in names if name.endswith(".json")]


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
    "EvidenceTailer",
    "InvalidCursorError",
    "ObservationLog",
    "RunObservations",
    "decode_cursor",
    "encode_cursor",
    "map_activities",
    "page_entries",
    "project_evidence",
]
