"""Recoverable, confined filesystem storage for Bayesian belief state."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any

from onr.application.bayesian_belief import (
    BayesianBeliefCheckpoint,
    belief_artifact_reference,
    canonical_mission_component,
)
from onr.contracts.bayesian_belief import BayesianBeliefSnapshot


class BayesianBeliefStoreError(RuntimeError):
    """A persisted belief record is malformed, inconsistent, or unconfined."""


@dataclass(frozen=True, slots=True)
class _CommittedState:
    generation: int
    snapshot: BayesianBeliefSnapshot | None
    checkpoint: BayesianBeliefCheckpoint
    pending: Mapping[str, Any] | None


def _mission_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("mission ID must be a non-empty string")
    if value != value.strip() or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("mission ID must be one path component")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FileBayesianBeliefStore:
    """Use one atomic committed-state pointer over bounded immutable generations."""

    def __init__(
        self,
        storage_root: Path,
        *,
        history_limit: int = 3,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) or history_limit < 1:
            raise ValueError("belief history limit must be a positive integer")
        requested = Path(storage_root).absolute()
        current = Path(requested.anchor)
        for part in requested.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise BayesianBeliefStoreError(
                    "belief storage root path must not contain a symlink"
                )
        self.storage_root = requested.resolve(strict=False)
        self.history_limit = history_limit
        self._fault_injector = fault_injector
        self._lock = RLock()

    def _relative_root(self, mission_id: str) -> PurePosixPath:
        mission = _mission_id(mission_id)
        return PurePosixPath("bayesian-beliefs") / canonical_mission_component(mission)

    def _path(self, relative: PurePosixPath, *, directory: bool = False) -> Path:
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise BayesianBeliefStoreError("belief storage path is not confined")
        candidate = self.storage_root.joinpath(*relative.parts)
        self._reject_symlink_components(candidate)
        try:
            candidate.resolve(strict=False).relative_to(self.storage_root)
        except (OSError, ValueError) as exc:
            raise BayesianBeliefStoreError("belief storage path escapes configured root") from exc
        if directory and candidate.exists() and not candidate.is_dir():
            raise BayesianBeliefStoreError("belief storage directory path is not a directory")
        return candidate

    def _reject_symlink_components(self, candidate: Path) -> None:
        try:
            relative = candidate.relative_to(self.storage_root)
        except ValueError as exc:
            raise BayesianBeliefStoreError("belief storage path escapes configured root") from exc
        current = self.storage_root
        if current.exists() and current.is_symlink():
            raise BayesianBeliefStoreError("belief storage root must not be a symlink")
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise BayesianBeliefStoreError("belief storage path contains a symlink")

    def _ensure_directory(self, relative: PurePosixPath) -> Path:
        directory = self._path(relative, directory=True)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_components(directory)
        current = self.storage_root
        for part in relative.parts:
            current = current / part
            if current.exists():
                if current.is_symlink() or not current.is_dir():
                    raise BayesianBeliefStoreError(
                        "belief storage directory contains an unsafe component"
                    )
            else:
                current.mkdir()
            self._reject_symlink_components(current)
        return directory

    def _atomic_write(self, relative: PurePosixPath, content: str, boundary: str) -> Path:
        path = self._path(relative)
        parent_relative = PurePosixPath(*relative.parts[:-1])
        self._ensure_directory(parent_relative)
        self._reject_symlink_components(path)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.tmp-",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(content)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._reject_symlink_components(path)
            os.replace(temporary_name, path)
            temporary_name = None
            self._fsync_directory(path.parent)
            if self._fault_injector is not None:
                self._fault_injector(boundary)
            return path
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    def mission_root(self, mission_id: str) -> Path:
        return self._path(self._relative_root(mission_id), directory=True)

    def current_path(self, mission_id: str) -> Path:
        return self._path(self._relative_root(mission_id) / "committed-state-v1.json")

    def _generation_relative(self, mission_id: str, generation: int) -> PurePosixPath:
        return self._relative_root(mission_id) / "generations" / f"{generation:020d}"

    def _read_json(self, relative: PurePosixPath, label: str) -> object:
        path = self._path(relative)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BayesianBeliefStoreError(f"{label} is corrupt: {path}") from exc

    def _load_state(
        self, mission_id: str, *, prune: bool = True
    ) -> _CommittedState | None:
        mission = _mission_id(mission_id)
        pointer_relative = self._relative_root(mission) / "committed-state-v1.json"
        pointer_path = self._path(pointer_relative)
        if not pointer_path.exists():
            return None
        value = self._read_json(pointer_relative, "Bayesian belief committed state")
        fields = {
            "schema_version",
            "mission_id",
            "generation",
            "belief_revision",
            "snapshot_sha256",
            "snapshot_path",
            "checkpoint_sha256",
            "pending_sha256",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise BayesianBeliefStoreError(
                "Bayesian belief committed state is corrupt: malformed fields"
            )
        generation = value.get("generation")
        belief_revision = value.get("belief_revision")
        if (
            value.get("schema_version") != 1
            or value.get("mission_id") != mission
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or isinstance(belief_revision, bool)
            or not isinstance(belief_revision, int)
            or belief_revision < 0
        ):
            raise BayesianBeliefStoreError("Bayesian belief committed state identity is invalid")
        generation_relative = self._generation_relative(mission, generation)
        checkpoint_value = self._read_json(
            generation_relative / "checkpoint-v1.json", "Bayesian belief checkpoint"
        )
        try:
            checkpoint = BayesianBeliefCheckpoint.from_dict(checkpoint_value)
        except ValueError as exc:
            raise BayesianBeliefStoreError("Bayesian belief checkpoint is corrupt") from exc
        checkpoint_hash = value.get("checkpoint_sha256")
        if (
            checkpoint.mission_id != mission
            or checkpoint.belief_revision != belief_revision
            or checkpoint.content_sha256 != checkpoint_hash
        ):
            raise BayesianBeliefStoreError("committed checkpoint binding is inconsistent")

        snapshot_hash = value.get("snapshot_sha256")
        snapshot_path_value = value.get("snapshot_path")
        snapshot: BayesianBeliefSnapshot | None = None
        snapshot_relative = generation_relative / "belief-v1.json"
        if snapshot_hash is not None:
            if not isinstance(snapshot_hash, str):
                raise BayesianBeliefStoreError("committed snapshot hash is invalid")
            expected_reference = belief_artifact_reference(mission, snapshot_hash)
            expected_path = expected_reference.partition("#sha256=")[0]
            if snapshot_path_value != expected_path:
                raise BayesianBeliefStoreError(
                    "committed snapshot path binding is inconsistent"
                )
            external_relative = PurePosixPath(expected_path)
            snapshot_value = self._read_json(
                external_relative, "Bayesian belief committed artifact"
            )
            try:
                snapshot = BayesianBeliefSnapshot.from_dict(snapshot_value)
            except ValueError as exc:
                raise BayesianBeliefStoreError("Bayesian belief artifact is corrupt") from exc
            if (
                snapshot.mission_id != mission
                or snapshot.belief_revision != belief_revision
                or snapshot.content_sha256 != snapshot_hash
            ):
                raise BayesianBeliefStoreError("committed snapshot binding is inconsistent")
            generation_value = self._read_json(
                snapshot_relative, "Bayesian belief generation artifact"
            )
            if generation_value != snapshot.to_dict():
                raise BayesianBeliefStoreError(
                    "generation and committed artifact content differ"
                )
        elif belief_revision != 0:
            raise BayesianBeliefStoreError("committed belief revision has no snapshot")
        elif snapshot_path_value is not None:
            raise BayesianBeliefStoreError("empty belief state has a snapshot path")

        pending_hash = value.get("pending_sha256")
        pending: Mapping[str, Any] | None = None
        if pending_hash is not None:
            pending_value = self._read_json(
                generation_relative / "pending-output-v1.json",
                "Bayesian belief pending output",
            )
            if not isinstance(pending_value, dict):
                raise BayesianBeliefStoreError("Bayesian belief pending output is malformed")
            if _sha256_text(_canonical_json(pending_value)) != pending_hash:
                raise BayesianBeliefStoreError("committed pending-output binding is inconsistent")
            pending = self._validate_pending(mission, snapshot, pending_value)
            if snapshot is None:
                raise BayesianBeliefStoreError("pending output has no committed snapshot")
            try:
                self._validate_update(mission, snapshot, checkpoint)
            except (TypeError, ValueError) as exc:
                raise BayesianBeliefStoreError(
                    "committed pending generation is internally inconsistent"
                ) from exc
        state = _CommittedState(generation, snapshot, checkpoint, pending)
        if prune:
            self._prune_generations(mission, generation)
        return state

    @staticmethod
    def _validate_pending(
        mission: str,
        snapshot: BayesianBeliefSnapshot | None,
        value: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        fields = {
            "schema_version",
            "topic",
            "event_id",
            "mission_id",
            "event_kind",
            "payload",
        }
        if set(value) != fields or value.get("schema_version") != 1:
            raise BayesianBeliefStoreError("pending output has unknown or missing fields")
        topic = value.get("topic")
        event_id = value.get("event_id")
        payload = value.get("payload")
        if (
            snapshot is None
            or not isinstance(topic, str)
            or not topic.strip()
            or not isinstance(event_id, str)
            or not event_id.strip()
            or value.get("mission_id") != mission
            or value.get("event_kind") != "belief.updated"
            or not isinstance(payload, dict)
            or set(payload)
            != {
                "source",
                "revision",
                "reference",
                "content_sha256",
                "health",
                "fresh",
            }
        ):
            raise BayesianBeliefStoreError("pending belief output is malformed")
        if (
            payload.get("source") != "bayesian_belief_snapshot"
            or payload.get("revision") != snapshot.belief_revision
            or payload.get("content_sha256") != snapshot.content_sha256
            or payload.get("reference")
            != belief_artifact_reference(mission, snapshot.content_sha256)
        ):
            raise BayesianBeliefStoreError("pending output does not bind committed belief state")
        return dict(value)

    def load_current(self, mission_id: str) -> BayesianBeliefSnapshot | None:
        state = self._load_state(mission_id)
        return None if state is None else state.snapshot

    def load_current_read_only(
        self, mission_id: str
    ) -> BayesianBeliefSnapshot | None:
        """Validate and return current state without pruning filesystem history."""

        state = self._load_state(mission_id, prune=False)
        return None if state is None else state.snapshot

    def load_checkpoint(self, mission_id: str) -> BayesianBeliefCheckpoint | None:
        state = self._load_state(mission_id)
        return None if state is None else state.checkpoint

    def load_pending_output(self, mission_id: str) -> Mapping[str, Any] | None:
        state = self._load_state(mission_id)
        return None if state is None or state.pending is None else dict(state.pending)

    @staticmethod
    def _validate_update(
        mission: str,
        snapshot: BayesianBeliefSnapshot,
        checkpoint: BayesianBeliefCheckpoint,
    ) -> None:
        if not isinstance(snapshot, BayesianBeliefSnapshot):
            raise TypeError("snapshot must be a BayesianBeliefSnapshot")
        if not isinstance(checkpoint, BayesianBeliefCheckpoint):
            raise TypeError("checkpoint must be a BayesianBeliefCheckpoint")
        if checkpoint.mission_id != mission or checkpoint.belief_revision != snapshot.belief_revision:
            raise ValueError("snapshot and checkpoint identity must match")
        if (
            checkpoint.last_input_event_id != snapshot.input_event_id
            or checkpoint.last_input_revision != snapshot.input_revision
        ):
            raise ValueError("snapshot and checkpoint input provenance must match")
        if tuple(item.key for item in snapshot.marginals) != checkpoint.keys:
            raise ValueError("snapshot and checkpoint belief keys must match")
        for index, marginal in enumerate(snapshot.marginals):
            probability = (
                math.fsum(1.0 for particle in checkpoint.particles if particle[index])
                / len(checkpoint.particles)
            )
            if marginal.probability_risk != probability:
                raise ValueError("snapshot marginals must match checkpoint particles")

    def _commit_generation(
        self,
        mission: str,
        snapshot: BayesianBeliefSnapshot | None,
        checkpoint: BayesianBeliefCheckpoint,
        pending: Mapping[str, Any] | None,
    ) -> Path:
        previous = self._load_state(mission)
        generation = 1 if previous is None else previous.generation + 1
        generation_relative = self._generation_relative(mission, generation)
        generation_path = self._path(generation_relative, directory=True)
        if generation_path.exists():
            self._remove_partial_generation(generation_relative)
        if snapshot is not None:
            snapshot_document = snapshot.to_canonical_json()
            self._atomic_write(
                generation_relative / "belief-v1.json",
                snapshot_document,
                "snapshot",
            )
            reference_path = belief_artifact_reference(
                mission, snapshot.content_sha256
            ).partition("#sha256=")[0]
            artifact_relative = PurePosixPath(reference_path)
            artifact_path = self._path(artifact_relative)
            if artifact_path.exists():
                existing = self._read_json(
                    artifact_relative, "Bayesian belief committed artifact"
                )
                if existing != snapshot.to_dict():
                    raise BayesianBeliefStoreError(
                        "immutable committed artifact content conflicts"
                    )
            else:
                self._atomic_write(
                    artifact_relative,
                    snapshot_document,
                    "artifact",
                )
        self._atomic_write(
            generation_relative / "checkpoint-v1.json",
            checkpoint.to_canonical_json(),
            "checkpoint",
        )
        pending_hash: str | None = None
        if pending is not None:
            pending_document = _canonical_json(pending)
            pending_hash = _sha256_text(pending_document)
            self._atomic_write(
                generation_relative / "pending-output-v1.json",
                pending_document,
                "pending",
            )
        pointer = {
            "schema_version": 1,
            "mission_id": mission,
            "generation": generation,
            "belief_revision": checkpoint.belief_revision,
            "snapshot_sha256": snapshot.content_sha256 if snapshot is not None else None,
            "snapshot_path": (
                belief_artifact_reference(mission, snapshot.content_sha256).partition(
                    "#sha256="
                )[0]
                if snapshot is not None
                else None
            ),
            "checkpoint_sha256": checkpoint.content_sha256,
            "pending_sha256": pending_hash,
        }
        pointer_path = self._atomic_write(
            self._relative_root(mission) / "committed-state-v1.json",
            _canonical_json(pointer),
            "commit",
        )
        self._prune_generations(mission, generation)
        return pointer_path

    def _remove_partial_generation(self, relative: PurePosixPath) -> None:
        path = self._path(relative, directory=True)
        if path.exists():
            self._reject_tree_symlinks(path)
            shutil.rmtree(path)

    def _reject_tree_symlinks(self, root: Path) -> None:
        self._reject_symlink_components(root)
        for directory, names, files in os.walk(root, followlinks=False):
            base = Path(directory)
            self._reject_symlink_components(base)
            for name in (*names, *files):
                child = base / name
                if child.is_symlink():
                    raise BayesianBeliefStoreError(
                        "belief storage generation contains a symlink"
                    )
                self._reject_symlink_components(child)

    def save_checkpoint(self, checkpoint: BayesianBeliefCheckpoint) -> Path:
        if not isinstance(checkpoint, BayesianBeliefCheckpoint):
            raise TypeError("checkpoint must be a BayesianBeliefCheckpoint")
        mission = _mission_id(checkpoint.mission_id)
        with self._lock:
            previous = self._load_state(mission)
            if previous is not None and previous.pending is not None:
                raise BayesianBeliefStoreError("pending belief output must be flushed first")
            snapshot = None if previous is None else previous.snapshot
            if snapshot is None and checkpoint.belief_revision != 0:
                raise ValueError("checkpoint revision requires a current belief artifact")
            if snapshot is not None and checkpoint.belief_revision != snapshot.belief_revision:
                raise ValueError("checkpoint revision must match current belief artifact")
            if previous is not None:
                prior_input = previous.checkpoint.last_input_revision
                next_input = checkpoint.last_input_revision
                if prior_input is not None and (next_input is None or next_input <= prior_input):
                    raise ValueError("checkpoint input revision must increase monotonically")
            return self._commit_generation(mission, snapshot, checkpoint, None)

    def save(
        self,
        snapshot: BayesianBeliefSnapshot,
        checkpoint: BayesianBeliefCheckpoint,
    ) -> Path:
        mission = _mission_id(snapshot.mission_id)
        self._validate_update(mission, snapshot, checkpoint)
        with self._lock:
            previous = self._load_state(mission)
            expected = 1 if previous is None or previous.snapshot is None else previous.snapshot.belief_revision + 1
            if snapshot.belief_revision != expected:
                raise ValueError("stored belief revisions must be contiguous and increasing")
            return self._commit_generation(mission, snapshot, checkpoint, None)

    def commit_update(
        self,
        snapshot: BayesianBeliefSnapshot,
        checkpoint: BayesianBeliefCheckpoint,
        *,
        pending_topic: str,
        pending_event: Mapping[str, Any],
    ) -> Path:
        """Commit snapshot, checkpoint, and a sequence-free output template."""

        mission = _mission_id(snapshot.mission_id)
        self._validate_update(mission, snapshot, checkpoint)
        if not isinstance(pending_topic, str) or not pending_topic.strip():
            raise ValueError("pending output topic must be a non-empty string")
        if not isinstance(pending_event, Mapping):
            raise TypeError("pending output must be a sequence-free event template")
        pending = dict(pending_event)
        pending["topic"] = pending_topic
        self._validate_pending(mission, snapshot, pending)
        with self._lock:
            previous = self._load_state(mission)
            if previous is not None and previous.pending is not None:
                raise BayesianBeliefStoreError("pending belief output must be flushed first")
            expected = 1 if previous is None or previous.snapshot is None else previous.snapshot.belief_revision + 1
            if snapshot.belief_revision != expected:
                raise ValueError("stored belief revisions must be contiguous and increasing")
            return self._commit_generation(mission, snapshot, checkpoint, pending)

    def clear_pending_output(self, mission_id: str, event_id: str) -> None:
        mission = _mission_id(mission_id)
        with self._lock:
            state = self._load_state(mission)
            if state is None or state.pending is None:
                return
            if state.pending.get("event_id") != event_id:
                raise BayesianBeliefStoreError("pending output event ID does not match")
            self._commit_generation(mission, state.snapshot, state.checkpoint, None)

    def load_reference(
        self, mission_id: str, reference: str, content_sha256: str
    ) -> BayesianBeliefSnapshot:
        mission = _mission_id(mission_id)
        expected = belief_artifact_reference(mission, content_sha256)
        if reference != expected:
            raise BayesianBeliefStoreError("belief artifact reference is not canonical")
        relative_text, separator, digest = reference.partition("#sha256=")
        if not separator or digest != content_sha256:
            raise BayesianBeliefStoreError("belief artifact reference hash is invalid")
        relative = PurePosixPath(relative_text)
        path = self._path(relative)
        if not path.is_file() or path.is_symlink():
            raise BayesianBeliefStoreError(
                "referenced committed belief artifact is not a confined file"
            )
        state = self._load_state(mission)
        if (
            state is None
            or state.snapshot is None
            or state.snapshot.content_sha256 != content_sha256
        ):
            raise BayesianBeliefStoreError("referenced committed belief state is unavailable")
        value = self._read_json(relative, "referenced Bayesian belief artifact")
        try:
            artifact = BayesianBeliefSnapshot.from_dict(value)
        except ValueError as exc:
            raise BayesianBeliefStoreError(
                "referenced Bayesian belief artifact is malformed"
            ) from exc
        if (
            artifact != state.snapshot
            or artifact.mission_id != mission
            or artifact.belief_revision != state.snapshot.belief_revision
            or artifact.content_sha256 != content_sha256
        ):
            raise BayesianBeliefStoreError(
                "referenced Bayesian belief artifact binding is invalid"
            )
        return artifact

    def load_revision(self, mission_id: str, revision: int) -> BayesianBeliefSnapshot | None:
        mission = _mission_id(mission_id)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("belief revision must be a positive integer")
        state = self._load_state(mission)
        if state is None:
            return None
        committed_name = f"{state.generation:020d}"
        generations = [
            path
            for path in self._generation_directories(mission)
            if path.name <= committed_name
        ]
        for directory in reversed(generations):
            relative = self._relative_root(mission) / "generations" / directory.name
            snapshot_path = self._path(relative / "belief-v1.json")
            if not snapshot_path.exists():
                continue
            value = self._read_json(relative / "belief-v1.json", "Bayesian belief artifact")
            try:
                snapshot = BayesianBeliefSnapshot.from_dict(value)
            except ValueError as exc:
                raise BayesianBeliefStoreError("Bayesian belief history is corrupt") from exc
            if snapshot.belief_revision == revision:
                return snapshot
        return None

    def _generation_directories(self, mission: str) -> list[Path]:
        relative = self._relative_root(mission) / "generations"
        root = self._path(relative, directory=True)
        if not root.exists():
            return []
        self._reject_symlink_components(root)
        directories: list[Path] = []
        for path in root.iterdir():
            self._reject_symlink_components(path)
            if path.is_dir() and path.name.isdigit():
                directories.append(path)
        return sorted(directories, key=lambda path: path.name)

    def _prune_generations(self, mission: str, committed_generation: int) -> None:
        directories = self._generation_directories(mission)
        committed_name = f"{committed_generation:020d}"
        committed = [path for path in directories if path.name <= committed_name]
        partial = [path for path in directories if path.name > committed_name]
        for path in partial:
            self._reject_tree_symlinks(path)
            shutil.rmtree(path)
        for path in committed[:-self.history_limit]:
            self._reject_tree_symlinks(path)
            shutil.rmtree(path)
        retained_hashes: set[str] = set()
        for path in committed[-self.history_limit :]:
            relative = (
                self._relative_root(mission)
                / "generations"
                / path.name
                / "belief-v1.json"
            )
            artifact_path = self._path(relative)
            if not artifact_path.exists():
                continue
            value = self._read_json(relative, "Bayesian belief generation artifact")
            try:
                retained_hashes.add(
                    BayesianBeliefSnapshot.from_dict(value).content_sha256
                )
            except ValueError as exc:
                raise BayesianBeliefStoreError(
                    "Bayesian belief generation history is corrupt"
                ) from exc
        content_root = self._path(
            self._relative_root(mission) / "generations" / "by-content",
            directory=True,
        )
        if content_root.exists():
            for path in content_root.iterdir():
                self._reject_tree_symlinks(path)
                if not path.is_dir():
                    raise BayesianBeliefStoreError(
                        "committed artifact content root contains a non-directory"
                    )
                if path.name not in retained_hashes:
                    shutil.rmtree(path)
        generations_root = self._path(
            self._relative_root(mission) / "generations", directory=True
        )
        if generations_root.exists():
            self._fsync_directory(generations_root)


__all__ = ["BayesianBeliefStoreError", "FileBayesianBeliefStore"]
