"""Deterministic standard-library SIR filtering for generic binary beliefs."""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from onr.contracts.bayesian_belief import (
    BayesianBeliefSnapshot,
    BeliefKey,
    BeliefMarginal,
    ForbiddenBeliefCombination,
    RiskObservation,
    canonical_json,
    canonical_sha256,
)
from onr.contracts.transport import TransportEvent
from onr.ports.transport import Subscription


def _probability(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a probability")
    selected = float(value)
    if not math.isfinite(selected) or not 0.0 <= selected <= 1.0:
        raise ValueError(f"{label} must be a finite probability")
    return selected


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("checkpoint random state is not JSON-safe")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError("checkpoint random state is malformed")


def _json_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class BayesianBeliefCheckpoint:
    """Bounded restart state for a ``BayesianBeliefManager``."""

    schema_version: int
    mission_id: str
    belief_revision: int
    last_input_event_id: str | None
    last_input_revision: int | None
    transition_probability: float
    keys: tuple[BeliefKey, ...]
    constraints: tuple[ForbiddenBeliefCombination, ...]
    particles: tuple[tuple[bool, ...], ...]
    random_state: object
    content_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("unsupported Bayesian belief checkpoint schema version")
        if not isinstance(self.mission_id, str) or not self.mission_id.strip():
            raise ValueError("checkpoint mission ID must be a non-empty string")
        if (
            isinstance(self.belief_revision, bool)
            or not isinstance(self.belief_revision, int)
            or self.belief_revision < 0
        ):
            raise ValueError("checkpoint belief revision must be non-negative")
        if (self.last_input_event_id is None) != (self.last_input_revision is None):
            raise ValueError("checkpoint input provenance must be wholly present or absent")
        if self.last_input_event_id is not None and (
            not isinstance(self.last_input_event_id, str)
            or not self.last_input_event_id.strip()
        ):
            raise ValueError("checkpoint input event ID must be a non-empty string")
        if self.last_input_revision is not None and (
            isinstance(self.last_input_revision, bool)
            or not isinstance(self.last_input_revision, int)
            or self.last_input_revision < 0
        ):
            raise ValueError("checkpoint input revision must be non-negative")
        transition_probability = _probability(
            self.transition_probability, "checkpoint transition probability"
        )
        keys = tuple(self.keys)
        if not keys or not all(isinstance(key, BeliefKey) for key in keys):
            raise ValueError("checkpoint keys must contain typed belief keys")
        if len(keys) != len(set(keys)):
            raise ValueError("checkpoint belief keys must be unique")
        constraints = tuple(self.constraints)
        if not all(isinstance(item, ForbiddenBeliefCombination) for item in constraints):
            raise ValueError("checkpoint constraints must be typed")
        constraint_ids = [item.constraint_id for item in constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("checkpoint constraint IDs must be unique")
        particles = tuple(tuple(particle) for particle in self.particles)
        if not particles or any(
            len(particle) != len(keys) or not all(isinstance(value, bool) for value in particle)
            for particle in particles
        ):
            raise ValueError("checkpoint particles must be non-empty Boolean assignments")
        random_state = _freeze_json(self.random_state)
        try:
            validator = random.Random()
            validator.setstate(random_state)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("checkpoint random state is invalid") from exc
        object.__setattr__(self, "transition_probability", transition_probability)
        object.__setattr__(self, "keys", tuple(sorted(keys)))
        object.__setattr__(
            self, "constraints", tuple(sorted(constraints, key=lambda item: item.constraint_id))
        )
        object.__setattr__(self, "particles", particles)
        object.__setattr__(self, "random_state", random_state)
        key_indexes = {key: index for index, key in enumerate(self.keys)}
        try:
            indexed_constraints = tuple(
                tuple((key_indexes[item.key], item.is_risk) for item in constraint.assignments)
                for constraint in self.constraints
            )
        except KeyError as exc:
            raise ValueError("checkpoint constraint references an undeclared belief key") from exc
        if any(
            any(
                all(particle[index] is required for index, required in assignments)
                for assignments in indexed_constraints
            )
            for particle in particles
        ):
            raise ValueError("checkpoint contains a constraint-violating particle")
        if (
            not isinstance(self.content_sha256, str)
            or len(self.content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.content_sha256)
        ):
            raise ValueError("checkpoint content hash must be a lowercase SHA-256 digest")
        if self.content_sha256 != canonical_sha256(self.content_dict()):
            raise ValueError("checkpoint content hash does not match its canonical content")

    def content_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "belief_revision": self.belief_revision,
            "last_input_event_id": self.last_input_event_id,
            "last_input_revision": self.last_input_revision,
            "transition_probability": self.transition_probability,
            "keys": [key.to_dict() for key in self.keys],
            "constraints": [constraint.to_dict() for constraint in self.constraints],
            "particles": [list(particle) for particle in self.particles],
            "random_state": _json_value(self.random_state),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.content_dict(), "content_sha256": self.content_sha256}

    def to_canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def create(
        cls,
        *,
        mission_id: str,
        belief_revision: int,
        last_input_event_id: str | None,
        last_input_revision: int | None,
        transition_probability: float,
        keys: Sequence[BeliefKey],
        constraints: Sequence[ForbiddenBeliefCombination],
        particles: Sequence[Sequence[bool]],
        random_state: object,
    ) -> "BayesianBeliefCheckpoint":
        selected_keys = tuple(sorted(keys))
        selected_constraints = tuple(sorted(constraints, key=lambda item: item.constraint_id))
        selected_particles = tuple(tuple(particle) for particle in particles)
        frozen_state = _freeze_json(random_state)
        content = {
            "schema_version": 1,
            "mission_id": mission_id,
            "belief_revision": belief_revision,
            "last_input_event_id": last_input_event_id,
            "last_input_revision": last_input_revision,
            "transition_probability": float(transition_probability),
            "keys": [key.to_dict() for key in selected_keys],
            "constraints": [constraint.to_dict() for constraint in selected_constraints],
            "particles": [list(particle) for particle in selected_particles],
            "random_state": _json_value(frozen_state),
        }
        return cls(
            schema_version=1,
            mission_id=mission_id,
            belief_revision=belief_revision,
            last_input_event_id=last_input_event_id,
            last_input_revision=last_input_revision,
            transition_probability=transition_probability,
            keys=selected_keys,
            constraints=selected_constraints,
            particles=selected_particles,
            random_state=frozen_state,
            content_sha256=canonical_sha256(content),
        )

    @classmethod
    def from_dict(cls, value: object) -> "BayesianBeliefCheckpoint":
        fields = {
            "schema_version",
            "mission_id",
            "belief_revision",
            "last_input_event_id",
            "last_input_revision",
            "transition_probability",
            "keys",
            "constraints",
            "particles",
            "random_state",
            "content_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("Bayesian belief checkpoint contains unknown or missing fields")
        if not isinstance(value["keys"], list) or not isinstance(value["constraints"], list):
            raise ValueError("checkpoint keys and constraints must be arrays")
        raw_particles = value["particles"]
        if not isinstance(raw_particles, list) or not all(
            isinstance(particle, list) for particle in raw_particles
        ):
            raise ValueError("checkpoint particles must be arrays")
        return cls(
            schema_version=value["schema_version"],
            mission_id=value["mission_id"],
            belief_revision=value["belief_revision"],
            last_input_event_id=value["last_input_event_id"],
            last_input_revision=value["last_input_revision"],
            transition_probability=value["transition_probability"],
            keys=tuple(BeliefKey.from_dict(item) for item in value["keys"]),
            constraints=tuple(
                ForbiddenBeliefCombination.from_dict(item) for item in value["constraints"]
            ),
            particles=tuple(tuple(particle) for particle in raw_particles),
            random_state=value["random_state"],
            content_sha256=value["content_sha256"],
        )

    @classmethod
    def from_json(cls, value: str) -> "BayesianBeliefCheckpoint":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Bayesian belief checkpoint JSON is invalid") from exc
        return cls.from_dict(decoded)


class BayesianBeliefManager:
    """Sequential-importance-resampling filter over generic binary risk keys."""

    def __init__(
        self,
        mission_id: str,
        keys: Iterable[BeliefKey],
        *,
        constraints: Iterable[ForbiddenBeliefCombination] = (),
        particle_count: int = 1024,
        transition_probability: float = 0.0,
        rng: random.Random | None = None,
        seed: int | None = None,
    ) -> None:
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission ID must be a non-empty string")
        if isinstance(particle_count, bool) or not isinstance(particle_count, int) or particle_count < 1:
            raise ValueError("particle count must be a positive integer")
        if rng is not None and seed is not None:
            raise ValueError("provide either an RNG or a seed, not both")
        if rng is not None and not isinstance(rng, random.Random):
            raise TypeError("RNG must be an instance of random.Random")

        selected_keys = tuple(keys)
        if not selected_keys or not all(isinstance(key, BeliefKey) for key in selected_keys):
            raise ValueError("belief keys must contain typed BeliefKey values")
        if len(selected_keys) != len(set(selected_keys)):
            raise ValueError("belief keys must be unique")
        selected_constraints = tuple(constraints)
        if not all(isinstance(item, ForbiddenBeliefCombination) for item in selected_constraints):
            raise ValueError("constraints must be typed forbidden combinations")
        constraint_ids = [item.constraint_id for item in selected_constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("constraint IDs must be unique")

        self.mission_id = mission_id
        self.keys = tuple(sorted(selected_keys))
        self.constraints = tuple(sorted(selected_constraints, key=lambda item: item.constraint_id))
        self.transition_probability = _probability(
            transition_probability, "transition probability"
        )
        self.rng = rng if rng is not None else random.Random(seed)
        self._key_indexes = {key: index for index, key in enumerate(self.keys)}
        unknown_keys = {
            assignment.key
            for constraint in self.constraints
            for assignment in constraint.assignments
            if assignment.key not in self._key_indexes
        }
        if unknown_keys:
            raise ValueError("logical constraints reference undeclared belief keys")
        self._indexed_constraints = tuple(
            tuple((self._key_indexes[item.key], item.is_risk) for item in constraint.assignments)
            for constraint in self.constraints
        )
        self._completion_cache: dict[tuple[bool | None, ...], bool] = {}
        if not self._has_completion((None,) * len(self.keys)):
            raise ValueError("logical constraints forbid every possible belief assignment")
        self._particles = tuple(self._sample_valid_particle() for _ in range(particle_count))
        self.belief_revision = 0
        self.last_input_event_id: str | None = None
        self.last_input_revision: int | None = None

    @property
    def particle_count(self) -> int:
        return len(self._particles)

    def _violates(self, particle: Sequence[bool]) -> bool:
        return any(
            all(particle[index] is required for index, required in assignments)
            for assignments in self._indexed_constraints
        )

    def _has_completion(self, partial: tuple[bool | None, ...]) -> bool:
        cached = self._completion_cache.get(partial)
        if cached is not None:
            return cached
        for assignments in self._indexed_constraints:
            if all(partial[index] is required for index, required in assignments):
                self._completion_cache[partial] = False
                return False
        try:
            index = partial.index(None)
        except ValueError:
            self._completion_cache[partial] = True
            return True
        for candidate in (False, True):
            selected = partial[:index] + (candidate,) + partial[index + 1 :]
            if self._has_completion(selected):
                self._completion_cache[partial] = True
                return True
        self._completion_cache[partial] = False
        return False

    def _sample_valid_particle(self) -> tuple[bool, ...]:
        while True:
            particle = tuple(self.rng.random() < 0.5 for _ in self.keys)
            if not self._violates(particle):
                return particle

    def _predict(self) -> tuple[tuple[bool, ...], ...]:
        predicted: list[tuple[bool, ...]] = []
        for particle in self._particles:
            proposal = tuple(
                not value if self.rng.random() < self.transition_probability else value
                for value in particle
            )
            predicted.append(particle if self._violates(proposal) else proposal)
        return tuple(predicted)

    def update(
        self,
        observation: RiskObservation,
        *,
        created_at: str | None = None,
    ) -> BayesianBeliefSnapshot:
        """Apply one observation atomically and return the next immutable snapshot."""

        if not isinstance(observation, RiskObservation):
            raise TypeError("observation must be a RiskObservation")
        if self.last_input_revision is not None and observation.input_revision <= self.last_input_revision:
            raise ValueError("input revision must increase monotonically")
        observation_indexes: list[tuple[int, float]] = []
        for association in observation.associations:
            key = BeliefKey(association.entity_id, observation.risk_type)
            try:
                index = self._key_indexes[key]
            except KeyError as exc:
                raise ValueError("observation references an undeclared entity/risk key") from exc
            observation_indexes.append((index, association.weight))

        prior_random_state = self.rng.getstate()
        try:
            predicted = self._predict()
            likelihoods = tuple(
                math.fsum(
                    weight
                    * (
                        observation.likelihood_given_risk
                        if particle[index]
                        else observation.likelihood_given_safe
                    )
                    for index, weight in observation_indexes
                )
                for particle in predicted
            )
            total = math.fsum(likelihoods)
            if not math.isfinite(total) or total <= 0.0:
                raise ValueError("observation has zero likelihood under every particle")
            normalized = tuple(weight / total for weight in likelihoods)
            start = self.rng.random() / self.particle_count
            positions = tuple(start + index / self.particle_count for index in range(self.particle_count))
            selected_particles: list[tuple[bool, ...]] = []
            cumulative = normalized[0]
            source_index = 0
            for position in positions:
                while position > cumulative and source_index < self.particle_count - 1:
                    source_index += 1
                    cumulative += normalized[source_index]
                selected_particles.append(predicted[source_index])
            posterior = tuple(selected_particles)
            if any(self._violates(particle) for particle in posterior):
                raise ValueError("logical constraints produced an invalid posterior")

            revision = self.belief_revision + 1
            timestamp = (
                datetime.now(timezone.utc).isoformat(timespec="microseconds")
                if created_at is None
                else created_at
            )
            snapshot = BayesianBeliefSnapshot.create(
                mission_id=self.mission_id,
                belief_revision=revision,
                input_event_id=observation.event_id,
                input_revision=observation.input_revision,
                created_at=timestamp,
                marginals=(
                    BeliefMarginal(
                        key,
                        math.fsum(1.0 for particle in posterior if particle[index])
                        / self.particle_count,
                    )
                    for index, key in enumerate(self.keys)
                ),
            )
        except Exception:
            self.rng.setstate(prior_random_state)
            raise

        self._particles = posterior
        self.belief_revision = revision
        self.last_input_event_id = observation.event_id
        self.last_input_revision = observation.input_revision
        return snapshot

    def checkpoint(self) -> BayesianBeliefCheckpoint:
        return BayesianBeliefCheckpoint.create(
            mission_id=self.mission_id,
            belief_revision=self.belief_revision,
            last_input_event_id=self.last_input_event_id,
            last_input_revision=self.last_input_revision,
            transition_probability=self.transition_probability,
            keys=self.keys,
            constraints=self.constraints,
            particles=self._particles,
            random_state=self.rng.getstate(),
        )

    def configure_constraints(
        self, constraints: Iterable[ForbiddenBeliefCombination]
    ) -> None:
        """Condition current particles on a validated replacement constraint set."""

        cloned_rng = random.Random()
        cloned_rng.setstate(self.rng.getstate())
        candidate = BayesianBeliefManager(
            self.mission_id,
            self.keys,
            constraints=tuple(constraints),
            particle_count=self.particle_count,
            transition_probability=self.transition_probability,
            rng=cloned_rng,
        )
        if self.belief_revision == 0:
            particles = candidate._particles
        else:
            valid = tuple(
                particle
                for particle in self._particles
                if not candidate._violates(particle)
            )
            if not valid:
                raise ValueError("logical constraints reject every current particle")
            particles = tuple(
                valid[cloned_rng.randrange(len(valid))]
                for _ in range(self.particle_count)
            )
        self.constraints = candidate.constraints
        self.rng = candidate.rng
        self._indexed_constraints = candidate._indexed_constraints
        self._completion_cache = candidate._completion_cache
        self._particles = particles

    def record_input(self, event_id: str, input_revision: int) -> None:
        """Advance generic input provenance after a non-observation state change."""

        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("input event ID must be a non-empty string")
        if (
            isinstance(input_revision, bool)
            or not isinstance(input_revision, int)
            or input_revision < 0
        ):
            raise ValueError("input revision must be non-negative")
        if self.last_input_revision is not None and input_revision <= self.last_input_revision:
            raise ValueError("input revision must increase monotonically")
        self.last_input_event_id = event_id
        self.last_input_revision = input_revision

    @classmethod
    def from_checkpoint(cls, checkpoint: BayesianBeliefCheckpoint) -> "BayesianBeliefManager":
        if not isinstance(checkpoint, BayesianBeliefCheckpoint):
            raise TypeError("checkpoint must be a BayesianBeliefCheckpoint")
        manager = cls(
            checkpoint.mission_id,
            checkpoint.keys,
            constraints=checkpoint.constraints,
            particle_count=len(checkpoint.particles),
            transition_probability=checkpoint.transition_probability,
            rng=random.Random(0),
        )
        if any(manager._violates(particle) for particle in checkpoint.particles):
            raise ValueError("checkpoint contains a constraint-violating particle")
        manager._particles = checkpoint.particles
        manager.belief_revision = checkpoint.belief_revision
        manager.last_input_event_id = checkpoint.last_input_event_id
        manager.last_input_revision = checkpoint.last_input_revision
        manager.rng.setstate(checkpoint.random_state)  # type: ignore[arg-type]
        return manager


def canonical_mission_component(mission_id: str) -> str:
    """Validate and percent-encode one portable mission path component."""

    if (
        not isinstance(mission_id, str)
        or not mission_id.strip()
        or mission_id != mission_id.strip()
        or "/" in mission_id
        or "\\" in mission_id
        or mission_id in {".", ".."}
    ):
        raise ValueError("mission ID must be one path component")
    return quote(mission_id, safe="._-")


def belief_artifact_reference(mission_id: str, content_sha256: str) -> str:
    """Return a portable reference to the immutable committed artifact file."""

    if (
        not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
    ):
        raise ValueError("belief artifact hash must be a lowercase SHA-256 digest")
    mission = canonical_mission_component(mission_id)
    return (
        f"bayesian-beliefs/{mission}/generations/by-content/"
        f"{content_sha256}/belief-v1.json"
        f"#sha256={content_sha256}"
    )


def create_risk_observation_event(
    mission_id: str,
    observation: RiskObservation,
    *,
    sequence: int,
) -> TransportEvent:
    if not isinstance(observation, RiskObservation):
        raise TypeError("observation must be a RiskObservation")
    if not isinstance(mission_id, str) or not mission_id.strip():
        raise ValueError("mission ID must be a non-empty string")
    return TransportEvent(
        schema_version=1,
        event_id=observation.event_id,
        mission_id=mission_id,
        sequence=sequence,
        event_kind="risk.observed",
        payload=observation.to_dict(),
    )


def create_belief_constraint_event(
    mission_id: str,
    constraints: Iterable[ForbiddenBeliefCombination],
    *,
    input_revision: int,
    event_id: str,
    sequence: int,
) -> TransportEvent:
    if isinstance(input_revision, bool) or not isinstance(input_revision, int) or input_revision < 0:
        raise ValueError("constraint input revision must be non-negative")
    selected = tuple(constraints)
    if not all(isinstance(item, ForbiddenBeliefCombination) for item in selected):
        raise ValueError("constraint event must contain typed forbidden combinations")
    return TransportEvent(
        schema_version=1,
        event_id=event_id,
        mission_id=mission_id,
        sequence=sequence,
        event_kind="belief.constraints",
        payload={
            "input_revision": input_revision,
            "constraints": [item.to_dict() for item in selected],
        },
    )


class BayesianBeliefService:
    """Transport wrapper that durably publishes reference-only belief updates."""

    def __init__(
        self,
        manager: BayesianBeliefManager,
        store: Any,
        transport: Any,
        *,
        observation_topic: str = "belief-observations",
        context_topic: str = "normalized-plans",
        service_id: str = "bayesian-belief-manager",
        subscription: Subscription | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(manager, BayesianBeliefManager):
            raise TypeError("belief service requires a BayesianBeliefManager")
        required_store_methods = (
            "commit_update",
            "save_checkpoint",
            "load_current",
            "load_pending_output",
            "clear_pending_output",
            "load_reference",
        )
        if any(not callable(getattr(store, name, None)) for name in required_store_methods):
            raise TypeError("belief service store does not expose its durable operations")
        self.manager = manager
        self.store = store
        self.transport = transport
        self.observation_topic = observation_topic
        self.context_topic = context_topic
        self.subscription = subscription or Subscription(
            service_id, manager.mission_id, observation_topic
        )
        if self.subscription.mission_id != manager.mission_id:
            raise ValueError("belief service subscription mission ID does not match")
        self._clock = clock
        self._last_input_revision = manager.last_input_revision

    @staticmethod
    def subscription_for(
        mission_id: str,
        *,
        observation_topic: str = "belief-observations",
        service_id: str = "bayesian-belief-manager",
        max_retries: int = 3,
    ) -> Subscription:
        return Subscription(service_id, mission_id, observation_topic, max_retries)

    def handle(self, event: TransportEvent) -> BayesianBeliefSnapshot | None:
        self.flush_pending_output()
        if not isinstance(event, TransportEvent):
            raise TypeError("belief service requires a TransportEvent")
        if event.mission_id != self.manager.mission_id:
            raise ValueError("belief input event belongs to another mission")
        if event.event_kind == "belief.constraints":
            if set(event.payload) != {"input_revision", "constraints"}:
                raise ValueError("belief constraint event contains unknown or missing fields")
            input_revision = event.payload.get("input_revision")
            raw_constraints = event.payload.get("constraints")
            if (
                isinstance(input_revision, bool)
                or not isinstance(input_revision, int)
                or input_revision < 0
                or not isinstance(raw_constraints, (list, tuple))
            ):
                raise ValueError("belief constraint event is malformed")
            if self._is_committed_input(event.event_id, input_revision):
                return None
            self._require_next_input_revision(input_revision)
            constraints = tuple(
                ForbiddenBeliefCombination.from_dict(item) for item in raw_constraints
            )
            prior = self.manager.checkpoint()
            try:
                self.manager.configure_constraints(constraints)
                self.manager.record_input(event.event_id, input_revision)
                self.store.save_checkpoint(self.manager.checkpoint())
            except Exception:
                self._recover_or_restore(prior, event.event_id, input_revision)
                raise
            self._last_input_revision = input_revision
            return None
        if event.event_kind != "risk.observed":
            raise ValueError("belief service received an unsupported event kind")
        observation = RiskObservation.from_dict(event.payload)
        if observation.event_id != event.event_id:
            raise ValueError("risk observation event identity does not match its payload")
        if self._is_committed_input(event.event_id, observation.input_revision):
            return self.load_current_snapshot()
        self._require_next_input_revision(observation.input_revision)
        prior = self.manager.checkpoint()
        try:
            snapshot = self.manager.update(
                observation, created_at=self._clock() if self._clock else None
            )
            checkpoint = self.manager.checkpoint()
            reference = belief_artifact_reference(
                snapshot.mission_id, snapshot.content_sha256
            )
            output = {
                "schema_version": 1,
                "event_id": (
                    f"belief.updated:{snapshot.mission_id}:{snapshot.belief_revision}"
                ),
                "mission_id": snapshot.mission_id,
                "event_kind": "belief.updated",
                "payload": {
                    "source": "bayesian_belief_snapshot",
                    "revision": snapshot.belief_revision,
                    "reference": reference,
                    "content_sha256": snapshot.content_sha256,
                    "health": "healthy",
                    "fresh": True,
                },
            }
            # The pending output is durable before current becomes visible. It is
            # singular and is cleared only after publication succeeds.
            self.store.commit_update(
                snapshot,
                checkpoint,
                pending_topic=self.context_topic,
                pending_event=output,
            )
        except Exception:
            self._recover_or_restore(
                prior, observation.event_id, observation.input_revision
            )
            raise
        self._last_input_revision = observation.input_revision
        self.flush_pending_output()
        return snapshot

    def _is_committed_input(self, event_id: str, revision: int) -> bool:
        return (
            self.manager.last_input_event_id == event_id
            and self.manager.last_input_revision == revision
        )

    def _recover_or_restore(
        self,
        prior: BayesianBeliefCheckpoint,
        event_id: str,
        input_revision: int,
    ) -> None:
        loader = getattr(self.store, "load_checkpoint", None)
        committed = loader(self.manager.mission_id) if callable(loader) else None
        if (
            isinstance(committed, BayesianBeliefCheckpoint)
            and committed.last_input_event_id == event_id
            and committed.last_input_revision == input_revision
        ):
            self.manager = BayesianBeliefManager.from_checkpoint(committed)
            self._last_input_revision = input_revision
        else:
            self.manager = BayesianBeliefManager.from_checkpoint(prior)

    def _require_next_input_revision(self, revision: int) -> None:
        if self._last_input_revision is not None and revision <= self._last_input_revision:
            raise ValueError("belief input revision must increase monotonically")

    def run_once(self, consumer: Any) -> BayesianBeliefSnapshot | None:
        self.flush_pending_output()
        delivery = consumer.receive()
        if delivery is None:
            return None
        if not isinstance(delivery.message, TransportEvent):
            delivery.nack()
            raise ValueError("belief delivery is not a TransportEvent")
        try:
            result = self.handle(delivery.message)
        except Exception:
            delivery.nack()
            raise
        delivery.ack()
        return result

    def flush_pending_output(self) -> TransportEvent | None:
        loader = getattr(self.store, "load_pending_output", None)
        clearer = getattr(self.store, "clear_pending_output", None)
        if not callable(loader) or not callable(clearer):
            raise TypeError("belief service store must expose pending-output operations")
        pending = loader(self.manager.mission_id)
        if pending is None:
            return None
        if not isinstance(pending, Mapping):
            raise TypeError("belief service store returned an invalid pending output")
        topic = pending.get("topic")
        event_id = pending.get("event_id")
        mission_id = pending.get("mission_id")
        event_kind = pending.get("event_kind")
        schema_version = pending.get("schema_version")
        payload = pending.get("payload")
        if (
            topic != self.context_topic
            or mission_id != self.manager.mission_id
            or not isinstance(topic, str)
            or not isinstance(mission_id, str)
            or not isinstance(event_id, str)
            or event_kind != "belief.updated"
            or schema_version != 1
            or not isinstance(payload, Mapping)
        ):
            raise ValueError("pending belief output does not match the service")
        get_event = getattr(self.transport, "get_event", None)
        existing = get_event(event_id) if callable(get_event) else None
        if existing is not None:
            if not isinstance(existing, TransportEvent) or not self._matches_pending(
                existing, pending
            ):
                raise ValueError("published belief output identity is inconsistent")
            clearer(self.manager.mission_id, event_id)
            return existing
        while True:
            sequence = self.transport.next_event_sequence(topic, mission_id)
            event = TransportEvent(
                schema_version=1,
                event_id=event_id,
                mission_id=mission_id,
                sequence=sequence,
                event_kind="belief.updated",
                payload=dict(payload),
            )
            try:
                self.transport.publish_event(topic, event)
            except ValueError:
                existing = get_event(event_id) if callable(get_event) else None
                if existing is not None:
                    if not isinstance(
                        existing, TransportEvent
                    ) or not self._matches_pending(existing, pending):
                        raise ValueError(
                            "published belief output identity is inconsistent"
                        )
                    event = existing
                    break
                if self.transport.next_event_sequence(topic, mission_id) <= sequence:
                    raise
                continue
            break
        clearer(self.manager.mission_id, event_id)
        return event

    @staticmethod
    def _matches_pending(event: object, pending: Mapping[str, object]) -> bool:
        return (
            isinstance(event, TransportEvent)
            and event.schema_version == pending.get("schema_version")
            and event.event_id == pending.get("event_id")
            and event.mission_id == pending.get("mission_id")
            and event.event_kind == pending.get("event_kind")
            and event.payload == pending.get("payload")
        )

    def load_current_snapshot(self) -> BayesianBeliefSnapshot | None:
        loader = getattr(self.store, "load_current", None)
        if not callable(loader):
            raise TypeError("belief service store must expose load_current")
        snapshot = loader(self.manager.mission_id)
        if snapshot is not None and not isinstance(snapshot, BayesianBeliefSnapshot):
            raise TypeError("belief service store returned an invalid snapshot")
        return snapshot

    def load_snapshot_reference(
        self, reference: str, content_sha256: str
    ) -> BayesianBeliefSnapshot:
        loader = getattr(self.store, "load_reference", None)
        if not callable(loader):
            raise TypeError("belief service store must expose load_reference")
        snapshot = loader(self.manager.mission_id, reference, content_sha256)
        if not isinstance(snapshot, BayesianBeliefSnapshot):
            raise TypeError("belief service store returned an invalid referenced snapshot")
        return snapshot


__all__ = [
    "BayesianBeliefCheckpoint",
    "BayesianBeliefManager",
    "BayesianBeliefService",
    "belief_artifact_reference",
    "canonical_mission_component",
    "create_belief_constraint_event",
    "create_risk_observation_event",
]
