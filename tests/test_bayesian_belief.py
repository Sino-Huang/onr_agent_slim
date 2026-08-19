from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from onr.adapters.bayesian_belief_store import (
    BayesianBeliefStoreError,
    FileBayesianBeliefStore,
)
from onr.application.bayesian_belief import (
    BayesianBeliefManager,
    BayesianBeliefService,
    create_belief_constraint_event,
    create_risk_observation_event,
)
from onr.contracts.bayesian_belief import (
    BayesianBeliefSnapshot,
    BeliefKey,
    EntityAssociation,
    ForbiddenBeliefCombination,
    RiskAssignment,
    RiskObservation,
)
from onr.contracts.transport import TransportEvent


CREATED_AT = "2026-08-19T12:00:00+00:00"


def _observation(
    event_id: str,
    revision: int,
    risk_type: str,
    associations: tuple[tuple[str, float], ...],
    likelihood_given_risk: float,
    likelihood_given_safe: float,
) -> RiskObservation:
    return RiskObservation(
        event_id=event_id,
        input_revision=revision,
        risk_type=risk_type,
        associations=tuple(EntityAssociation(*item) for item in associations),
        likelihood_given_risk=likelihood_given_risk,
        likelihood_given_safe=likelihood_given_safe,
    )


def _manager(seed: int = 17) -> BayesianBeliefManager:
    return BayesianBeliefManager(
        "mission-generic",
        (
            BeliefKey("entity-beta", "collision"),
            BeliefKey("entity-alpha", "rule-compliance"),
            BeliefKey("entity-alpha", "collision"),
        ),
        particle_count=512,
        seed=seed,
    )


def test_seeded_generic_updates_are_deterministic_across_two_risk_types() -> None:
    left = _manager()
    right = _manager()
    observations = (
        _observation(
            "event-collision",
            4,
            "collision",
            (("entity-alpha", 0.8), ("entity-beta", 0.2)),
            0.9,
            0.1,
        ),
        _observation(
            "event-compliance",
            5,
            "rule-compliance",
            (("entity-alpha", 1.0),),
            0.2,
            0.8,
        ),
    )

    left_snapshots = tuple(left.update(item, created_at=CREATED_AT) for item in observations)
    right_snapshots = tuple(right.update(item, created_at=CREATED_AT) for item in observations)

    assert [item.to_dict() for item in left_snapshots] == [
        item.to_dict() for item in right_snapshots
    ]
    final = left_snapshots[-1]
    assert final.belief_revision == 2
    assert final.input_event_id == "event-compliance"
    assert [item.key for item in final.marginals] == sorted(item.key for item in final.marginals)
    probabilities = {item.key: item.probability_risk for item in final.marginals}
    assert probabilities[BeliefKey("entity-alpha", "collision")] > probabilities[
        BeliefKey("entity-beta", "collision")
    ]
    assert probabilities[BeliefKey("entity-alpha", "rule-compliance")] < 0.5


def test_constraints_are_enforced_and_impossible_constraints_are_rejected() -> None:
    alpha = BeliefKey("alpha", "location-reporting")
    beta = BeliefKey("beta", "location-reporting")
    forbid_both_safe = ForbiddenBeliefCombination(
        "at-least-one-risk",
        (RiskAssignment(alpha, False), RiskAssignment(beta, False)),
    )
    manager = BayesianBeliefManager(
        "mission-constraints",
        (alpha, beta),
        constraints=(forbid_both_safe,),
        particle_count=256,
        transition_probability=1.0,
        seed=3,
    )
    manager.update(
        _observation("neutral", 1, "location-reporting", (("alpha", 1.0),), 0.5, 0.5),
        created_at=CREATED_AT,
    )
    assert all(any(particle) for particle in manager.checkpoint().particles)

    only_key = BeliefKey("entity", "collision")
    impossible = (
        ForbiddenBeliefCombination("forbid-safe", (RiskAssignment(only_key, False),)),
        ForbiddenBeliefCombination("forbid-risk", (RiskAssignment(only_key, True),)),
    )
    with pytest.raises(ValueError, match="every possible"):
        BayesianBeliefManager("mission-impossible", (only_key,), constraints=impossible, seed=1)

    undeclared = ForbiddenBeliefCombination(
        "unknown-key",
        (RiskAssignment(BeliefKey("other", "collision"), True),),
    )
    with pytest.raises(ValueError, match="undeclared"):
        BayesianBeliefManager("mission-invalid", (only_key,), constraints=(undeclared,), seed=1)


def test_rejected_observation_does_not_advance_or_mutate_filter() -> None:
    manager = BayesianBeliefManager(
        "mission-reject",
        (BeliefKey("entity", "collision"),),
        particle_count=32,
        seed=9,
    )
    before = manager.checkpoint().to_dict()
    with pytest.raises(ValueError, match="zero likelihood"):
        manager.update(
            _observation("impossible", 1, "collision", (("entity", 1.0),), 0.0, 0.0),
            created_at=CREATED_AT,
        )
    assert manager.checkpoint().to_dict() == before


def test_snapshot_round_trip_and_hash_verification() -> None:
    snapshot = _manager().update(
        _observation("event", 1, "collision", (("entity-alpha", 1.0),), 0.8, 0.2),
        created_at=CREATED_AT,
    )
    restored = BayesianBeliefSnapshot.from_json(snapshot.to_canonical_json())
    assert restored == snapshot
    assert json.loads(restored.to_canonical_json()) == restored.to_dict()

    tampered = restored.to_dict()
    cast(list[dict[str, Any]], tampered["marginals"])[0]["probability_risk"] = 0.0
    with pytest.raises(ValueError, match="does not match"):
        BayesianBeliefSnapshot.from_dict(tampered)

    malformed = restored.to_dict()
    malformed["unexpected"] = True
    with pytest.raises(ValueError, match="unknown or missing"):
        BayesianBeliefSnapshot.from_dict(malformed)


def test_store_rejects_malformed_hash_mismatch_and_invalid_paths(tmp_path: Path) -> None:
    store = FileBayesianBeliefStore(tmp_path)
    current = store.current_path("mission-corrupt")
    current.parent.mkdir(parents=True)
    current.write_text("{not-json", encoding="utf-8")
    with pytest.raises(BayesianBeliefStoreError, match="corrupt"):
        store.load_current("mission-corrupt")

    other_store = FileBayesianBeliefStore(tmp_path / "other")
    snapshot = _manager().update(
        _observation("event", 1, "collision", (("entity-alpha", 1.0),), 0.8, 0.2),
        created_at=CREATED_AT,
    )
    path = other_store.current_path(snapshot.mission_id)
    path.parent.mkdir(parents=True)
    mismatched = snapshot.to_dict()
    mismatched["content_sha256"] = "0" * 64
    path.write_text(json.dumps(mismatched), encoding="utf-8")
    with pytest.raises(BayesianBeliefStoreError, match="corrupt"):
        other_store.load_current(snapshot.mission_id)

    for invalid in ("../escape", "two/parts", " two-spaces ", ""):
        with pytest.raises(ValueError):
            store.load_current(invalid)


def test_store_restart_checkpoint_and_bounded_history(tmp_path: Path) -> None:
    store = FileBayesianBeliefStore(tmp_path, history_limit=2)
    manager = _manager(seed=23)
    first = manager.update(
        _observation("event-1", 1, "collision", (("entity-alpha", 1.0),), 0.8, 0.2),
        created_at=CREATED_AT,
    )
    artifact_path = store.save(first, manager.checkpoint())
    assert artifact_path == store.current_path(first.mission_id)

    loaded_checkpoint = store.load_checkpoint(first.mission_id)
    assert loaded_checkpoint is not None
    restarted = BayesianBeliefManager.from_checkpoint(loaded_checkpoint)
    next_observation = _observation(
        "event-2",
        2,
        "rule-compliance",
        (("entity-alpha", 1.0),),
        0.7,
        0.3,
    )
    expected = manager.update(next_observation, created_at=CREATED_AT)
    actual = restarted.update(next_observation, created_at=CREATED_AT)
    assert actual == expected
    store.save(actual, restarted.checkpoint())

    for revision in (3, 4):
        snapshot = restarted.update(
            _observation(
                f"event-{revision}",
                revision,
                "collision",
                (("entity-beta", 1.0),),
                0.6,
                0.4,
            ),
            created_at=CREATED_AT,
        )
        store.save(snapshot, restarted.checkpoint())

    loaded_checkpoint = store.load_checkpoint(first.mission_id)
    assert loaded_checkpoint is not None
    reloaded = BayesianBeliefManager.from_checkpoint(loaded_checkpoint)
    assert reloaded.belief_revision == 4
    mission_root = store.mission_root(first.mission_id)
    assert len(tuple((mission_root / "generations").glob("[0-9]*"))) == 2
    assert store.load_revision(first.mission_id, 1) is None
    assert store.load_revision(first.mission_id, 4) == store.load_current(first.mission_id)


def test_read_only_current_load_validates_without_pruning_partial_generation(
    tmp_path: Path,
) -> None:
    store = FileBayesianBeliefStore(tmp_path)
    manager = _manager(seed=29)
    snapshot = manager.update(
        _observation(
            "event-read-only",
            1,
            "collision",
            (("entity-alpha", 1.0),),
            0.8,
            0.2,
        ),
        created_at=CREATED_AT,
    )
    store.save(snapshot, manager.checkpoint())
    committed = json.loads(
        store.current_path(snapshot.mission_id).read_text(encoding="utf-8")
    )["generation"]
    partial = (
        store.mission_root(snapshot.mission_id)
        / "generations"
        / f"{committed + 1:020d}"
    )
    partial.mkdir()
    private = partial / "private-partial.json"
    private.write_text('{"private":true}', encoding="utf-8")

    assert store.load_current_read_only(snapshot.mission_id) == snapshot
    assert private.is_file()

    assert store.load_current(snapshot.mission_id) == snapshot
    assert not partial.exists()


def test_event_service_stores_before_reference_only_publication(tmp_path: Path) -> None:
    order: list[str] = []

    class RecordingStore(FileBayesianBeliefStore):
        def commit_update(self, snapshot, checkpoint, **kwargs):  # type: ignore[no-untyped-def]
            order.append("store")
            return super().commit_update(snapshot, checkpoint, **kwargs)

    class RecordingTransport:
        def __init__(self) -> None:
            self.events = []

        def next_event_sequence(self, topic: str, mission_id: str) -> int:
            _ = topic, mission_id
            return len(self.events)

        def publish_event(self, topic: str, event):  # type: ignore[no-untyped-def]
            order.append("publish")
            self.events.append((topic, event))
            return event

    alpha = BeliefKey("alpha", "collision")
    beta = BeliefKey("beta", "collision")
    manager = BayesianBeliefManager(
        "mission-events", (alpha, beta), particle_count=64, seed=4
    )
    transport = RecordingTransport()
    service = BayesianBeliefService(
        manager,
        RecordingStore(tmp_path),
        transport,
        clock=lambda: CREATED_AT,
    )
    constraint = ForbiddenBeliefCombination(
        "not-both-safe",
        (RiskAssignment(alpha, False), RiskAssignment(beta, False)),
    )
    service.handle(
        create_belief_constraint_event(
            "mission-events",
            (constraint,),
            input_revision=1,
            event_id="constraints-1",
            sequence=0,
        )
    )
    observation = _observation(
        "observation-2", 2, "collision", (("alpha", 1.0),), 0.8, 0.2
    )
    snapshot = service.handle(
        create_risk_observation_event("mission-events", observation, sequence=1)
    )

    assert snapshot is not None
    assert order == ["store", "publish"]
    topic, event = transport.events[0]
    assert topic == "normalized-plans"
    assert event.event_kind == "belief.updated"
    assert set(event.payload) == {
        "source",
        "revision",
        "reference",
        "content_sha256",
        "health",
        "fresh",
    }
    assert "marginals" not in event.payload
    assert event.payload["reference"].endswith(
        f"#sha256={snapshot.content_sha256}"
    )
    assert all(any(particle) for particle in service.manager.checkpoint().particles)


def test_pending_output_recovers_before_redelivery_ack(tmp_path: Path) -> None:
    order: list[str] = []

    class RecoveringTransport:
        def __init__(self) -> None:
            self.fail = True
            self.events = {}

        def next_event_sequence(self, topic: str, mission_id: str) -> int:
            _ = topic, mission_id
            return len(self.events)

        def publish_event(self, topic: str, event):  # type: ignore[no-untyped-def]
            _ = topic
            order.append("publish")
            if self.fail:
                raise RuntimeError("injected publication failure")
            self.events[event.event_id] = event
            return event

        def get_event(self, event_id: str):  # type: ignore[no-untyped-def]
            return self.events.get(event_id)

    class Delivery:
        def __init__(self, message) -> None:  # type: ignore[no-untyped-def]
            self.message = message

        def ack(self) -> None:
            order.append("ack")

        def nack(self) -> None:
            order.append("nack")

    class Consumer:
        def __init__(self, delivery: Delivery) -> None:
            self.delivery = delivery

        def receive(self):  # type: ignore[no-untyped-def]
            delivery, self.delivery = self.delivery, None  # type: ignore[assignment]
            return delivery

    mission_id = "mission-recovery"
    store = FileBayesianBeliefStore(tmp_path)
    transport = RecoveringTransport()
    manager = BayesianBeliefManager(
        mission_id, (BeliefKey("entity", "collision"),), particle_count=64, seed=8
    )
    service = BayesianBeliefService(
        manager, store, transport, clock=lambda: CREATED_AT
    )
    observation = _observation(
        "observation-1", 1, "collision", (("entity", 1.0),), 0.8, 0.2
    )
    event = create_risk_observation_event(mission_id, observation, sequence=0)

    with pytest.raises(RuntimeError, match="publication failure"):
        service.handle(event)

    assert store.load_current(mission_id) is not None
    pending = store.load_pending_output(mission_id)
    assert pending is not None and "sequence" not in pending
    checkpoint = store.load_checkpoint(mission_id)
    assert checkpoint is not None
    restarted = BayesianBeliefService(
        BayesianBeliefManager.from_checkpoint(checkpoint),
        store,
        transport,
        clock=lambda: CREATED_AT,
    )
    transport.fail = False
    result = restarted.run_once(Consumer(Delivery(event)))

    assert result == store.load_current(mission_id)
    assert store.load_pending_output(mission_id) is None
    assert list(transport.events) == ["belief.updated:mission-recovery:1"]
    assert order[-2:] == ["publish", "ack"]


def test_constraint_only_checkpoint_survives_restart(tmp_path: Path) -> None:
    mission_id = "mission-constraint-restart"
    key = BeliefKey("entity", "collision")
    constraint = ForbiddenBeliefCombination(
        "must-be-risk", (RiskAssignment(key, False),)
    )
    store = FileBayesianBeliefStore(tmp_path)

    class QuietTransport:
        def get_event(self, event_id: str):
            _ = event_id
            return None

    service = BayesianBeliefService(
        BayesianBeliefManager(mission_id, (key,), particle_count=32, seed=2),
        store,
        QuietTransport(),
    )
    service.handle(
        create_belief_constraint_event(
            mission_id,
            (constraint,),
            input_revision=7,
            event_id="constraint-7",
            sequence=0,
        )
    )

    assert store.load_current(mission_id) is None
    checkpoint = store.load_checkpoint(mission_id)
    assert checkpoint is not None
    restarted = BayesianBeliefManager.from_checkpoint(checkpoint)
    assert restarted.constraints == (constraint,)
    assert restarted.last_input_event_id == "constraint-7"
    assert restarted.last_input_revision == 7
    assert all(particle == (True,) for particle in restarted.checkpoint().particles)


def test_encoded_mission_reference_resolves_and_fixed_clock_stabilizes_hash(
    tmp_path: Path,
) -> None:
    mission_id = "mission:demo"

    class CapturingTransport:
        def __init__(self) -> None:
            self.events = {}

        def next_event_sequence(self, topic: str, mission_id: str) -> int:
            _ = topic, mission_id
            return 0

        def publish_event(self, topic: str, event):  # type: ignore[no-untyped-def]
            _ = topic
            self.events[event.event_id] = event
            return event

        def get_event(self, event_id: str):
            return self.events.get(event_id)

    hashes: list[str] = []
    for index in range(2):
        store = FileBayesianBeliefStore(tmp_path / str(index))
        transport = CapturingTransport()
        service = BayesianBeliefService(
            BayesianBeliefManager(
                mission_id,
                (BeliefKey("entity", "collision"),),
                particle_count=64,
                seed=11,
            ),
            store,
            transport,
            clock=lambda: CREATED_AT,
        )
        observation = _observation(
            "observation-1", 1, "collision", (("entity", 1.0),), 0.9, 0.1
        )
        snapshot = service.handle(
            create_risk_observation_event(mission_id, observation, sequence=0)
        )
        assert snapshot is not None
        event = transport.events[f"belief.updated:{mission_id}:1"]
        reference = event.payload["reference"]
        assert isinstance(reference, str) and "mission%3Ademo" in reference
        assert store.current_path(mission_id).parent.name == "mission%3Ademo"
        relative_reference = reference.partition("#sha256=")[0]
        artifact_path = store.storage_root / relative_reference
        assert artifact_path.is_file() and not artifact_path.is_symlink()
        assert json.loads(artifact_path.read_text(encoding="utf-8")) == snapshot.to_dict()
        committed = json.loads(
            store.current_path(mission_id).read_text(encoding="utf-8")
        )
        assert committed["snapshot_path"] == relative_reference
        assert committed["snapshot_sha256"] == snapshot.content_sha256
        assert committed["belief_revision"] == snapshot.belief_revision
        generation_artifact = (
            store.mission_root(mission_id)
            / "generations"
            / f"{committed['generation']:020d}"
            / "belief-v1.json"
        )
        assert json.loads(generation_artifact.read_text(encoding="utf-8")) == snapshot.to_dict()
        assert store.load_reference(
            mission_id, reference, snapshot.content_sha256
        ) == snapshot
        with pytest.raises(BayesianBeliefStoreError):
            store.load_reference(
                mission_id,
                reference.replace(snapshot.content_sha256, "f" * 64),
                snapshot.content_sha256,
            )
        with pytest.raises(BayesianBeliefStoreError):
            store.load_reference(
                mission_id,
                f"../outside.json#sha256={snapshot.content_sha256}",
                snapshot.content_sha256,
            )
        hashes.append(snapshot.content_sha256)

    assert hashes[0] == hashes[1]


@pytest.mark.parametrize(
    "fault_boundary", ("snapshot", "artifact", "checkpoint", "pending", "commit")
)
def test_generation_commit_recovers_at_every_write_boundary(
    tmp_path: Path, fault_boundary: str
) -> None:
    mission_id = f"mission-fault-{fault_boundary}"
    key = BeliefKey("entity", "collision")
    manager = BayesianBeliefManager(mission_id, (key,), particle_count=64, seed=21)
    first = manager.update(
        _observation("observation-1", 1, "collision", (("entity", 1.0),), 0.8, 0.2),
        created_at=CREATED_AT,
    )
    FileBayesianBeliefStore(tmp_path).save(first, manager.checkpoint())

    injected = False

    def fail_once(boundary: str) -> None:
        nonlocal injected
        if boundary == fault_boundary and not injected:
            injected = True
            raise RuntimeError(f"fault after {boundary}")

    class CapturingTransport:
        def __init__(self) -> None:
            self.events: dict[str, TransportEvent] = {}

        def next_event_sequence(self, topic: str, selected_mission: str) -> int:
            _ = topic, selected_mission
            return len(self.events)

        def publish_event(self, topic: str, event: TransportEvent) -> TransportEvent:
            _ = topic
            self.events[event.event_id] = event
            return event

        def get_event(self, event_id: str) -> TransportEvent | None:
            return self.events.get(event_id)

    transport = CapturingTransport()
    faulting_store = FileBayesianBeliefStore(tmp_path, fault_injector=fail_once)
    service = BayesianBeliefService(
        manager, faulting_store, transport, clock=lambda: CREATED_AT
    )
    second_observation = _observation(
        "observation-2", 2, "collision", (("entity", 1.0),), 0.7, 0.3
    )
    event = create_risk_observation_event(
        mission_id, second_observation, sequence=1
    )

    with pytest.raises(RuntimeError, match=f"fault after {fault_boundary}"):
        service.handle(event)

    restart_store = FileBayesianBeliefStore(tmp_path)
    checkpoint = restart_store.load_checkpoint(mission_id)
    assert checkpoint is not None
    assert checkpoint.belief_revision == (2 if fault_boundary == "commit" else 1)
    restarted = BayesianBeliefService(
        BayesianBeliefManager.from_checkpoint(checkpoint),
        restart_store,
        transport,
        clock=lambda: CREATED_AT,
    )
    recovered = restarted.handle(event)

    assert recovered is not None and recovered.belief_revision == 2
    assert restart_store.load_current(mission_id) == recovered
    assert restart_store.load_pending_output(mission_id) is None
    assert list(transport.events) == [f"belief.updated:{mission_id}:2"]


def test_pending_publication_rebases_after_competing_context_sequence(
    tmp_path: Path,
) -> None:
    mission_id = "mission-sequence-rebase"

    class CompetingTransport:
        def __init__(self) -> None:
            self.events: list[TransportEvent] = []
            self.identities: dict[str, TransportEvent] = {}
            self.compete = True

        def next_event_sequence(self, topic: str, selected_mission: str) -> int:
            _ = topic, selected_mission
            return max((event.sequence for event in self.events), default=-1) + 1

        def publish_event(self, topic: str, event: TransportEvent) -> TransportEvent:
            _ = topic
            if self.compete:
                self.compete = False
                competitor = TransportEvent(
                    1,
                    "competing-context-fact",
                    mission_id,
                    event.sequence,
                    "source-fact",
                    {"source": "fsm_status", "revision": 1},
                )
                self.events.append(competitor)
                self.identities[competitor.event_id] = competitor
                raise ValueError("event sequence conflicts with existing content")
            self.events.append(event)
            self.identities[event.event_id] = event
            return event

        def get_event(self, event_id: str) -> TransportEvent | None:
            return self.identities.get(event_id)

    store = FileBayesianBeliefStore(tmp_path)
    transport = CompetingTransport()
    service = BayesianBeliefService(
        BayesianBeliefManager(
            mission_id,
            (BeliefKey("entity", "collision"),),
            particle_count=64,
            seed=9,
        ),
        store,
        transport,
        clock=lambda: CREATED_AT,
    )
    observation = _observation(
        "observation-1", 1, "collision", (("entity", 1.0),), 0.8, 0.2
    )

    snapshot = service.handle(
        create_risk_observation_event(mission_id, observation, sequence=0)
    )

    assert snapshot is not None
    belief_event = transport.identities[f"belief.updated:{mission_id}:1"]
    assert belief_event.sequence == 1
    assert store.load_pending_output(mission_id) is None


def test_store_rejects_write_through_preexisting_symlink(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    outside = tmp_path / "outside"
    storage_root.mkdir()
    outside.mkdir()
    (storage_root / "bayesian-beliefs").symlink_to(
        outside, target_is_directory=True
    )
    manager = BayesianBeliefManager(
        "mission-symlink",
        (BeliefKey("entity", "collision"),),
        particle_count=32,
        seed=3,
    )
    snapshot = manager.update(
        _observation("observation-1", 1, "collision", (("entity", 1.0),), 0.8, 0.2),
        created_at=CREATED_AT,
    )

    with pytest.raises(BayesianBeliefStoreError, match="symlink"):
        FileBayesianBeliefStore(storage_root).save(snapshot, manager.checkpoint())

    assert tuple(outside.iterdir()) == ()
