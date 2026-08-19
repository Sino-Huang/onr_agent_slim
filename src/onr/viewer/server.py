"""Read-only local HTTP server for the mission trace viewer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
from typing import Callable, Mapping, Sequence, cast
from urllib.parse import parse_qs, quote, unquote, urlsplit

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.application.bayesian_belief import belief_artifact_reference
from onr.contracts.context_coordination import (
    MissionSnapshot,
    mission_snapshot_from_transport_event,
)
from onr.contracts.fsm import FSMExecutionRecord, FSMStatus, ManeuverFeedback, Statechart
from onr.contracts.hyper_agent import ReplanRequest
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    TransportEvent,
)
from onr.ports.mission_log_summarizer import SummaryArtifact
from onr.ports.operational_log import OperationalLogRecord
from onr.runtime.config import RuntimeConfig, load_runtime_config
from onr.runtime.lease import RuntimeLease, RuntimeLeaseStore
from onr.viewer.debug import (
    KNOWN_DEBUG_ROLES,
    load_debug_artifacts,
    load_llm_conversations,
)
from onr.viewer.trace import TraceProjection, sanitize_payload


_MAX_ARTIFACT_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 64
_EPOCH = "1970-01-01T00:00:00+00:00"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MISSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
_SOURCE_ORDER = {
    "transport-event": 0,
    "transport-snapshot": 1,
    "transport-fsm": 2,
    "transport-feedback": 3,
    "transport-replan": 4,
    "command": 5,
    "receipt": 6,
    "outcome": 7,
    "operational-log": 8,
    "summary": 9,
    "statechart": 10,
    "fsm-execution": 11,
    "bayesian-belief": 12,
}


@dataclass(frozen=True, slots=True)
class _PublicArtifact:
    mission_id: str
    sequence: int
    identifier: str
    observed_at: str
    source_kind: str
    record: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _RuntimeView:
    config: RuntimeConfig
    store: RuntimeLeaseStore
    lease: RuntimeLease


def _aware_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return _EPOCH
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return _EPOCH


def _encoded(value: str) -> str:
    return quote(value, safe="._-")


def _safe_dirs(root: Path) -> tuple[Path, ...]:
    try:
        resolved_root = root.resolve()
        children = tuple(root.iterdir())
    except OSError:
        return ()
    result: list[Path] = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            child.resolve().relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        result.append(child)
    return tuple(sorted(result, key=lambda item: item.name))


def _mission_dirs(
    root: Path, mission_id: str | None, *, encoded: bool
) -> tuple[Path, ...]:
    if mission_id is None:
        return _safe_dirs(root)
    candidate = root / (_encoded(mission_id) if encoded else mission_id)
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            return ()
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return ()
    return (candidate,)


def _safe_json_files(root: Path) -> tuple[Path, ...]:
    try:
        resolved_root = root.resolve()
        children = tuple(root.iterdir())
    except OSError:
        return ()
    result: list[Path] = []
    for child in children:
        try:
            if child.suffix != ".json" or child.is_symlink() or not child.is_file():
                continue
            child.resolve().relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        result.append(child)
    return tuple(sorted(result, key=lambda item: item.name))


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _within_json_depth(value: object) -> bool:
    """Validate parsed container depth without recursive Python calls."""

    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            return False
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
    return True


def _open_confined(path: Path, root: Path) -> int:
    """Open one file beneath a non-symlink root using directory descriptors."""

    absolute_root = Path(os.path.abspath(root))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise OSError("artifact path escapes configured root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("artifact path is not confined")

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


def _read_mapping(
    path: Path, *, root: Path | None = None
) -> Mapping[str, object] | None:
    descriptor: int | None = None
    try:
        descriptor = _open_confined(path, root or path.parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ARTIFACT_BYTES:
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= _MAX_ARTIFACT_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_ARTIFACT_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > _MAX_ARTIFACT_BYTES:
            return None
        data = b"".join(chunks)
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        if not _within_json_depth(value):
            return None
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return cast(Mapping[str, object], value) if isinstance(value, dict) else None


def _artifact(
    mission_id: str,
    sequence: int,
    identifier: str,
    source_kind: str,
    record: Mapping[str, object],
    observed_at: str = _EPOCH,
) -> _PublicArtifact:
    return _PublicArtifact(
        mission_id, sequence, identifier, observed_at, source_kind, record
    )


def _typed_event_artifacts(event: TransportEvent) -> tuple[_PublicArtifact, ...]:
    """Validate known typed event payloads and expose their public record view."""

    if event.event_kind == "mission-snapshot":
        snapshot = mission_snapshot_from_transport_event(event)
        return (
            _artifact(
                snapshot.mission_id,
                snapshot.version,
                f"mission-snapshot:{snapshot.mission_id}:{snapshot.version}",
                "transport-snapshot",
                snapshot.to_dict(),
                _aware_timestamp(snapshot.created_at),
            ),
        )
    if event.event_kind == "fsm-status":
        status = FSMStatus.from_dict(event.payload)
        if status.mission_id != event.mission_id:
            raise ValueError("FSM status mission mismatch")
        return (
            _artifact(
                status.mission_id,
                status.statechart_revision,
                event.event_id,
                "transport-fsm",
                status.to_dict(),
            ),
        )
    if event.event_kind == "statechart":
        chart = Statechart.from_dict(event.payload)
        if chart.mission_id != event.mission_id:
            raise ValueError("Statechart mission mismatch")
        return (
            _artifact(
                chart.mission_id,
                chart.plan_revision,
                event.event_id,
                "transport-fsm",
                chart.to_dict(),
            ),
        )
    if event.event_kind == "fsm-execution-record":
        execution = FSMExecutionRecord.from_dict(event.payload)
        if execution.mission_id != event.mission_id:
            raise ValueError("FSM execution mission mismatch")
        return (
            _artifact(
                execution.mission_id,
                execution.record_revision,
                event.event_id,
                "transport-fsm",
                execution.to_dict(),
            ),
        )
    if event.event_kind == "maneuver-feedback":
        feedback = ManeuverFeedback.from_dict(event.payload)
        if feedback.mission_id != event.mission_id:
            raise ValueError("maneuver feedback mission mismatch")
        payload = feedback.payload
        command_id = payload.get("command_id")
        correlation_id = payload.get("correlation_id")
        explicit_parent = payload.get("parent_id")
        source = payload.get("source", "environment")
        plan_revision = payload.get("plan_revision")
        snapshot_id = payload.get("snapshot_id", payload.get("mission_snapshot_id"))
        if source not in {"environment", "maneuver-control"}:
            source = "environment"
        for value in (command_id, correlation_id, explicit_parent, snapshot_id):
            if value is not None and not isinstance(value, str):
                raise ValueError("feedback public reference must be text")
        if plan_revision is not None and (
            isinstance(plan_revision, bool)
            or not isinstance(plan_revision, int)
            or plan_revision < 0
        ):
            raise ValueError("feedback plan revision is invalid")
        parent_id = explicit_parent
        if parent_id is None and isinstance(command_id, str) and command_id:
            parent_id = f"command:{command_id}"
        if parent_id is None:
            parent_id = event.event_id
        record = {
            "schema_version": 1,
            "feedback_id": feedback.feedback_id,
            "mission_id": feedback.mission_id,
            "maneuver_id": feedback.maneuver_id,
            "lifecycle": feedback.lifecycle,
            "source_sequence": event.sequence,
            "source": source,
            "command_id": command_id,
            "correlation_id": correlation_id,
            "parent_id": parent_id,
            "plan_revision": plan_revision,
            "snapshot_id": snapshot_id,
        }
        return (
            _artifact(
                feedback.mission_id,
                event.sequence,
                feedback.feedback_id,
                "transport-feedback",
                record,
            ),
        )
    if event.event_kind == "replan-request":
        request = ReplanRequest.from_dict(event.payload)
        if request.mission_id != event.mission_id:
            raise ValueError("replan request mission mismatch")
        record = {
            "schema_version": 1,
            "request_id": request.request_id,
            "mission_id": request.mission_id,
            "reason": request.reason,
            "requester": request.requester,
            "observed_plan_revision": request.observed_plan_revision,
            "source_revisions": dict(request.source_revisions),
            "source_sequence": event.sequence,
            "correlation_id": request.request_id,
            "parent_id": event.event_id,
            "status": "requested",
            "snapshot_id": None,
        }
        return (
            _artifact(
                request.mission_id,
                event.sequence,
                request.request_id,
                "transport-replan",
                record,
            ),
        )
    return ()


def _load_transport_events(
    transport_root: Path, mission_id: str | None
) -> list[_PublicArtifact]:
    artifacts: list[_PublicArtifact] = []
    for topic_dir in _safe_dirs(transport_root / "topics"):
        for mission_dir in _mission_dirs(
            topic_dir / "missions", mission_id, encoded=True
        ):
            for path in _safe_json_files(mission_dir):
                prefix = path.name.split("-", 1)[0]
                raw = _read_mapping(path, root=transport_root)
                if raw is None or not prefix.isdigit():
                    continue
                try:
                    event = TransportEvent.from_dict(raw)
                    if (
                        _encoded(event.mission_id) != mission_dir.name
                        or event.sequence != int(prefix)
                        or (mission_id is not None and event.mission_id != mission_id)
                    ):
                        continue
                    typed = _typed_event_artifacts(event)
                except (KeyError, TypeError, ValueError):
                    continue
                artifacts.append(
                    _artifact(
                        event.mission_id,
                        event.sequence,
                        event.event_id,
                        "transport-event",
                        event.to_dict(),
                    )
                )
                artifacts.extend(typed)
    return artifacts


def _load_commands(
    transport_root: Path, mission_id: str | None
) -> list[_PublicArtifact]:
    artifacts: list[_PublicArtifact] = []
    seen_receipts: set[str] = set()
    commands: dict[str, Command] = {}
    pending_outcomes: list[tuple[int, CommandOutcome]] = []
    for service_dir in _safe_dirs(transport_root / "commands"):
        for mission_dir in _mission_dirs(service_dir, mission_id, encoded=True):
            for path in _safe_json_files(mission_dir):
                prefix = path.name.split("-", 1)[0]
                envelope = _read_mapping(path, root=transport_root)
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
                            _encoded(command.mission_id) != mission_dir.name
                            or _encoded(command.target_service) != service_dir.name
                            or envelope.get("command_kind") != command.command_kind
                            or (mission_id is not None and command.mission_id != mission_id)
                        ):
                            continue
                        commands[command.command_id] = command
                        artifacts.append(
                            _artifact(
                                command.mission_id,
                                sequence,
                                command.command_id,
                                "command",
                                command.to_dict(),
                            )
                        )
                    elif envelope.get("kind") == "outcome":
                        raw = envelope.get("outcome")
                        if not isinstance(raw, Mapping):
                            continue
                        outcome = CommandOutcome.from_dict(raw)
                        if (
                            _encoded(outcome.mission_id) != mission_dir.name
                            or (mission_id is not None and outcome.mission_id != mission_id)
                        ):
                            continue
                        pending_outcomes.append((sequence, outcome))
                except (KeyError, TypeError, ValueError):
                    continue

    for command_id, command in sorted(commands.items()):
        receipt_path = transport_root / "receipts" / f"{_encoded(command_id)}.json"
        raw = _read_mapping(receipt_path, root=transport_root)
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
        artifacts.append(
            _artifact(
                receipt.mission_id,
                0,
                receipt.command_id,
                "receipt",
                receipt.to_dict(),
            )
        )

    for sequence, outcome in pending_outcomes:
        command = commands.get(outcome.command_id)
        if command is None or command.correlation_id != outcome.correlation_id:
            continue
        artifacts.append(
            _artifact(
                outcome.mission_id,
                sequence,
                outcome.command_id,
                "outcome",
                outcome.to_dict(),
            )
        )
    return artifacts


def _load_mission_tree(
    root: Path,
    mission_id: str | None,
    loader: Callable[[str, Path, Path], _PublicArtifact | None],
    *,
    events: bool = False,
    public_root: Path | None = None,
) -> list[_PublicArtifact]:
    artifacts: list[_PublicArtifact] = []
    for mission_dir in _mission_dirs(root, mission_id, encoded=False):
        artifact_dir = mission_dir / "events" if events else mission_dir
        for path in _safe_json_files(artifact_dir):
            artifact = loader(mission_dir.name, path, public_root or root)
            if artifact is not None:
                artifacts.append(artifact)
    return artifacts


def _load_log(
    mission_dir_name: str, path: Path, public_root: Path
) -> _PublicArtifact | None:
    raw = _read_mapping(path, root=public_root)
    if raw is None or not path.stem.isdigit():
        return None
    try:
        record = OperationalLogRecord.from_dict(raw)
        if record.mission_id != mission_dir_name or record.sequence != int(path.stem):
            return None
        return _artifact(
            record.mission_id,
            record.sequence,
            record.record_id,
            "operational-log",
            record.to_dict(),
            _aware_timestamp(record.event_time),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_summary(
    mission_dir_name: str, path: Path, public_root: Path
) -> _PublicArtifact | None:
    raw = _read_mapping(path, root=public_root)
    if raw is None or not path.stem.isdigit():
        return None
    try:
        summary = SummaryArtifact.from_dict(raw)
        if summary.mission_id != mission_dir_name or summary.sequence != int(path.stem):
            return None
        return _artifact(
            summary.mission_id,
            summary.sequence,
            summary.summary_id,
            "summary",
            summary.to_dict(),
            _aware_timestamp(summary.created_at),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_fsm(
    mission_dir_name: str, path: Path, public_root: Path
) -> _PublicArtifact | None:
    if path.name not in {"statechart.json", "execution-record.json"}:
        return None
    raw = _read_mapping(path, root=public_root)
    if raw is None:
        return None
    try:
        if path.name == "statechart.json":
            chart = Statechart.from_dict(raw)
            if chart.mission_id != mission_dir_name:
                return None
            return _artifact(
                chart.mission_id,
                chart.plan_revision,
                f"statechart:{chart.mission_id}:{chart.plan_revision}",
                "statechart",
                chart.to_dict(),
            )
        execution = FSMExecutionRecord.from_dict(raw)
        if execution.mission_id != mission_dir_name:
            return None
        return _artifact(
            execution.mission_id,
            execution.record_revision,
            f"fsm-execution:{execution.mission_id}:{execution.record_revision}",
            "fsm-execution",
            execution.to_dict(),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_public_artifacts(
    config: RuntimeConfig, mission_id: str | None = None
) -> tuple[_PublicArtifact, ...]:
    artifacts: list[_PublicArtifact] = []
    if config.transport.backend == "file":
        artifacts.extend(_load_transport_events(config.transport.root, mission_id))
        artifacts.extend(_load_commands(config.transport.root, mission_id))
    storage_root = config.storage.root
    artifacts.extend(
        _load_mission_tree(
            storage_root / "operational-log",
            mission_id,
            _load_log,
            events=True,
            public_root=storage_root,
        )
    )
    artifacts.extend(
        _load_mission_tree(
            storage_root / "summaries",
            mission_id,
            _load_summary,
            public_root=storage_root,
        )
    )
    artifacts.extend(
        _load_mission_tree(
            storage_root / "fsm",
            mission_id,
            _load_fsm,
            public_root=storage_root,
        )
    )
    artifacts.extend(_load_current_beliefs(storage_root, mission_id))
    return tuple(
        sorted(
            artifacts,
            key=lambda item: (
                item.mission_id,
                _SOURCE_ORDER[item.source_kind],
                item.sequence,
                item.identifier,
                json.dumps(item.record, sort_keys=True, separators=(",", ":")),
            ),
        )
    )


def _load_current_beliefs(
    storage_root: Path, mission_id: str | None
) -> list[_PublicArtifact]:
    """Project only snapshots accepted by the confined committed-state loader."""

    missions: list[str] = []
    if mission_id is not None:
        missions.append(mission_id)
    else:
        for mission_dir in _safe_dirs(storage_root / "bayesian-beliefs"):
            try:
                candidate = unquote(mission_dir.name, errors="strict")
            except UnicodeError:
                continue
            if _encoded(candidate) == mission_dir.name and _valid_mission_id(candidate):
                missions.append(candidate)

    try:
        store = FileBayesianBeliefStore(storage_root)
    except Exception:
        return []
    artifacts: list[_PublicArtifact] = []
    for selected in sorted(set(missions)):
        try:
            snapshot = store.load_current_read_only(selected)
            if snapshot is None:
                continue
            reference = belief_artifact_reference(
                snapshot.mission_id, snapshot.content_sha256
            )
        except Exception:
            continue
        event_id = (
            f"bayesian-belief:{snapshot.mission_id}:{snapshot.belief_revision}"
        )
        record = {
            "schema_version": 1,
            "event_id": event_id,
            "mission_id": snapshot.mission_id,
            "sequence": snapshot.belief_revision,
            "event_kind": "bayesian-belief",
            "payload": {
                "source": "bayesian_belief_snapshot",
                "revision": snapshot.belief_revision,
                "reference": reference,
                "content_sha256": snapshot.content_sha256,
                "input_event_id": snapshot.input_event_id,
                "input_revision": snapshot.input_revision,
                "marginals": [item.to_dict() for item in snapshot.marginals],
            },
        }
        artifacts.append(
            _artifact(
                snapshot.mission_id,
                snapshot.belief_revision,
                event_id,
                "bayesian-belief",
                record,
                _aware_timestamp(snapshot.created_at),
            )
        )
    return artifacts


def _valid_mission_id(value: str | None) -> bool:
    return value is not None and _MISSION_ID.fullmatch(value) is not None


def _validate_host(host: str) -> str:
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("viewer host must be 127.0.0.1, ::1, or localhost")
    return "127.0.0.1" if host == "localhost" else host


def _loopback_authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _authority_variants(host: str, port: int) -> frozenset[str]:
    authority = _loopback_authority(host, port)
    if port != 80:
        return frozenset({authority})
    host_only = f"[{host}]" if ":" in host else host
    return frozenset({host_only, authority})


def _same_loopback_origin(value: str, host: str, port: int) -> bool:
    try:
        parsed = urlsplit(value)
        selected_port = parsed.port if parsed.port is not None else 80
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == host
        and selected_port == port
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


class ViewerApplication:
    """Resolve runtime state and public viewer responses without exposing config."""

    def __init__(
        self,
        *,
        repo_root: Path,
        config_path: Path | None = None,
        static_root: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.config_path = Path(config_path) if config_path is not None else None
        self.static_root = (
            Path(static_root) if static_root is not None else Path(__file__).with_name("web")
        )
        self._projection = TraceProjection()

    def _runtime(self) -> _RuntimeView | None:
        try:
            config = load_runtime_config(self.config_path, repo_root=self.repo_root)
            store = RuntimeLeaseStore(config.storage.root / "runtime")
            lease = store.inspect()
        except Exception:
            return None
        if lease is None:
            return None
        return _RuntimeView(config, store, lease)

    @staticmethod
    def _current_lease(runtime: _RuntimeView) -> RuntimeLease | None:
        lease = runtime.store.inspect()
        if lease is None or (
            lease.session_id,
            lease.started_at,
        ) != (
            runtime.lease.session_id,
            runtime.lease.started_at,
        ):
            return None
        return lease

    def runtime_payload(self) -> dict[str, object]:
        runtime = self._runtime()
        if runtime is None:
            return {"active": False, "available": False, "status": "unavailable"}
        artifacts = _load_public_artifacts(runtime.config)
        lease = self._current_lease(runtime)
        if lease is None:
            return {"active": False, "available": False, "status": "unavailable"}
        mission_ids = sorted({artifact.mission_id for artifact in artifacts})
        return {
            "active": lease.status == "active",
            "available": True,
            "status": lease.status,
            "started_at": lease.started_at,
            "last_seen": lease.last_seen,
            "mission_ids": mission_ids,
        }

    def trace_payload(self, mission_id: str | None) -> dict[str, object]:
        if not _valid_mission_id(mission_id):
            return {"items": []}
        runtime = self._runtime()
        if runtime is None:
            return {"items": []}
        selected = cast(str, mission_id)
        artifacts = _load_public_artifacts(runtime.config, selected)
        observations = [
            {
                "schema_version": 1,
                "observation_sequence": sequence,
                "observed_at": artifact.observed_at,
                "record": artifact.record,
            }
            for sequence, artifact in enumerate(artifacts, 1)
        ]
        parent_ids: dict[str, str] = {}
        for artifact in artifacts:
            if artifact.source_kind != "transport-event":
                continue
            event_id = artifact.record.get("event_id")
            payload = artifact.record.get("payload")
            if not isinstance(event_id, str) or not isinstance(payload, Mapping):
                continue
            safe_parent, _ = sanitize_payload({"parent_id": payload.get("parent_id")})
            parent_id = safe_parent.get("parent_id")
            if isinstance(parent_id, str) and parent_id:
                parent_ids[event_id] = parent_id
        items = self._projection.project(observations)
        projected = []
        for item in items:
            public = item.to_dict()
            if item.event_id in parent_ids:
                public["parent_id"] = parent_ids[item.event_id]
            projected.append(public)
        selected_items = [
            item for item in projected if item["mission_id"] == selected
        ]
        if self._current_lease(runtime) is None:
            return {"items": []}
        return {"items": selected_items}

    def debug_payload(
        self, mission_id: str | None, role: str | None = None
    ) -> dict[str, object]:
        empty: dict[str, object] = {
            "enabled": False,
            "profiles": [],
            "invocations": [],
            "conversations": [],
        }
        if not _valid_mission_id(mission_id) or (
            role is not None and role not in KNOWN_DEBUG_ROLES
        ):
            return empty
        runtime = self._runtime()
        if runtime is None or not runtime.config.debug:
            return empty
        try:
            profiles, invocations = load_debug_artifacts(
                runtime.config.storage.root, cast(str, mission_id), role=role
            )
            conversations = load_llm_conversations(
                runtime.config.storage.root, cast(str, mission_id), role=role
            )
        except Exception:
            return empty
        if self._current_lease(runtime) is None:
            return empty
        return {
            "enabled": True,
            "profiles": profiles,
            "invocations": invocations,
            "conversations": conversations,
        }

    def static_file(self, request_path: str) -> Path | None:
        try:
            decoded = unquote(request_path, errors="strict")
        except UnicodeError:
            return None
        if "\\" in decoded or "\x00" in decoded:
            return None
        if any(component in {".", ".."} for component in decoded.split("/")):
            return None
        relative = "index.html" if decoded == "/" else decoded.lstrip("/")
        if not relative or PurePosixPath(relative).is_absolute():
            return None
        try:
            root = self.static_root.resolve()
            candidate = (root / relative).resolve()
            candidate.relative_to(root)
            if not candidate.is_file():
                return None
        except (OSError, ValueError):
            return None
        return candidate


class ViewerRequestHandler(BaseHTTPRequestHandler):
    """Serve the viewer UI and its read-only JSON resources."""

    server_version = "ONRViewer/1"

    @property
    def application(self) -> ViewerApplication:
        return cast("ViewerHTTPServer", self.server).application

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_CONNECT = do_POST
    do_TRACE = do_POST

    def _serve(self, *, head_only: bool) -> None:
        if not self._request_boundary_valid():
            self._send_error(HTTPStatus.FORBIDDEN, head_only=head_only)
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/runtime":
            self._send_json(self.application.runtime_payload(), head_only=head_only)
            return
        if parsed.path == "/api/trace":
            query = parse_qs(parsed.query, keep_blank_values=True)
            mission_values = query.get("mission_id", []) if set(query) <= {"mission_id"} else []
            mission_id = mission_values[0] if len(mission_values) == 1 else None
            self._send_json(
                self.application.trace_payload(mission_id), head_only=head_only
            )
            return
        if parsed.path == "/api/debug":
            query = parse_qs(parsed.query, keep_blank_values=True)
            valid_keys = set(query) <= {"mission_id", "role"}
            mission_values = query.get("mission_id", []) if valid_keys else []
            role_values = query.get("role", []) if valid_keys else []
            mission_id = mission_values[0] if len(mission_values) == 1 else None
            role = role_values[0] if len(role_values) == 1 else None
            if len(role_values) > 1 or (
                role is not None and role not in KNOWN_DEBUG_ROLES
            ):
                mission_id = None
            self._send_json(
                self.application.debug_payload(mission_id, role), head_only=head_only
            )
            return
        path = self.application.static_file(parsed.path)
        if path is None:
            self._send_error(HTTPStatus.NOT_FOUND, head_only=head_only)
            return
        try:
            content = path.read_bytes()
        except OSError:
            self._send_error(HTTPStatus.NOT_FOUND, head_only=head_only)
            return
        content_type, _ = mimetypes.guess_type(path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header(
            "Cache-Control",
            "no-cache" if path.suffix.lower() == ".html" else "public, max-age=3600",
        )
        self._security_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(content)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CSP)

    def _request_boundary_valid(self) -> bool:
        viewer_server = cast("ViewerHTTPServer", self.server)
        hosts = self.headers.get_all("Host", failobj=[])
        if len(hosts) != 1 or hosts[0] not in viewer_server.allowed_authorities:
            return False
        origins = self.headers.get_all("Origin", failobj=[])
        return not origins or (
            len(origins) == 1
            and any(
                _same_loopback_origin(
                    origins[0], host, viewer_server.listener_port
                )
                for host in viewer_server.allowed_hosts
            )
        )

    def _send_json(
        self,
        payload: Mapping[str, object],
        *,
        status: HTTPStatus = HTTPStatus.OK,
        head_only: bool = False,
    ) -> None:
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(content)

    def _send_error(self, status: HTTPStatus, *, head_only: bool = False) -> None:
        self._send_json(
            {"error": status.phrase.lower().replace(" ", "_")},
            status=status,
            head_only=head_only,
        )

    def _method_not_allowed(self) -> None:
        if not self._request_boundary_valid():
            self._send_error(HTTPStatus.FORBIDDEN)
            return
        content = b'{"error":"method_not_allowed"}'
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(content)


class ViewerHTTPServer(ThreadingHTTPServer):
    """Threaded loopback-only server carrying immutable viewer configuration."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        application: ViewerApplication,
        *,
        requested_host: str,
    ) -> None:
        self.application = application
        self.address_family = socket.AF_INET6 if address[0] == "::1" else socket.AF_INET
        super().__init__(address, ViewerRequestHandler)
        bound = cast(tuple[object, ...], self.server_address)
        self.listener_host = str(bound[0])
        self.listener_port = cast(int, bound[1])
        self.listener_authority = _loopback_authority(
            self.listener_host, self.listener_port
        )
        hosts = {self.listener_host}
        if requested_host == "localhost":
            hosts.add("localhost")
        self.allowed_hosts = frozenset(hosts)
        self.allowed_authorities = frozenset(
            authority
            for host in self.allowed_hosts
            for authority in _authority_variants(host, self.listener_port)
        )


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 14398,
    repo_root: Path | str = Path.cwd(),
    config_path: Path | str | None = None,
    static_root: Path | str | None = None,
) -> ViewerHTTPServer:
    """Create a configured loopback server without starting its serving loop."""

    bind_host = _validate_host(host)
    application = ViewerApplication(
        repo_root=Path(repo_root),
        config_path=Path(config_path) if config_path is not None else None,
        static_root=Path(static_root) if static_root is not None else None,
    )
    return ViewerHTTPServer(
        (bind_host, port), application, requested_host=host
    )


def _host_argument(value: str) -> str:
    try:
        _validate_host(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local read-only ONR trace viewer")
    parser.add_argument("--host", type=_host_argument, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14398)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config-path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    server = create_server(
        host=args.host,
        port=args.port,
        repo_root=args.repo_root,
        config_path=args.config_path,
    )
    try:
        print(f"ONR viewer listening on http://{args.host}:{server.server_port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = ["ViewerApplication", "ViewerHTTPServer", "create_server", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
