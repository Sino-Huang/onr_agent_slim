import hashlib
import json

import pytest

from onr.contracts.hyper_agent import FrozenMissionSpec, HumanQuestion, MissionInput, ReplanRequest
from onr.contracts.planning import (
    ManeuverIntent,
    MissionSpec,
    PlannerChoice,
    SymbolicManeuver,
    SymbolicMissionSpec,
    TemporalManeuver,
)


def _temporal_spec() -> MissionSpec:
    return MissionSpec(
        mission_id="mission-1",
        objective="Survey the operating area",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        maneuvers=(TemporalManeuver("survey", ManeuverIntent("survey"), (), 2),),
        horizon=5,
        source_authority="mission-control",
    )


def _symbolic_spec() -> SymbolicMissionSpec:
    return SymbolicMissionSpec(
        mission_id="mission-2",
        objective="Report the finding",
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        maneuvers=(SymbolicManeuver("report", ManeuverIntent("report"), (), 1),),
        source_authority="mission-control",
    )


@pytest.mark.parametrize("spec", [_temporal_spec(), _symbolic_spec()])
def test_mission_specs_strictly_round_trip_from_dict_and_json(spec) -> None:
    assert type(spec).from_dict(spec.to_dict()) == spec
    assert type(spec).from_json(spec.to_canonical_json()) == spec
    with pytest.raises(ValueError):
        type(spec).from_dict({**spec.to_dict(), "unexpected": True})
    with pytest.raises(ValueError):
        type(spec).from_dict({key: value for key, value in spec.to_dict().items() if key != "mission_id"})
    with pytest.raises(ValueError):
        type(spec).from_json('{"value": NaN}')


def test_frozen_mission_authority_is_hashed_and_validated() -> None:
    mission_input = MissionInput("mission-1", "Survey the operating area", "mission-control")
    spec = _temporal_spec()
    record = FrozenMissionSpec(mission_input, spec, 3, spec.to_canonical_json())
    assert record.sha256 == hashlib.sha256(record.canonical_document.encode()).hexdigest()
    assert record.canonical_json == record.canonical_document
    assert record.mission_id == "mission-1"
    with pytest.raises(ValueError):
        FrozenMissionSpec(mission_input, spec, 3, "{}")


def test_replan_request_freezes_authority_and_is_canonical() -> None:
    request = ReplanRequest(
        request_id="request-1",
        mission_id="mission-1",
        reason="new observation",
        requester="maneuver-control",
        observed_plan_revision=4,
        source_revisions={"mission": 7, "scene": 2},
        coalesced_request_ids=("request-0",),
        coalesced_reasons=("stale plan",),
    )
    assert request.plan_revision == 4
    assert request.authoritative_source_revisions["mission"] == 7
    with pytest.raises(TypeError):
        request.source_revisions["scene"] = 3  # pyright: ignore[reportIndexIssue]
    assert json.loads(request.to_canonical_json())["source_revisions"] == {"mission": 7, "scene": 2}


def test_only_hyper_agent_can_issue_human_question() -> None:
    with pytest.raises(ValueError):
        HumanQuestion("question-1", "mission-1", "What is the target?", {"urgency": "high"})
    with pytest.raises((TypeError, ValueError)):
        HumanQuestion(
            "question-2",
            "mission-1",
            "Answer this",
            {},
            requester="maneuver-control",  # pyright: ignore[reportCallIssue]
        )
