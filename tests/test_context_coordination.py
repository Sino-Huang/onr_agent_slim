from __future__ import annotations

import json
from typing import Any, cast

from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport, InProcessTransportState
from onr.application.context_coordination import ContextCoordination
from onr.contracts.context_coordination import (
    MISSION_SNAPSHOT_SOURCES,
    MissionSnapshot,
    mission_snapshot_from_transport_event,
)
from onr.contracts.planning import (
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanningProfile,
)
from onr.contracts.transport import (
    TransportEvent,
    create_normalized_plan_transport_event,
    normalized_plan_transport_event_to_wire,
)
from onr.ports.transport import Subscription


def _plan(revision: int = 1) -> NormalizedPlan:
    return NormalizedPlan(
        mission_id="mission-context",
        source_authority="mission-control",
        plan_revision=revision,
        mission_snapshot_id=f"plan-snapshot-{revision}",
        planner_choice=PlannerChoice(PlanningProfile.TEMPORAL, "minizinc"),
        outcome=PlanningOutcome.UNSOLVABLE,
    )


def _deliver(service: ContextCoordination, consumer: Any) -> MissionSnapshot | None:
    return service.run_once(consumer)


def test_context_coordination_publishes_complete_immutable_manifest_through_transport() -> None:
    input_subscription = Subscription(
        "context-coordination", "mission-context", "normalized-plans"
    )
    output_subscription = Subscription(
        "snapshot-reader", "mission-context", "mission-snapshots"
    )
    transport = InProcessTransport((input_subscription, output_subscription))
    coordination = ContextCoordination(
        transport,
        "mission-context",
    )

    plan_event = create_normalized_plan_transport_event(
        _plan(), event_id="plan-event-1", sequence=0
    )
    transport.publish_event(
        coordination.input_topic,
        normalized_plan_transport_event_to_wire(plan_event),
    )
    with transport.open_consumer(coordination.subscription) as source_consumer, transport.open_consumer(
        output_subscription
    ) as output_consumer:
        snapshot = _deliver(coordination, source_consumer)
        assert snapshot is not None
        assert snapshot.version == 1
        assert snapshot.plan_revision == 1
        assert set(snapshot.missing_sources) == set(MISSION_SNAPSHOT_SOURCES) - {"plan"}
        assert snapshot.source_revisions["plan"] == 1
        assert snapshot.source_references["plan"] == plan_event.event_id

        delivery = output_consumer.receive()
        assert delivery is not None
        assert hasattr(delivery.message, "event_kind")
        wire_snapshot = mission_snapshot_from_transport_event(cast(TransportEvent, delivery.message))
        delivery.ack()
        assert wire_snapshot == snapshot
        assert json.loads(snapshot.to_canonical_json()) == snapshot.to_dict()
        assert MissionSnapshot.from_json(snapshot.to_canonical_json()) == snapshot
        try:
            snapshot.source_revisions["plan"] = 2  # type: ignore[index]
        except TypeError:
            pass
        else:
            raise AssertionError("snapshot source revisions must be immutable")

        final_snapshot = snapshot
        for index, source in enumerate(MISSION_SNAPSHOT_SOURCES[1:], start=1):
            coordination.publish_source_fact(
                source,
                revision=index,
                reference=f"{source}-ref-{index}",
                health="healthy",
            )
            final_snapshot = _deliver(coordination, source_consumer)
            assert final_snapshot is not None
            next_delivery = output_consumer.receive()
            assert next_delivery is not None
            next_delivery.ack()

        assert final_snapshot.missing_sources == ()
        assert all(final_snapshot.source_references.values())
        assert _deliver(coordination, source_consumer) is None


def test_context_coordination_does_not_publish_unchanged_facts_and_tracks_health_transition() -> None:
    input_subscription = Subscription(
        "context-coordination", "mission-context", "normalized-plans"
    )
    output_subscription = Subscription(
        "snapshot-reader", "mission-context", "mission-snapshots"
    )
    transport = InProcessTransport((input_subscription, output_subscription))
    coordination = ContextCoordination(transport, "mission-context")
    with transport.open_consumer(coordination.subscription) as source_consumer, transport.open_consumer(
        output_subscription
    ) as output_consumer:
        coordination.publish_source_fact("environment_data", 3, reference="scene-3")
        assert _deliver(coordination, source_consumer) is not None
        first = output_consumer.receive()
        assert first is not None
        assert hasattr(first.message, "event_kind")
        first_snapshot = mission_snapshot_from_transport_event(cast(TransportEvent, first.message))
        first.ack()

        coordination.publish_source_fact("environment_data", 3, reference="scene-3")
        assert _deliver(coordination, source_consumer) is None
        assert output_consumer.receive() is None

        transport.publish_event(
            coordination.input_topic,
            TransportEvent(
                1,
                "scene-health-only",
                "mission-context",
                2,
                "source-health",
                {"source": "environment_data", "health": "degraded"},
            ),
        )
        changed = _deliver(coordination, source_consumer)
        assert changed is not None
        assert changed.version == first_snapshot.version + 1
        assert changed.source_health["environment_data"] == "degraded"
        assert changed.source_freshness["environment_data"] is True


def test_environment_driven_snapshot_uses_latest_environment_for_active_maneuver() -> None:
    input_subscription = Subscription(
        "context-coordination", "mission-context", "planning-evidence"
    )
    transport = InProcessTransport((input_subscription,))
    environment = type(
        "EnvironmentSource",
        (),
        {
            "update_ownership": "environment_driven",
            "has_current_maneuver": True,
        },
    )()
    coordination = ContextCoordination(
        transport,
        "mission-context",
        input_topic="planning-evidence",
        subscription=input_subscription,
        environment_update_source=cast(Any, environment),
    )

    with transport.open_consumer(input_subscription) as consumer:
        coordination.publish_source_fact(
            "environment_data", 1, reference="environment-data:mission-context:1"
        )
        assert _deliver(coordination, consumer) is not None
        coordination.publish_source_fact(
            "active_maneuver", 1, reference="environment-data:mission-context:1"
        )
        assert _deliver(coordination, consumer) is not None
        coordination.publish_source_fact(
            "environment_data", 2, reference="environment-data:mission-context:2"
        )
        snapshot = _deliver(coordination, consumer)

    assert snapshot is not None
    assert snapshot.environment_data == "environment-data:mission-context:2"
    assert snapshot.active_maneuver == snapshot.environment_data
    assert snapshot.source_revisions["active_maneuver"] == 2


def test_context_coordination_restores_latest_snapshot_and_preserves_reference_on_health_only_update() -> None:
    input_subscription = Subscription(
        "context-coordination", "mission-context", "normalized-plans"
    )
    output_subscription = Subscription(
        "snapshot-reader", "mission-context", "mission-snapshots"
    )
    state = InProcessTransportState()
    first_transport = InProcessTransport(
        (input_subscription, output_subscription), state=state
    )
    first = ContextCoordination(first_transport, "mission-context")
    plan_event = create_normalized_plan_transport_event(
        _plan(5), event_id="plan-event-5", sequence=0
    )
    first_transport.publish_event(
        first.input_topic, normalized_plan_transport_event_to_wire(plan_event)
    )
    first_source = first_transport.open_consumer(first.subscription)
    assert first.run_once(first_source) is not None
    first.publish_source_fact(
        "environment_data", 7, reference="scene-7", health="healthy"
    )
    assert first.run_once(first_source) is not None
    first_source.close()

    restarted_transport = InProcessTransport(
        (input_subscription, output_subscription), state=state
    )
    restarted = ContextCoordination(restarted_transport, "mission-context")
    restarted.publish_source_fact(
        "environment_data", 7, health="degraded"
    )
    restarted_source = restarted_transport.open_consumer(restarted.subscription)
    changed = restarted.run_once(restarted_source)
    assert changed is not None
    assert changed.version == 3
    assert changed.plan_revision == 5
    assert changed.source_revisions["environment_data"] == 7
    assert changed.source_references["environment_data"] == "scene-7"
    assert changed.source_health["environment_data"] == "degraded"
    latest = restarted_transport.latest_event(
        restarted.snapshot_topic, "mission-context", event_kind="mission-snapshot"
    )
    assert latest is not None
    assert latest.event_id == "mission-snapshot:mission-context:3"

    restarted.publish_source_fact(
        "environment_data", 6, reference="old-scene", health="healthy"
    )
    assert restarted.run_once(restarted_source) is None
    restarted_source.close()


def test_context_coordination_rejects_out_of_order_plan_revision() -> None:
    subscription = Subscription("context-coordination", "mission-context", "normalized-plans")
    transport = InProcessTransport((subscription,))
    coordination = ContextCoordination(transport, "mission-context")
    source_consumer = transport.open_consumer(coordination.subscription)
    for revision, event_id, sequence in ((5, "plan-5", 0), (3, "plan-3", 1)):
        event = create_normalized_plan_transport_event(
            _plan(revision), event_id=event_id, sequence=sequence
        )
        transport.publish_event(coordination.input_topic, normalized_plan_transport_event_to_wire(event))
    assert coordination.run_once(source_consumer) is not None
    assert coordination.run_once(source_consumer) is None
    source_consumer.close()
    latest = transport.latest_event(coordination.snapshot_topic, "mission-context")
    assert latest is not None
    assert mission_snapshot_from_transport_event(latest).version == 1


def test_context_coordination_nacks_malformed_relevant_events_until_dead_letter() -> None:
    subscription = Subscription(
        "context-coordination", "mission-context", "normalized-plans", max_retries=2
    )
    transport = InProcessTransport((subscription,), max_retries=2)
    coordination = ContextCoordination(transport, "mission-context", max_retries=2)
    malformed = TransportEvent(
        1, "malformed-plan", "mission-context", 0, "normalized-plan", {}
    )
    transport.publish_event(coordination.input_topic, malformed)
    source_consumer = transport.open_consumer(coordination.subscription)
    assert coordination.run_once(source_consumer) is None
    assert coordination.run_once(source_consumer) is None
    assert coordination.run_once(source_consumer) is None
    assert len(transport.get_dead_letters(subscription)) == 1
    malformed_source = TransportEvent(
        1,
        "malformed-source",
        "mission-context",
        1,
        "source-fact",
        {"source": "environment_data", "health": "healthy"},
    )
    transport.publish_event(coordination.input_topic, malformed_source)
    assert coordination.run_once(source_consumer) is None
    assert coordination.run_once(source_consumer) is None
    assert coordination.run_once(source_consumer) is None
    assert len(transport.get_dead_letters(subscription)) == 2
    source_consumer.close()


def test_context_coordination_restores_latest_snapshot_from_file_transport(tmp_path: Any) -> None:
    input_subscription = Subscription(
        "context-coordination", "mission-context", "normalized-plans"
    )
    output_subscription = Subscription(
        "snapshot-reader", "mission-context", "mission-snapshots"
    )
    first_transport = FileTransport(tmp_path, (input_subscription, output_subscription))
    first = ContextCoordination(first_transport, "mission-context")
    plan_event = create_normalized_plan_transport_event(
        _plan(2), event_id="file-plan-2", sequence=0
    )
    first_transport.publish_event(
        first.input_topic, normalized_plan_transport_event_to_wire(plan_event)
    )
    first.publish_source_fact("fsm_status", 4, reference="fsm-4")
    first_consumer = first_transport.open_consumer(first.subscription)
    assert first.run_once(first_consumer) is not None
    assert first.run_once(first_consumer) is not None
    first_consumer.close()

    restarted_transport = FileTransport(tmp_path, (input_subscription, output_subscription))
    restarted = ContextCoordination(restarted_transport, "mission-context")
    restarted.publish_source_fact("fsm_status", 4, health="degraded")
    restarted_consumer = restarted_transport.open_consumer(restarted.subscription)
    changed = restarted.run_once(restarted_consumer)
    assert changed is not None
    assert changed.version == 3
    assert changed.source_references["fsm_status"] == "fsm-4"
    restarted_consumer.close()


def test_belief_updated_is_reference_only_and_publishes_only_changed_fact() -> None:
    input_subscription = Subscription(
        "context-coordination", "mission-context", "normalized-plans"
    )
    output_subscription = Subscription(
        "snapshot-reader", "mission-context", "mission-snapshots"
    )
    transport = InProcessTransport((input_subscription, output_subscription))
    coordination = ContextCoordination(transport, "mission-context", clock=lambda: "t-belief")
    content_hash = "a" * 64
    payload = {
        "source": "bayesian_belief_snapshot",
        "revision": 1,
        "reference": (
            "bayesian-beliefs/mission-context/belief-v1-current.json"
            f"#sha256={content_hash}"
        ),
        "content_sha256": content_hash,
        "health": "healthy",
        "fresh": True,
    }
    for sequence in (0, 1):
        transport.publish_event(
            coordination.input_topic,
            TransportEvent(
                1,
                f"belief-{sequence}",
                "mission-context",
                sequence,
                "belief.updated",
                payload,
            ),
        )

    with transport.open_consumer(coordination.subscription) as consumer:
        first = coordination.run_once(consumer)
        unchanged = coordination.run_once(consumer)

    assert first is not None
    assert first.bayesian_belief_snapshot == payload["reference"]
    assert first.source_revisions["bayesian_belief_snapshot"] == 1
    assert first.source_health["bayesian_belief_snapshot"] == "healthy"
    assert first.source_freshness["bayesian_belief_snapshot"] is True
    assert "source_hashes" not in first.to_dict()
    assert unchanged is None
    assert transport.next_event_sequence("mission-snapshots", "mission-context") == 1


def test_equal_belief_revision_rejects_conflicting_provenance() -> None:
    subscription = Subscription(
        "context-coordination", "mission-context", "normalized-plans"
    )
    transport = InProcessTransport((subscription,))
    coordination = ContextCoordination(transport, "mission-context")

    def belief_event(
        sequence: int, content_hash: str, health: str = "healthy"
    ) -> TransportEvent:
        return TransportEvent(
            1,
            f"belief-{sequence}",
            "mission-context",
            sequence,
            "belief.updated",
            {
                "source": "bayesian_belief_snapshot",
                "revision": 1,
                "reference": (
                    "bayesian-beliefs/mission-context/belief-v1-current.json"
                    f"#sha256={content_hash}"
                ),
                "content_sha256": content_hash,
                "health": health,
                "fresh": True,
            },
        )

    transport.publish_event(coordination.input_topic, belief_event(0, "a" * 64))
    transport.publish_event(
        coordination.input_topic, belief_event(1, "a" * 64, "degraded")
    )
    transport.publish_event(coordination.input_topic, belief_event(2, "b" * 64))
    with transport.open_consumer(coordination.subscription) as consumer:
        accepted = coordination.run_once(consumer)
        health_changed = coordination.run_once(consumer)
        rejected = coordination.run_once(consumer)

    assert accepted is not None
    assert health_changed is not None
    assert health_changed.source_health["bayesian_belief_snapshot"] == "degraded"
    assert rejected is None
    latest = transport.latest_event(
        coordination.snapshot_topic,
        "mission-context",
        event_kind="mission-snapshot",
    )
    assert latest is not None
    assert mission_snapshot_from_transport_event(latest) == health_changed
    assert transport.next_event_sequence("mission-snapshots", "mission-context") == 2
