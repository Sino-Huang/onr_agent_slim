from __future__ import annotations

from pathlib import Path

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.bayesian_belief import (
    BayesianBeliefManager,
    BayesianBeliefService,
    create_risk_observation_event,
)
from onr.application.context_coordination import ContextCoordination
from onr.contracts.bayesian_belief import (
    BeliefKey,
    EntityAssociation,
    RiskObservation,
)
from onr.contracts.context_coordination import create_source_fact_event
from onr.ports.transport import Subscription


def test_context_drain_advances_entire_batch_and_returns_only_latest() -> None:
    mission_id = "mission-drain"
    subscription = Subscription("context-coordination", mission_id, "planning-evidence")
    transport = InProcessTransport((subscription,))
    service = ContextCoordination(
        transport,
        mission_id,
        input_topic="planning-evidence",
        subscription=subscription,
        clock=lambda: "2026-08-23T00:00:00+10:00",
    )
    for revision in range(100):
        transport.publish_event(
            "planning-evidence",
            create_source_fact_event(
                mission_id,
                "environment_data",
                revision,
                event_id=f"environment:{revision}",
                sequence=revision,
                reference=f"environment-data:{revision}",
            ),
        )

    with transport.open_consumer(subscription) as consumer:
        latest = service.drain_to_latest(consumer)
        assert service.drain_to_latest(consumer) is None

    assert latest is not None
    assert latest.version == 100
    assert latest.environment_data == "environment-data:99"
    assert transport.get_cursor(subscription)["sequence"] == 99
    assert transport.next_event_sequence("mission-snapshots", mission_id) == 100


def test_belief_drain_commits_every_observation_once_and_returns_latest(
    tmp_path: Path,
) -> None:
    mission_id = "mission-belief-drain"
    subscription = Subscription(
        "bayesian-belief-manager", mission_id, "belief-observations"
    )
    transport = InProcessTransport((subscription,))
    service = BayesianBeliefService(
        BayesianBeliefManager(
            mission_id,
            (BeliefKey("entity-1", "event-risk"),),
            particle_count=64,
            seed=5,
        ),
        FileBayesianBeliefStore(tmp_path),
        transport,
        context_topic="planning-evidence",
        subscription=subscription,
        clock=lambda: "2026-08-23T00:00:00+10:00",
    )
    for revision in range(1, 11):
        observation = RiskObservation(
            event_id=f"risk:{revision}",
            input_revision=revision,
            risk_type="event-risk",
            associations=(EntityAssociation("entity-1", 1.0),),
            likelihood_given_risk=0.75,
            likelihood_given_safe=0.25,
        )
        transport.publish_event(
            "belief-observations",
            create_risk_observation_event(
                mission_id,
                observation,
                sequence=revision - 1,
            ),
        )

    with transport.open_consumer(subscription) as consumer:
        latest = service.drain_to_latest(consumer)
        assert service.drain_to_latest(consumer) is None

    assert latest is not None
    assert latest.belief_revision == 10
    assert latest.input_event_id == "risk:10"
    assert service.load_current_snapshot() == latest
    assert transport.get_cursor(subscription)["sequence"] == 9
    assert transport.next_event_sequence("planning-evidence", mission_id) == 10
