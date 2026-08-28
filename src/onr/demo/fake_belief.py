"""Deterministic Bayesian entity-risk evidence for the packaged demo."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from onr.application.bayesian_belief import (
    BayesianBeliefManager,
    BayesianBeliefService,
    create_risk_observation_event,
)
from onr.contracts.bayesian_belief import (
    BayesianBeliefSnapshot,
    BeliefKey,
    EntityAssociation,
    RiskObservation,
)

_ENTITY_IDS = tuple(str(index) for index in range(1, 21))


def create_fake_entity_risk_snapshot(mission_id: str) -> BayesianBeliefSnapshot:
    """Produce deterministic event-risk marginals through the real belief manager."""

    manager = BayesianBeliefManager(
        mission_id,
        (BeliefKey(entity_id, "event-risk") for entity_id in _ENTITY_IDS),
        particle_count=2048,
        seed=23,
    )
    snapshot: BayesianBeliefSnapshot | None = None

    for revision, entity_id in enumerate(_ENTITY_IDS, start=1):
        risk_signal = 0.2 + ((revision * 29) % 60) / 100
        snapshot = manager.update(
            RiskObservation(
                event_id=f"fake-event-risk:{entity_id}",
                input_revision=revision,
                risk_type="event-risk",
                associations=(EntityAssociation(entity_id=entity_id, weight=1.0),),
                likelihood_given_risk=risk_signal,
                likelihood_given_safe=1.0 - risk_signal,
            ),
            created_at=f"2026-08-20T00:00:{revision:02d}+00:00",
        )

    if snapshot is None:
        raise RuntimeError("fake belief generation produced no snapshot")
    return snapshot


def seed_event_risk_beliefs(
    service: BayesianBeliefService,
    report: Sequence[Mapping[str, object]],
) -> BayesianBeliefSnapshot:
    """Seed one observation per report entity through transport and the real service."""

    if not isinstance(service, BayesianBeliefService):
        raise TypeError("belief seeding requires BayesianBeliefService")
    expected = {item.entity_id for item in service.manager.keys}
    first_by_entity: dict[str | int, tuple[int, Mapping[str, object]]] = {}
    for source_index, record in enumerate(report, start=1):
        entity_id = record.get("entity_id")
        if isinstance(entity_id, bool) or not (
            (isinstance(entity_id, int) and entity_id > 0)
            or (isinstance(entity_id, str) and bool(entity_id.strip()))
        ):
            raise ValueError("event report entity ID is missing")
        if entity_id not in expected and str(entity_id) in expected:
            entity_id = str(entity_id)
        first_by_entity.setdefault(entity_id, (source_index, record))
    if set(first_by_entity) != expected:
        raise ValueError("event report entities do not match event-risk belief keys")

    for input_revision, entity_id in enumerate(
        sorted(first_by_entity, key=int), start=1
    ):
        source_index, _record = first_by_entity[entity_id]
        uncertainty = 0.1 + ((source_index * 37) % 35) / 100
        event_id = f"initial-event-risk:{service.manager.mission_id}:{entity_id}"
        if service.transport.get_event(event_id) is not None:
            continue
        sequence = service.transport.next_event_sequence(
            service.observation_topic, service.manager.mission_id
        )
        observation = RiskObservation(
            event_id=event_id,
            input_revision=input_revision,
            risk_type="event-risk",
            associations=(EntityAssociation(entity_id, 1.0),),
            likelihood_given_risk=1.0 - uncertainty,
            likelihood_given_safe=uncertainty,
        )
        service.transport.publish_event(
            service.observation_topic,
            create_risk_observation_event(
                service.manager.mission_id,
                observation,
                sequence=sequence,
            ),
        )

    with service.transport.open_consumer(service.subscription) as consumer:
        latest = service.drain_to_latest(consumer)
    snapshot = latest or service.load_current_snapshot()
    if not isinstance(snapshot, BayesianBeliefSnapshot):
        raise RuntimeError("event-risk belief seeding produced no snapshot")
    return snapshot


__all__ = ["create_fake_entity_risk_snapshot", "seed_event_risk_beliefs"]
