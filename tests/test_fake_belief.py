from pathlib import Path

from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.file_transport import FileTransport
from onr.application.bayesian_belief import BayesianBeliefManager, BayesianBeliefService
from onr.contracts.bayesian_belief import BeliefKey
from onr.demo.fake_belief import (
    create_fake_entity_risk_snapshot,
    seed_event_risk_beliefs,
)
from onr.demo.fake_environment import FakeEnvironment
from onr.ports.transport import Subscription


def test_fake_entity_risks_are_deterministic_belief_manager_output() -> None:
    first = create_fake_entity_risk_snapshot("mission:demo")
    second = create_fake_entity_risk_snapshot("mission:demo")

    assert first == second
    assert first.belief_revision == 20
    assert first.input_event_id == "fake-event-risk:20"
    assert {item.key.entity_id for item in first.marginals} == {
        str(index) for index in range(1, 21)
    }
    assert len(first.marginals) == 20
    assert {item.key.risk_type for item in first.marginals} == {"event-risk"}
    assert all(0.0 <= item.probability_risk <= 1.0 for item in first.marginals)
    assert len({round(item.probability_risk, 3) for item in first.marginals}) > 1


def test_initial_event_risks_replay_through_transport_and_durable_service(
    tmp_path: Path,
) -> None:
    mission_id = "mission:seeded"
    subscription = Subscription(
        "bayesian-belief-manager", mission_id, "belief-observations"
    )
    transport = FileTransport(tmp_path / "transport", (subscription,))
    environment = FakeEnvironment(transport, mission_id)
    service = BayesianBeliefService(
        BayesianBeliefManager(
            mission_id,
            tuple(
                BeliefKey(str(entity_id), "event-risk") for entity_id in range(1, 21)
            ),
            particle_count=256,
            seed=23,
        ),
        FileBayesianBeliefStore(tmp_path / "belief"),
        transport,
        subscription=subscription,
        clock=lambda: "2026-08-23T00:00:00+10:00",
    )

    snapshot = seed_event_risk_beliefs(service, environment.event_report)

    assert snapshot.belief_revision == 20
    assert service.load_current_snapshot() == snapshot
    assert transport.next_event_sequence("belief-observations", mission_id) == 20
    assert transport.next_event_sequence("normalized-plans", mission_id) == 20
    assert transport.get_cursor(subscription)["sequence"] == 19


def test_initial_event_risks_preserve_numeric_physical_entity_ids(
    tmp_path: Path,
) -> None:
    mission_id = "mission:physical-seeded"
    subscription = Subscription(
        "bayesian-belief-manager", mission_id, "belief-observations"
    )
    transport = FileTransport(tmp_path / "transport", (subscription,))
    source = FakeEnvironment(transport, mission_id).event_report
    numeric_report = tuple(
        {**record, "entity_id": int(record["entity_id"])} for record in source
    )
    service = BayesianBeliefService(
        BayesianBeliefManager(
            mission_id,
            tuple(BeliefKey(entity_id, "event-risk") for entity_id in range(1, 21)),
            particle_count=256,
            seed=23,
        ),
        FileBayesianBeliefStore(tmp_path / "belief"),
        transport,
        subscription=subscription,
        clock=lambda: "2026-08-23T00:00:00+10:00",
    )

    snapshot = seed_event_risk_beliefs(service, numeric_report)

    assert {item.key.entity_id for item in snapshot.marginals} == set(range(1, 21))
