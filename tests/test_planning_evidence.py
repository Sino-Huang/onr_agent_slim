import hashlib
from types import MappingProxyType
from typing import cast

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.operational_log import InProcessOperationalLog
from onr.application.hyper_agent import HyperAgent, PlanningHeartbeatOutcome
from onr.contracts import PlanningIntent
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import PlannerChoice
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
    TranslationAttemptOutcome,
)
from onr.contracts.transport import TransportEvent
from onr.ports.transport import Subscription


def _mission_input() -> MissionInput:
    return MissionInput(
        "mission-1",
        "Observe the highest-risk ships at their reported event times.",
        "mission-control",
    )


def _planning_intent(mission_input: MissionInput) -> PlanningIntent:
    return PlanningIntent(
        mission_id=mission_input.mission_id,
        source_authority=mission_input.source_authority,
        objective="Maximize risk-weighted ship observation coverage",
        rationale="Event times, travel, and field of view make this temporal optimization.",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        mission_input_sha256=hashlib.sha256(
            mission_input.to_canonical_json().encode("utf-8")
        ).hexdigest(),
        details={"risk_source": "operational_scene_graph"},
    )


def _scene_snapshot(
    mission_id: str = "mission-1",
) -> tuple[MissionSnapshot, TransportEvent]:
    event_id = f"scene:{mission_id}:7"
    scene = TransportEvent(
        schema_version=1,
        event_id=event_id,
        mission_id=mission_id,
        sequence=0,
        event_kind="operational_scene_graph",
        payload={"graph": {"mission_id": mission_id, "entities": []}},
    )
    snapshot = MissionSnapshot(
        mission_id=mission_id,
        version=7,
        created_at="time-7",
        operational_scene_graph=event_id,
        source_revisions={"operational_scene_graph": 7},
        source_health={"operational_scene_graph": "healthy"},
        source_freshness={"operational_scene_graph": True},
    )
    return snapshot, scene


def _attempt(
    choice: PlannerChoiceRecord,
    *,
    attempt_id: str,
    outcome: str,
    mission_snapshot_id: str | None = None,
    asset_references: dict[str, str] | None = None,
    asset_sha256: dict[str, str] | None = None,
) -> PlannerGenerationAttempt:
    return PlannerGenerationAttempt(
        attempt_id=attempt_id,
        decision_id=choice.decision_id,
        mission_id=choice.mission_id,
        mission_input_sha256=choice.mission_input_sha256,
        planning_intent_sha256=choice.planning_intent_sha256,
        planner_choice=choice.planner_choice,
        rationale=choice.rationale,
        mission_snapshot_id=(
            mission_snapshot_id
            if mission_snapshot_id is not None
            else f"{choice.mission_id}:snapshot:7"
        ),
        translator_id="hyper-minizinc",
        translator_version="1.0.0",
        outcome=outcome,
        asset_references={} if asset_references is None else asset_references,
        asset_sha256={} if asset_sha256 is None else asset_sha256,
    )


def test_hyper_records_planner_choice_without_creating_a_mission_spec() -> None:
    mission_input = _mission_input()
    intent = _planning_intent(mission_input)
    transport = InProcessTransport()
    operational_log = InProcessOperationalLog()
    hyper = HyperAgent(
        lambda _: intent,
        transport=transport,
        operational_log=operational_log,
    )

    choice = hyper.choose_planner(mission_input)

    assert isinstance(choice, PlannerChoiceRecord)
    assert choice.mission_input_sha256 == intent.mission_input_sha256
    assert choice.planner_choice == PlannerChoice("temporal", "minizinc")
    assert choice.rationale == intent.rationale
    assert hyper.planner_choice("mission-1") == choice
    assert hyper.authority("mission-1") is None

    event = transport.latest_event("planning-evidence", "mission-1")
    assert event is not None
    assert event.event_kind == "planner-choice"
    assert PlannerChoiceRecord.from_dict(event.payload) == choice
    records = operational_log.replay("mission-1")
    assert records[-1].event_kind == "planner-choice"
    assert records[-1].details["decision_id"] == choice.decision_id


def test_hyper_records_distinguishable_immutable_generation_attempts() -> None:
    mission_input = _mission_input()
    intent = _planning_intent(mission_input)
    snapshot, scene = _scene_snapshot()
    subscription = Subscription(
        "planning-evidence-reader", "mission-1", "planning-evidence"
    )
    transport = InProcessTransport((subscription,))
    hyper = HyperAgent(lambda _: intent, transport=transport)
    choice = hyper.choose_planner(mission_input)
    rejected = _attempt(
        choice,
        attempt_id="attempt-1",
        outcome="rejected",
        asset_references={"model.mzn": "artifacts/attempt-1/model.mzn"},
        asset_sha256={"model.mzn": "a" * 64},
    )
    accepted = _attempt(
        choice,
        attempt_id="attempt-2",
        outcome="accepted",
        asset_references={
            "model.mzn": "artifacts/attempt-2/model.mzn",
            "data.dzn": "artifacts/attempt-2/data.dzn",
        },
        asset_sha256={"model.mzn": "b" * 64, "data.dzn": "c" * 64},
    )

    rejected_result = hyper.planning_heartbeat(
        mission_input, snapshot, scene, lambda *_: rejected
    )
    accepted_result = hyper.planning_heartbeat(
        mission_input, snapshot, scene, lambda *_: accepted
    )

    assert rejected_result.attempt is not None
    assert accepted_result.attempt is not None
    assert rejected_result.attempt.outcome is TranslationAttemptOutcome.REJECTED
    assert accepted_result.attempt.outcome is TranslationAttemptOutcome.ACCEPTED
    assert rejected.decision_id == accepted.decision_id == choice.decision_id
    assert rejected.mission_input_sha256 == intent.mission_input_sha256
    assert accepted.mission_snapshot_id == "mission-1:snapshot:7"
    assert accepted.asset_references == MappingProxyType(
        {
            "model.mzn": "artifacts/attempt-2/model.mzn",
            "data.dzn": "artifacts/attempt-2/data.dzn",
        }
    )
    with pytest.raises(TypeError):
        accepted.asset_references["model.mzn"] = "changed"  # type: ignore[index]

    events: list[TransportEvent] = []
    with transport.open_consumer(subscription) as reader:
        while (delivery := reader.receive()) is not None:
            events.append(cast(TransportEvent, delivery.message))
            delivery.ack()

    assert [event.event_kind for event in events] == [
        "planner-choice",
        "planner-generation-attempt",
        "planner-generation-attempt",
    ]
    attempts = [
        PlannerGenerationAttempt.from_dict(event.payload) for event in events[1:]
    ]
    assert attempts == [rejected, accepted]
    assert PlannerGenerationAttempt.from_json(accepted.to_canonical_json()) == accepted
    assert "mission_text" not in accepted.to_dict()
    assert "reasoning" not in accepted.to_dict()


def test_planner_choice_and_attempt_identity_are_idempotent() -> None:
    mission_input = _mission_input()
    intent = _planning_intent(mission_input)
    snapshot, scene = _scene_snapshot()
    hyper = HyperAgent(lambda _: intent, transport=InProcessTransport())

    choice = hyper.choose_planner(mission_input)
    assert hyper.choose_planner(mission_input) == choice
    first_attempt = _attempt(choice, attempt_id="attempt-1", outcome="rejected")

    first = hyper.planning_heartbeat(
        mission_input, snapshot, scene, lambda *_: first_attempt
    )
    repeated = hyper.planning_heartbeat(
        mission_input, snapshot, scene, lambda *_: first_attempt
    )

    assert repeated.attempt == first.attempt
    conflicting = _attempt(
        choice,
        attempt_id="attempt-1",
        outcome="rejected",
        asset_references={"model.mzn": "changed/model.mzn"},
        asset_sha256={"model.mzn": "d" * 64},
    )
    with pytest.raises(ValueError, match="different generation attempt"):
        hyper.planning_heartbeat(
            mission_input, snapshot, scene, lambda *_: conflicting
        )


def test_accepted_attempt_requires_asset_references_and_hashes() -> None:
    mission_input = _mission_input()
    choice = PlannerChoiceRecord.from_planning_intent(_planning_intent(mission_input))

    with pytest.raises(ValueError, match="accepted generation attempt requires assets"):
        _attempt(choice, attempt_id="attempt-1", outcome="accepted")


def test_planning_heartbeat_rejects_another_missions_attempt() -> None:
    first_input = _mission_input()
    second_input = MissionInput("mission-2", "Survey", "mission-control")
    intents = {
        first_input.mission_id: _planning_intent(first_input),
        second_input.mission_id: _planning_intent(second_input),
    }
    hyper = HyperAgent(lambda value: intents[value.mission_id])
    hyper.choose_planner(first_input)
    second_choice = hyper.choose_planner(second_input)
    snapshot, scene = _scene_snapshot(first_input.mission_id)
    wrong_mission = _attempt(
        second_choice,
        attempt_id="attempt-2",
        outcome="rejected",
        mission_snapshot_id=f"{first_input.mission_id}:snapshot:7",
    )

    with pytest.raises(ValueError, match="current Planner Choice"):
        hyper.planning_heartbeat(
            first_input, snapshot, scene, lambda *_: wrong_mission
        )


def test_planning_heartbeat_reports_missing_scene_without_starting_planning() -> None:
    mission_input = _mission_input()
    intent = _planning_intent(mission_input)
    snapshot = MissionSnapshot(
        mission_id=mission_input.mission_id,
        version=1,
        created_at="time-1",
    )
    calls: list[object] = []
    hyper = HyperAgent(lambda _: intent)

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start without scene evidence")

    result = hyper.planning_heartbeat(
        mission_input,
        snapshot,
        None,
        generate,
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE
    assert result.planner_choice is None
    assert result.attempt is None
    assert calls == []
    assert hyper.planner_choice(mission_input.mission_id) is None


def test_planning_heartbeat_reports_stale_scene_without_starting_planning() -> None:
    mission_input = _mission_input()
    intent = _planning_intent(mission_input)
    _, scene = _scene_snapshot()
    snapshot = MissionSnapshot(
        mission_id=mission_input.mission_id,
        version=7,
        created_at="time-7",
        operational_scene_graph=scene.event_id,
        source_revisions={"operational_scene_graph": 7},
        source_health={"operational_scene_graph": "healthy"},
        source_freshness={"operational_scene_graph": False},
    )
    calls: list[object] = []
    hyper = HyperAgent(lambda _: intent)

    def generate(*args: object) -> PlannerGenerationAttempt:
        calls.append(args)
        raise AssertionError("generation must not start with stale scene evidence")

    result = hyper.planning_heartbeat(
        mission_input,
        snapshot,
        scene,
        generate,
    )

    assert result.outcome is PlanningHeartbeatOutcome.INSUFFICIENT_SCENE_EVIDENCE
    assert result.mission_snapshot_id == "mission-1:snapshot:7"
    assert result.planner_choice is None
    assert result.attempt is None
    assert calls == []
