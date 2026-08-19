import hashlib
from collections.abc import Mapping

import pytest
from langchain.agents.middleware import TodoListMiddleware

from onr.agents import (
    DeepAgentsPlanningIntentInterpreter,
    PLANNING_INTENT_SCHEMA,
    create_hyper_agent,
    create_planning_intent_agent,
)
from onr.agents.hyper_agent import MISSION_SPEC_SCHEMA
from onr.agents.structured_output import StructuredOutputRetriesExhausted
from onr.contracts import PlanningIntent
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import PlannerChoice


class _ResponseAgent:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[Mapping[str, object]] = []

    def invoke(self, value: Mapping[str, object]) -> object:
        self.calls.append(value)
        return self.responses.pop(0)


def _candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "mission_id": "mission-1",
        "source_authority": "mission-control",
        "objective": "Survey the operating area",
        "planner_choice": {
            "planning_profile": "temporal",
            "planner_id": "minizinc",
        },
        "rationale": "The requested survey produces the needed assessment.",
        "details": {
            "constraints": {"regions": ["alpha", {"priority": 1}]},
            "include_imagery": True,
        },
    }
    candidate.update(overrides)
    return candidate


def test_planning_intent_interpreter_builds_a_trusted_temporal_intent() -> None:
    mission_input = MissionInput(
        "mission-1", "Survey the operating area", "mission-control"
    )
    agent = _ResponseAgent([{"structured_response": _candidate()}])

    result = DeepAgentsPlanningIntentInterpreter(agent).interpret(mission_input)

    assert isinstance(result, PlanningIntent)
    assert result.schema_version == 1
    assert result.planner_choice == PlannerChoice("temporal", "minizinc")
    assert result.mission_input_sha256 == hashlib.sha256(
        mission_input.to_canonical_json().encode("utf-8")
    ).hexdigest()
    assert result.to_dict()["details"] == _candidate()["details"]


@pytest.mark.parametrize(
    "candidate",
    (
        _candidate(mission_id="another-mission"),
        _candidate(source_authority="untrusted-authority"),
    ),
)
def test_planning_intent_interpreter_rejects_untrusted_identity(
    candidate: dict[str, object],
) -> None:
    agent = _ResponseAgent([{"structured_response": candidate}])

    with pytest.raises(ValueError):
        DeepAgentsPlanningIntentInterpreter(agent).interpret(
            MissionInput("mission-1", "Survey", "mission-control")
        )


def test_planning_intent_interpreter_recovers_from_malformed_output_safely() -> None:
    raw_candidate = "PRIVATE malformed planning intent"
    agent = _ResponseAgent(
        [
            {"structured_response": raw_candidate},
            {"structured_response": _candidate()},
        ]
    )

    result = DeepAgentsPlanningIntentInterpreter(agent, max_retries=1).interpret(
        MissionInput("mission-1", "Survey", "mission-control")
    )

    assert isinstance(result, PlanningIntent)
    assert len(agent.calls) == 2
    messages = agent.calls[1]["messages"]
    assert isinstance(messages, list)
    assert raw_candidate not in messages[1].content


def test_planning_intent_interpreter_caps_four_retries_at_five_invokes() -> None:
    agent = _ResponseAgent([{} for _ in range(5)])

    with pytest.raises(StructuredOutputRetriesExhausted):
        DeepAgentsPlanningIntentInterpreter(agent, max_retries=4).interpret(
            MissionInput("mission-1", "Survey", "mission-control")
        )

    assert len(agent.calls) == 5


def test_planning_intent_factory_uses_its_schema_without_changing_hyper_factory(
    monkeypatch,
) -> None:
    import deepagents

    created: list[dict[str, object]] = []

    def fake_create_deep_agent(**kwargs: object) -> object:
        created.append(kwargs)
        return object()

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create_deep_agent)

    create_planning_intent_agent(model=object())
    create_hyper_agent(model=object())

    planning_intent_kwargs, hyper_kwargs = created
    assert planning_intent_kwargs["response_format"] is PLANNING_INTENT_SCHEMA
    assert hyper_kwargs["response_format"] is MISSION_SPEC_SCHEMA
    middleware = planning_intent_kwargs["middleware"]
    assert isinstance(middleware, list)
    assert [type(item) for item in middleware] == [TodoListMiddleware]


def test_planning_intent_schema_requires_configured_planners_and_safe_details() -> None:
    planner_choices = PLANNING_INTENT_SCHEMA["properties"]["planner_choice"]["oneOf"]
    assert {
        (
            choice["properties"]["planning_profile"]["enum"][0],
            choice["properties"]["planner_id"]["enum"][0],
        )
        for choice in planner_choices
    } == {("temporal", "minizinc"), ("symbolic", "fast-downward")}

    details = PLANNING_INTENT_SCHEMA["properties"]["details"]
    assert details["type"] == "object"
    assert details["additionalProperties"] is True
    assert "propertyNames" not in details
    assert "planner assets" in details["description"]
