import importlib.util
import json

import pytest

import onr.contracts.planning as planning_contracts
from onr.contracts.hyper_agent import HumanQuestion, ReplanRequest


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
    assert json.loads(request.to_canonical_json())["source_revisions"] == {
        "mission": 7,
        "scene": 2,
    }


def test_only_hyper_agent_can_issue_human_question() -> None:
    with pytest.raises(ValueError):
        HumanQuestion(
            "question-1", "mission-1", "What is the target?", {"urgency": "high"}
        )
    with pytest.raises((TypeError, ValueError)):
        HumanQuestion(
            "question-2",
            "mission-1",
            "Answer this",
            {},
            requester="maneuver-control",  # pyright: ignore[reportCallIssue]
        )


@pytest.mark.parametrize(
    "module_name",
    (
        "onr.application.minizinc",
        "onr.application.pddl",
        "onr.application.symbolic_planning",
        "onr.application.temporal_planning",
    ),
)
def test_legacy_planning_modules_are_removed(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None


@pytest.mark.parametrize(
    "contract_name",
    (
        "MissionSpec",
        "SymbolicMissionSpec",
        "SymbolicPlanningResult",
        "TemporalPlanningResult",
    ),
)
def test_legacy_planning_contracts_are_removed(contract_name: str) -> None:
    assert not hasattr(planning_contracts, contract_name)
