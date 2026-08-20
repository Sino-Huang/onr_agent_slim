"""Deterministic Bayesian entity-risk evidence for the packaged demo."""

from __future__ import annotations

from onr.application.bayesian_belief import BayesianBeliefManager
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
