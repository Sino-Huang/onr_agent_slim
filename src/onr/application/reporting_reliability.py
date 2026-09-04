"""Deterministic quadrature for Mission 1 reporting reliability."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from onr.contracts.bayesian_belief import canonical_json, canonical_sha256
from onr.contracts.environment import EnvironmentTickResult
from onr.contracts.reporting_reliability import (
    ReportingReliabilitySnapshot,
    SharedOmissionReliability,
    ShipReportingReliability,
)
from onr.contracts.transport import TransportEvent


P_BIN_COUNT = 201
Q_BIN_COUNT = 101
HONEST_PRIOR_MASS = 0.75
P_ALPHA = 2.1918
P_BETA = 1.9307
OUTCOMES = ("clean", "altered", "omitted")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def reporting_reliability_reference(
    snapshot: ReportingReliabilitySnapshot,
) -> str:
    return (
        f"bayesian-beliefs/{quote(snapshot.mission_id, safe='._-')}/"
        f"reporting-reliability-{snapshot.content_sha256}.json"
        f"#sha256={snapshot.content_sha256}"
    )


def _normalise(values: Iterable[float]) -> tuple[float, ...]:
    selected = tuple(float(value) for value in values)
    total = math.fsum(selected)
    if total <= 0.0:
        raise ValueError("reporting reliability evidence has zero probability")
    return tuple(value / total for value in selected)


def _quantile(grid: Sequence[float], weights: Sequence[float], probability: float) -> float:
    cumulative = 0.0
    for value, weight in zip(grid, weights, strict=True):
        cumulative += weight
        if cumulative >= probability:
            return float(value)
    return float(grid[-1])


@dataclass(frozen=True, slots=True)
class ReportingReliabilityCheckpoint:
    schema_version: int
    mission_id: str
    belief_revision: int
    last_input_event_id: str
    last_input_revision: int
    p_grid: tuple[float, ...]
    q_grid: tuple[float, ...]
    q_weights: tuple[float, ...]
    ship_weights: Mapping[int, tuple[tuple[float, ...], ...]]
    outcome_counts: Mapping[int, Mapping[str, int]]
    processed_check_ids: tuple[str, ...]
    configuration: Mapping[str, object]
    content_sha256: str

    def content_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "belief_revision": self.belief_revision,
            "last_input_event_id": self.last_input_event_id,
            "last_input_revision": self.last_input_revision,
            "p_grid": list(self.p_grid),
            "q_grid": list(self.q_grid),
            "q_weights": list(self.q_weights),
            "ship_weights": {
                str(entity_id): [list(row) for row in rows]
                for entity_id, rows in sorted(self.ship_weights.items())
            },
            "outcome_counts": {
                str(entity_id): dict(counts)
                for entity_id, counts in sorted(self.outcome_counts.items())
            },
            "processed_check_ids": list(self.processed_check_ids),
            "evidence_cursor": len(self.processed_check_ids),
            "configuration": dict(self.configuration),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.content_dict(), "content_sha256": self.content_sha256}

    @classmethod
    def create(cls, **kwargs: Any) -> "ReportingReliabilityCheckpoint":
        temporary = cls(content_sha256="", **kwargs)
        return cls(**kwargs, content_sha256=canonical_sha256(temporary.content_dict()))

    @classmethod
    def from_dict(cls, value: object) -> "ReportingReliabilityCheckpoint":
        if not isinstance(value, Mapping):
            raise ValueError("reporting reliability checkpoint must be an object")
        checkpoint = cls(
            schema_version=value["schema_version"],
            mission_id=value["mission_id"],
            belief_revision=value["belief_revision"],
            last_input_event_id=value["last_input_event_id"],
            last_input_revision=value["last_input_revision"],
            p_grid=tuple(float(item) for item in value["p_grid"]),
            q_grid=tuple(float(item) for item in value["q_grid"]),
            q_weights=tuple(float(item) for item in value["q_weights"]),
            ship_weights={
                int(entity_id): tuple(tuple(float(item) for item in row) for row in rows)
                for entity_id, rows in value["ship_weights"].items()
            },
            outcome_counts={
                int(entity_id): dict(counts)
                for entity_id, counts in value["outcome_counts"].items()
            },
            processed_check_ids=tuple(value["processed_check_ids"]),
            configuration=dict(value["configuration"]),
            content_sha256=value["content_sha256"],
        )
        if value.get("evidence_cursor") != len(checkpoint.processed_check_ids):
            raise ValueError("checkpoint evidence cursor is inconsistent")
        if checkpoint.content_sha256 != canonical_sha256(checkpoint.content_dict()):
            raise ValueError("checkpoint content hash does not match")
        return checkpoint


class ReportingReliabilityManager:
    """Exact conditional-grid update for vessel corruption rates and shared q."""

    belief_kind = "reporting_reliability"

    def __init__(self, mission_id: str, entity_ids: Iterable[int]) -> None:
        entities = tuple(sorted(set(entity_ids)))
        if not mission_id.strip() or not entities or any(
            isinstance(entity_id, bool) or not isinstance(entity_id, int) or entity_id <= 0
            for entity_id in entities
        ):
            raise ValueError("reporting reliability requires a Mission and numeric ships")
        self.mission_id = mission_id
        self.p_grid = (0.0,) + tuple((index + 0.5) / P_BIN_COUNT for index in range(P_BIN_COUNT))
        continuous = _normalise(
            value ** (P_ALPHA - 1.0) * (1.0 - value) ** (P_BETA - 1.0)
            for value in self.p_grid[1:]
        )
        base = (HONEST_PRIOR_MASS,) + tuple((1.0 - HONEST_PRIOR_MASS) * value for value in continuous)
        self.q_grid = tuple((index + 0.5) / Q_BIN_COUNT for index in range(Q_BIN_COUNT))
        self.q_weights = tuple(1.0 / Q_BIN_COUNT for _ in self.q_grid)
        self.ship_weights = {
            entity_id: tuple(base for _ in self.q_grid) for entity_id in entities
        }
        self.outcome_counts = {
            entity_id: {outcome: 0 for outcome in OUTCOMES} for entity_id in entities
        }
        self.processed_check_ids: list[str] = []
        self.belief_revision = 1
        self.last_input_event_id = "reporting-reliability:initial"
        self.last_input_revision = 0

    @classmethod
    def from_checkpoint(
        cls, checkpoint: ReportingReliabilityCheckpoint
    ) -> "ReportingReliabilityManager":
        manager = cls(checkpoint.mission_id, checkpoint.ship_weights)
        manager.p_grid = checkpoint.p_grid
        manager.q_grid = checkpoint.q_grid
        manager.q_weights = checkpoint.q_weights
        manager.ship_weights = dict(checkpoint.ship_weights)
        manager.outcome_counts = {
            entity_id: dict(counts)
            for entity_id, counts in checkpoint.outcome_counts.items()
        }
        manager.processed_check_ids = list(checkpoint.processed_check_ids)
        manager.belief_revision = checkpoint.belief_revision
        manager.last_input_event_id = checkpoint.last_input_event_id
        manager.last_input_revision = checkpoint.last_input_revision
        return manager

    def update_checks(
        self,
        checks: Iterable[Mapping[str, object]],
        *,
        input_event_id: str,
        input_revision: int,
        created_at: str,
    ) -> ReportingReliabilitySnapshot | None:
        seen = set(self.processed_check_ids)
        new_checks = [check for check in checks if check.get("check_id") not in seen]
        if not new_checks:
            return None
        if input_revision < self.last_input_revision:
            raise ValueError("reporting reliability input revision moved backwards")
        for check in new_checks:
            self._update_one(check)
        self.belief_revision += 1
        self.last_input_event_id = input_event_id
        self.last_input_revision = input_revision
        return self.snapshot(
            input_event_id=input_event_id,
            input_revision=input_revision,
            created_at=created_at,
        )

    def _update_one(self, check: Mapping[str, object]) -> None:
        check_id = check.get("check_id")
        entity_id = check.get("entity_id")
        outcome = check.get("outcome")
        if not isinstance(check_id, str) or not check_id or check_id in self.processed_check_ids:
            raise ValueError("report check ID is invalid or duplicated")
        if entity_id not in self.ship_weights:
            raise ValueError("report check references an unknown numeric ship")
        if outcome not in OUTCOMES:
            raise ValueError("report check outcome is invalid")
        conditional = self.ship_weights[int(entity_id)]
        q_evidence: list[float] = []
        updated_rows: list[tuple[float, ...]] = []
        for q, row in zip(self.q_grid, conditional, strict=True):
            if outcome == "clean":
                likelihoods = tuple(1.0 - p for p in self.p_grid)
            elif outcome == "altered":
                likelihoods = tuple(p * (1.0 - q) for p in self.p_grid)
            else:
                likelihoods = tuple(p * q for p in self.p_grid)
            unnormalised = tuple(weight * likelihood for weight, likelihood in zip(row, likelihoods, strict=True))
            evidence = math.fsum(unnormalised)
            q_evidence.append(evidence)
            updated_rows.append(_normalise(unnormalised))
        self.q_weights = _normalise(
            weight * evidence
            for weight, evidence in zip(self.q_weights, q_evidence, strict=True)
        )
        self.ship_weights[int(entity_id)] = tuple(updated_rows)
        self.outcome_counts[int(entity_id)][str(outcome)] += 1
        self.processed_check_ids.append(check_id)

    def snapshot(
        self, *, input_event_id: str, input_revision: int, created_at: str
    ) -> ReportingReliabilitySnapshot:
        ships = []
        for entity_id, rows in sorted(self.ship_weights.items()):
            marginal = tuple(
                math.fsum(
                    q_weight * rows[q_index][p_index]
                    for q_index, q_weight in enumerate(self.q_weights)
                )
                for p_index in range(len(self.p_grid))
            )
            mean = math.fsum(value * weight for value, weight in zip(self.p_grid, marginal, strict=True))
            variance = math.fsum(weight * (value - mean) ** 2 for value, weight in zip(self.p_grid, marginal, strict=True))
            expected_omission = math.fsum(
                self.q_weights[q_index]
                * self.q_grid[q_index]
                * math.fsum(value * weight for value, weight in zip(self.p_grid, rows[q_index], strict=True))
                for q_index in range(len(self.q_grid))
            )
            expected_posterior_variance = 0.0
            for outcome in OUTCOMES:
                probability = first = second = 0.0
                for q_index, q in enumerate(self.q_grid):
                    for p, p_weight in zip(self.p_grid, rows[q_index], strict=True):
                        likelihood = (
                            1.0 - p
                            if outcome == "clean"
                            else p * (1.0 - q)
                            if outcome == "altered"
                            else p * q
                        )
                        joint = self.q_weights[q_index] * p_weight * likelihood
                        probability += joint
                        first += joint * p
                        second += joint * p * p
                if probability > 0.0:
                    posterior_mean = first / probability
                    posterior_variance = max(
                        0.0, second / probability - posterior_mean * posterior_mean
                    )
                    expected_posterior_variance += probability * posterior_variance
            ships.append(
                ShipReportingReliability(
                    entity_id=entity_id,
                    mean=mean,
                    variance=variance,
                    credible_interval=(
                        _quantile(self.p_grid, marginal, 0.025),
                        _quantile(self.p_grid, marginal, 0.975),
                    ),
                    honest_probability=marginal[0],
                    expected_omission_probability=expected_omission,
                    expected_variance_reduction=max(
                        0.0, variance - expected_posterior_variance
                    ),
                    outcome_counts=self.outcome_counts[entity_id],
                )
            )
        q_mean = math.fsum(value * weight for value, weight in zip(self.q_grid, self.q_weights, strict=True))
        q_variance = math.fsum(weight * (value - q_mean) ** 2 for value, weight in zip(self.q_grid, self.q_weights, strict=True))
        return ReportingReliabilitySnapshot.create(
            mission_id=self.mission_id,
            belief_revision=self.belief_revision,
            input_event_id=input_event_id,
            input_revision=input_revision,
            created_at=created_at,
            ships=ships,
            omission=SharedOmissionReliability(
                q_mean,
                q_variance,
                (
                    _quantile(self.q_grid, self.q_weights, 0.025),
                    _quantile(self.q_grid, self.q_weights, 0.975),
                ),
            ),
        )

    def checkpoint(self) -> ReportingReliabilityCheckpoint:
        return ReportingReliabilityCheckpoint.create(
            schema_version=1,
            mission_id=self.mission_id,
            belief_revision=self.belief_revision,
            last_input_event_id=self.last_input_event_id,
            last_input_revision=self.last_input_revision,
            p_grid=self.p_grid,
            q_grid=self.q_grid,
            q_weights=self.q_weights,
            ship_weights=self.ship_weights,
            outcome_counts=self.outcome_counts,
            processed_check_ids=tuple(self.processed_check_ids),
            configuration={
                "honest_prior_mass": HONEST_PRIOR_MASS,
                "p_alpha": P_ALPHA,
                "p_beta": P_BETA,
                "p_bins": P_BIN_COUNT,
                "q_alpha": 1.0,
                "q_beta": 1.0,
                "q_bins": Q_BIN_COUNT,
            },
        )


class FileReportingReliabilityStore:
    """Atomic current checkpoint plus immutable hash-addressed snapshots."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()

    def _mission_root(self, mission_id: str) -> Path:
        return self.root / "bayesian-beliefs" / quote(mission_id, safe="._-")

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = canonical_json(value) + "\n"
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                temporary = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def save(
        self,
        snapshot: ReportingReliabilitySnapshot,
        checkpoint: ReportingReliabilityCheckpoint,
        pending: Mapping[str, object] | None,
    ) -> None:
        mission_root = self._mission_root(snapshot.mission_id)
        artifact = mission_root / f"reporting-reliability-{snapshot.content_sha256}.json"
        if not artifact.exists():
            self._write(artifact, snapshot.to_dict())
        self._write(
            mission_root / "reporting-reliability-state-v1.json",
            {
                "schema_version": 1,
                "snapshot": snapshot.to_dict(),
                "checkpoint": checkpoint.to_dict(),
                "pending": None if pending is None else dict(pending),
            },
        )

    def load(
        self, mission_id: str
    ) -> tuple[ReportingReliabilitySnapshot, ReportingReliabilityCheckpoint, Mapping[str, object] | None] | None:
        path = self._mission_root(mission_id) / "reporting-reliability-state-v1.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        snapshot = ReportingReliabilitySnapshot.from_dict(value["snapshot"])
        checkpoint = ReportingReliabilityCheckpoint.from_dict(value["checkpoint"])
        if snapshot.mission_id != mission_id or checkpoint.mission_id != mission_id or snapshot.belief_revision != checkpoint.belief_revision:
            raise ValueError("stored reporting reliability state is inconsistent")
        return snapshot, checkpoint, value["pending"]

    def load_reference(self, mission_id: str, reference: str) -> ReportingReliabilitySnapshot:
        marker = "#sha256="
        if marker not in reference:
            raise ValueError("reporting reliability reference is not hash-addressed")
        relative, digest = reference.rsplit(marker, 1)
        expected = f"bayesian-beliefs/{quote(mission_id, safe='._-')}/reporting-reliability-{digest}.json"
        if relative != expected:
            raise ValueError("reporting reliability reference is not for this Mission")
        snapshot = ReportingReliabilitySnapshot.from_dict(
            json.loads((self.root / relative).read_text(encoding="utf-8"))
        )
        if snapshot.content_sha256 != digest:
            raise ValueError("reporting reliability reference hash does not match")
        return snapshot


class ReportingReliabilityService:
    """Publish Mission 1 reliability snapshots through the existing source seam."""

    belief_kind = "reporting_reliability"

    def __init__(
        self,
        manager: ReportingReliabilityManager,
        store: FileReportingReliabilityStore,
        transport: Any,
        snapshot: ReportingReliabilitySnapshot,
        *,
        pending: Mapping[str, object] | None = None,
        observation_topic: str = "belief-observations",
        context_topic: str = "normalized-plans",
        clock: Callable[[], str],
    ) -> None:
        self.manager = manager
        self.store = store
        self.transport = transport
        self.observation_topic = observation_topic
        self.context_topic = context_topic
        self._clock = clock
        self._snapshot = snapshot
        if pending is not None:
            self._publish_pending(dict(pending))
            self.store.save(snapshot, manager.checkpoint(), None)

    @classmethod
    def create(
        cls,
        mission_id: str,
        entity_ids: Iterable[int],
        store: FileReportingReliabilityStore,
        transport: Any,
        *,
        observation_topic: str = "belief-observations",
        context_topic: str = "normalized-plans",
        clock: Callable[[], str],
    ) -> "ReportingReliabilityService":
        loaded = store.load(mission_id)
        if loaded is not None:
            snapshot, checkpoint, pending = loaded
            configured = tuple(sorted(set(entity_ids)))
            if configured != tuple(sorted(checkpoint.ship_weights)):
                raise ValueError("configured reporting ships do not match checkpoint")
            return cls(
                ReportingReliabilityManager.from_checkpoint(checkpoint),
                store,
                transport,
                snapshot,
                pending=pending,
                observation_topic=observation_topic,
                context_topic=context_topic,
                clock=clock,
            )
        manager = ReportingReliabilityManager(mission_id, entity_ids)
        snapshot = manager.snapshot(
            input_event_id=manager.last_input_event_id,
            input_revision=manager.last_input_revision,
            created_at=clock(),
        )
        service = cls(
            manager,
            store,
            transport,
            snapshot,
            observation_topic=observation_topic,
            context_topic=context_topic,
            clock=clock,
        )
        service._commit_and_publish(snapshot)
        return service

    @staticmethod
    def _reference(snapshot: ReportingReliabilitySnapshot) -> str:
        return reporting_reliability_reference(snapshot)

    def _pending(self, snapshot: ReportingReliabilitySnapshot) -> dict[str, object]:
        return {
            "event_id": f"belief.updated:{snapshot.mission_id}:{snapshot.belief_revision}:{snapshot.content_sha256}",
            "payload": {
                "source": "bayesian_belief_snapshot",
                "revision": snapshot.belief_revision,
                "reference": self._reference(snapshot),
                "content_sha256": snapshot.content_sha256,
                "health": "healthy",
                "fresh": True,
            },
        }

    def _commit_and_publish(self, snapshot: ReportingReliabilitySnapshot) -> None:
        pending = self._pending(snapshot)
        self.store.save(snapshot, self.manager.checkpoint(), pending)
        self._publish_pending(pending)
        self.store.save(snapshot, self.manager.checkpoint(), None)
        self._snapshot = snapshot

    def _publish_pending(self, pending: Mapping[str, object]) -> None:
        event_id = str(pending["event_id"])
        get_event = getattr(self.transport, "get_event", None)
        existing = get_event(event_id) if callable(get_event) else None
        if existing is None:
            event = TransportEvent(
                schema_version=1,
                event_id=event_id,
                mission_id=self.manager.mission_id,
                sequence=self.transport.next_event_sequence(self.context_topic, self.manager.mission_id),
                event_kind="belief.updated",
                payload=pending["payload"],
            )
            try:
                self.transport.publish_event(self.context_topic, event)
            except ValueError:
                latest = self.transport.latest_event(
                    self.context_topic,
                    self.manager.mission_id,
                    event_kind="belief.updated",
                )
                if latest is None or latest.event_id != event_id:
                    raise

    def ingest_environment_tick(
        self, tick: EnvironmentTickResult
    ) -> ReportingReliabilitySnapshot | None:
        environment = tick.environment_data
        if environment.get("mission_id") != self.manager.mission_id:
            raise ValueError("reporting reliability tick Mission ID does not match")
        revision = environment.get("state_version")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("reporting reliability tick state revision is invalid")
        world = environment.get("world_model_info")
        checks = world.get("event_report_checks", ()) if isinstance(world, Mapping) else ()
        if not isinstance(checks, (list, tuple)) or not all(isinstance(check, Mapping) for check in checks):
            raise ValueError("event_report_checks must be an array of objects")
        event_hash = canonical_sha256(_plain(environment))
        snapshot = self.manager.update_checks(
            checks,
            input_event_id=f"environment-data:{self.manager.mission_id}:{event_hash}",
            input_revision=revision,
            created_at=self._clock(),
        )
        if snapshot is not None:
            self._commit_and_publish(snapshot)
        return snapshot

    def handle(self, event: TransportEvent) -> None:
        """Raw Event Observations are intentionally not reliability evidence."""

        _ = event
        return None

    def load_current_snapshot(self) -> ReportingReliabilitySnapshot:
        return self._snapshot

    def load_snapshot_reference(self, reference: str) -> ReportingReliabilitySnapshot:
        return self.store.load_reference(self.manager.mission_id, reference)


__all__ = [
    "FileReportingReliabilityStore",
    "ReportingReliabilityCheckpoint",
    "ReportingReliabilityManager",
    "ReportingReliabilityService",
    "reporting_reliability_reference",
]
