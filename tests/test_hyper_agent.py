import asyncio
import json
from collections.abc import Mapping

import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.hyper_agent import HyperAgent
from onr.agents.hyper_agent import DeepAgentsMissionInterpreter, create_hyper_agent
from onr.agents.structured_output import StructuredOutputRetriesExhausted
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import HumanQuestion, MissionInput, ReplanRequest
from onr.contracts.planning import (
    ManeuverIntent,
    MissionSpec,
    PlannerChoice,
    PlanningOutcome,
    SymbolicManeuver,
    SymbolicMissionSpec,
    SymbolicPlanStep,
    ScheduledManeuver,
    TemporalManeuver,
)
from onr.ports.transport import Subscription


def _spec(mission_id: str = "mission-1", objective: str = "Survey the operating area") -> MissionSpec:
    return MissionSpec(
        mission_id=mission_id,
        objective=objective,
        planner_choice=PlannerChoice("temporal", "minizinc"),
        maneuvers=(TemporalManeuver("survey", ManeuverIntent("survey"), (), 1),),
        horizon=3,
        source_authority="mission-control",
    )


def _symbolic_spec(mission_id: str = "mission-symbolic") -> SymbolicMissionSpec:
    return SymbolicMissionSpec(
        mission_id=mission_id,
        objective="Survey symbolically",
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        maneuvers=(SymbolicManeuver("survey", ManeuverIntent("survey"), (), 1),),
        source_authority="mission-control",
    )


class Planner:
    def __init__(self, outcomes: list[PlanningOutcome] | None = None) -> None:
        self.outcomes = outcomes or [PlanningOutcome.SOLVED]

    def plan(self, spec, plan_revision: int, mission_snapshot_id: str):
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome is PlanningOutcome.SOLVED and isinstance(spec, MissionSpec):
            maneuvers = (ScheduledManeuver("survey", spec.maneuvers[0].intent, (), 0, 1),)
        elif outcome is PlanningOutcome.SOLVED:
            maneuvers = (SymbolicPlanStep(0, "survey", spec.maneuvers[0].intent, (), 1),)
        else:
            maneuvers = ()
        from onr.contracts.planning import NormalizedPlan

        return NormalizedPlan(
            mission_spec=spec,
            plan_revision=plan_revision,
            mission_snapshot_id=mission_snapshot_id,
            planner_choice=spec.planner_choice,
            outcome=outcome,
            maneuvers=maneuvers,
        )


def _snapshot(mission_id: str = "mission-1", revision: int = 1) -> MissionSnapshot:
    return MissionSnapshot(
        mission_id,
        revision,
        f"time-{revision}",
        source_revisions={"operational_scene_graph": revision},
        source_references={"operational_scene_graph": f"scene-{revision}"},
    )


def test_freeze_validates_before_authority_and_publishes_audit_event() -> None:
    transport = InProcessTransport()
    mission_input = MissionInput("mission-1", "Survey the operating area", "mission-control")
    service = HyperAgent(lambda _: _spec(), planner=Planner(), transport=transport)
    frozen = service.freeze_mission(mission_input)
    assert service.authority("mission-1") == frozen
    event = transport.latest_event("mission-specifications", "mission-1")
    assert event is not None and event.event_kind == "mission-specification"

    invalid = HyperAgent(lambda _: {"not": "a MissionSpec"}, transport=transport)
    with pytest.raises(ValueError):
        invalid.freeze_mission(mission_input)
    assert invalid.authority("mission-1") is None
    assert transport.next_event_sequence("mission-specifications", "mission-1") == 1


def test_initial_heartbeat_and_replan_coalescing_are_mission_local() -> None:
    transport = InProcessTransport()
    planner = Planner()
    service = HyperAgent(lambda item: _spec(item.mission_id), planner=planner, transport=transport)
    service.freeze_mission(MissionInput("mission-1", "Survey", "mission-control"))
    service.freeze_mission(MissionInput("mission-2", "Survey", "mission-control"))

    first = service.heartbeat(_snapshot("mission-1"))
    assert first.outcome is PlanningOutcome.SOLVED and first.plan_revision == 1
    service.submit_replan(ReplanRequest("r-1", "mission-1", "scene changed", "maneuver-control", 1, {"scene": 2}))
    service.submit_replan(ReplanRequest("r-1", "mission-1", "duplicate", "maneuver-control", 9, {"scene": 9}))
    service.submit_replan(ReplanRequest("r-2", "mission-1", "new target", "maneuver-control", 2, {"scene": 3}))
    service.submit_replan(ReplanRequest("other", "mission-2", "different mission", "maneuver-control", 1))
    second = service.heartbeat(_snapshot("mission-1", 3))
    assert second.plan_revision == 2
    assert second.request is not None
    assert second.request.coalesced_request_ids == ("r-1", "r-2")
    assert service.heartbeat(_snapshot("mission-2")).mission_id == "mission-2"


def test_failed_plan_does_not_replace_active_plan_or_publish() -> None:
    transport = InProcessTransport()
    service = HyperAgent(lambda _: _spec(), planner=Planner([PlanningOutcome.UNSOLVABLE]), transport=transport)
    service.freeze_mission(MissionInput("mission-1", "Survey", "mission-control"))
    result = service.heartbeat(_snapshot())
    assert result.outcome is PlanningOutcome.UNSOLVABLE
    assert result.plan is None
    assert transport.latest_event("normalized-plans", "mission-1") is None


def test_solved_plan_is_consumable_by_fsm_and_retains_superseded_maneuvers() -> None:
    transport = InProcessTransport()
    service = HyperAgent(lambda _: _spec(), planner=Planner(), transport=transport)
    service.freeze_mission(MissionInput("mission-1", "Survey", "mission-control"))
    first = service.heartbeat(_snapshot())
    subscription = Subscription("fsm", "mission-1", "normalized-plans")
    transport.subscriptions = (subscription,)
    runner = FSMRunner(transport, store=InMemoryFSMStateStore(), subscription=subscription)
    consumer = transport.open_consumer(subscription)
    initial = asyncio.run(runner.run_once(consumer))
    assert initial is not None and initial.active_state == first.entry_state

    service.submit_replan(ReplanRequest("r-1", "mission-1", "changed", "maneuver-control", 1))
    service.heartbeat(_snapshot(revision=2))
    status = asyncio.run(runner.run_once(consumer))
    assert status is not None and status.plan_revision == 2
    assert status.retained_maneuver_ids == ("survey",)
    consumer.close()


def test_human_question_can_only_be_issued_by_hyper_agent() -> None:
    service = HyperAgent(lambda _: _spec())
    question = service.ask_human("mission-1", "question-1", "What is the target?", {"urgent": True})
    assert isinstance(question, HumanQuestion)
    assert question.requester == "hyper-agent"
    with pytest.raises((TypeError, ValueError)):
        HumanQuestion(
            "question-2", "mission-1", "No", {}, requester="maneuver-control"  # pyright: ignore[reportCallIssue]
        )


def test_deep_agents_interpreter_uses_strict_domain_parser() -> None:
    class Agent:
        def invoke(self, _: object) -> dict[str, object]:
            return {"structured_response": _spec().to_dict()}

    result = DeepAgentsMissionInterpreter(Agent()).interpret(
        MissionInput("mission-1", "Survey", "mission-control")
    )
    assert result == _spec()


class _ResponseAgent:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[Mapping[str, object]] = []

    def invoke(self, value: Mapping[str, object]) -> object:
        self.calls.append(value)
        return self.responses.pop(0)


def test_interpreter_recovers_temporal_output_with_original_safe_message() -> None:
    raw_failure = "PRIVATE malformed candidate"
    agent = _ResponseAgent(
        [
            {"structured_response": raw_failure},
            {"structured_response": _spec().to_dict()},
        ]
    )
    mission_input = MissionInput("mission-1", "Survey", "mission-control")

    result = DeepAgentsMissionInterpreter(agent).interpret(mission_input)

    assert result == _spec()
    assert len(agent.calls) == 2
    expected = json.dumps(
        mission_input.to_dict(),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    first_messages = agent.calls[0]["messages"]
    second_messages = agent.calls[1]["messages"]
    assert isinstance(first_messages, list) and len(first_messages) == 1
    assert isinstance(second_messages, list) and len(second_messages) == 2
    assert first_messages[0].content == expected
    assert second_messages[0].content == expected
    assert raw_failure not in second_messages[1].content


def test_interpreter_accepts_valid_symbolic_candidate() -> None:
    spec = _symbolic_spec()
    agent = _ResponseAgent([{"structured_response": spec.to_dict()}])

    result = DeepAgentsMissionInterpreter(agent).interpret(
        MissionInput(spec.mission_id, "Survey", "mission-control")
    )

    assert result == spec
    assert len(agent.calls) == 1


def test_interpreter_retries_malformed_symbolic_structure_once() -> None:
    spec = _symbolic_spec()
    malformed = {**spec.to_dict(), "domain_revision": "one"}
    agent = _ResponseAgent(
        [
            {"structured_response": malformed},
            {"structured_response": spec.to_dict()},
        ]
    )

    result = DeepAgentsMissionInterpreter(agent, max_retries=1).interpret(
        MissionInput(spec.mission_id, "Survey", "mission-control")
    )

    assert result == spec
    assert len(agent.calls) == 2


def test_interpreter_retries_malformed_structured_output_with_safe_feedback() -> None:
    raw_candidate = "PRIVATE malformed tool candidate"
    raw_exception = "PRIVATE tool-call exception"

    class MalformedStructuredResponse:
        def __repr__(self) -> str:
            return raw_candidate

        def model_dump(self) -> dict[str, object]:
            raise RuntimeError(raw_exception)

    agent = _ResponseAgent(
        [
            {"structured_response": MalformedStructuredResponse()},
            {"structured_response": _spec().to_dict()},
        ]
    )

    assert DeepAgentsMissionInterpreter(agent, max_retries=1).interpret(
        MissionInput("mission-1", "Survey", "mission-control")
    ) == _spec()
    assert len(agent.calls) == 2
    messages = agent.calls[1]["messages"]
    assert isinstance(messages, list)
    assert raw_candidate not in messages[1].content
    assert raw_exception not in messages[1].content


@pytest.mark.parametrize(("max_retries", "expected_calls"), [(0, 1), (1, 2), (4, 5)])
def test_interpreter_retry_budget_limits_calls(
    max_retries: int, expected_calls: int
) -> None:
    agent = _ResponseAgent([{} for _ in range(expected_calls)])

    with pytest.raises(StructuredOutputRetriesExhausted):
        DeepAgentsMissionInterpreter(agent, max_retries=max_retries).interpret(
            MissionInput("mission-1", "Survey", "mission-control")
        )

    assert len(agent.calls) == expected_calls


@pytest.mark.parametrize(
    "candidate",
    [
        {
            key: value
            for key, value in _spec().to_dict().items()
            if key != "objective"
        },
        {**_spec().to_dict(), "unexpected": True},
        {**_spec().to_dict(), "horizon": "three"},
        {
            **_spec().to_dict(),
            "planner_choice": {
                "planning_profile": "quantum",
                "planner_id": "minizinc",
            },
        },
    ],
    ids=["missing", "extra", "wrong-type", "invalid-enum"],
)
def test_interpreter_retries_structural_contract_errors(
    candidate: dict[str, object],
) -> None:
    agent = _ResponseAgent(
        [
            {"structured_response": candidate},
            {"structured_response": _spec().to_dict()},
        ]
    )

    assert DeepAgentsMissionInterpreter(agent, max_retries=1).interpret(
        MissionInput("mission-1", "Survey", "mission-control")
    ) == _spec()
    assert len(agent.calls) == 2


def test_interpreter_does_not_retry_semantic_bad_bound() -> None:
    candidate = {**_spec().to_dict(), "horizon": 0}
    agent = _ResponseAgent(
        [
            {"structured_response": candidate},
            {"structured_response": _spec().to_dict()},
        ]
    )

    with pytest.raises(ValueError, match="mission horizon must be positive"):
        DeepAgentsMissionInterpreter(agent).interpret(
            MissionInput("mission-1", "Survey", "mission-control")
        )

    assert len(agent.calls) == 1


def test_freeze_structural_recovery_creates_one_authority_and_publication() -> None:
    agent = _ResponseAgent(
        [
            {"structured_response": {"mission_id": "mission-1"}},
            {"structured_response": _spec().to_dict()},
        ]
    )
    transport = InProcessTransport()
    service = HyperAgent(
        DeepAgentsMissionInterpreter(agent, max_retries=1),
        transport=transport,
    )

    frozen = service.freeze_mission(
        MissionInput("mission-1", "Survey", "mission-control")
    )

    assert len(agent.calls) == 2
    assert len(service.authorities) == 1
    assert service.authority("mission-1") is frozen
    event = transport.latest_event("mission-specifications", "mission-1")
    assert event is not None and event.event_kind == "mission-specification"
    assert transport.next_event_sequence("mission-specifications", "mission-1") == 1


def test_freeze_recovery_exhaustion_leaves_no_authority_plan_or_publication() -> None:
    agent = _ResponseAgent([{}, {"structured_response": None}])
    transport = InProcessTransport()
    service = HyperAgent(
        DeepAgentsMissionInterpreter(agent, max_retries=1),
        planner=Planner(),
        transport=transport,
    )

    with pytest.raises(StructuredOutputRetriesExhausted):
        service.freeze_mission(
            MissionInput("mission-1", "Survey", "mission-control")
        )

    assert len(agent.calls) == 2
    assert service.authority("mission-1") is None
    assert service.active_plan("mission-1") is None
    assert transport.latest_event("mission-specifications", "mission-1") is None


def test_freeze_identity_mismatch_does_not_retry_or_publish() -> None:
    candidate = _spec(mission_id="wrong-mission")
    agent = _ResponseAgent([{"structured_response": candidate.to_dict()}])
    transport = InProcessTransport()
    service = HyperAgent(
        DeepAgentsMissionInterpreter(agent),
        transport=transport,
    )

    with pytest.raises(ValueError, match="Mission ID does not match"):
        service.freeze_mission(
            MissionInput("mission-1", "Survey", "mission-control")
        )

    assert len(agent.calls) == 1
    assert service.authority("mission-1") is None
    assert transport.latest_event("mission-specifications", "mission-1") is None


def test_freeze_source_authority_mismatch_does_not_retry_or_publish() -> None:
    spec = _symbolic_spec()
    candidate = {**spec.to_dict(), "source_authority": "untrusted-source"}
    agent = _ResponseAgent([{"structured_response": candidate}])
    transport = InProcessTransport()
    service = HyperAgent(DeepAgentsMissionInterpreter(agent), transport=transport)

    with pytest.raises(ValueError, match="source authority does not match"):
        service.freeze_mission(
            MissionInput(spec.mission_id, "Survey", "mission-control")
        )

    assert len(agent.calls) == 1
    assert service.authority(spec.mission_id) is None
    assert transport.latest_event("mission-specifications", spec.mission_id) is None


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (
            {
                **_symbolic_spec().to_dict(),
                "planner_choice": {
                    "planning_profile": "symbolic",
                    "planner_id": "unsupported-planner",
                },
            },
            "unsupported symbolic planner",
        ),
        ({**_symbolic_spec().to_dict(), "maneuvers": []}, "requires symbolic maneuvers"),
        (
            {
                **_symbolic_spec().to_dict(),
                "maneuvers": [
                    {
                        "maneuver_id": "survey",
                        "intent": {"action": "survey", "parameters": {}},
                        "dependencies": ["report"],
                        "cost": 1,
                    },
                    {
                        "maneuver_id": "report",
                        "intent": {"action": "report", "parameters": {}},
                        "dependencies": ["survey"],
                        "cost": 1,
                    },
                ],
            },
            "dependencies must be acyclic",
        ),
    ],
    ids=["unsupported-planner", "empty-maneuvers", "dependency-cycle"],
)
def test_semantic_planning_invariants_do_not_retry_or_publish(
    candidate: dict[str, object], message: str
) -> None:
    spec = _symbolic_spec()
    agent = _ResponseAgent(
        [
            {"structured_response": candidate},
            {"structured_response": spec.to_dict()},
        ]
    )
    transport = InProcessTransport()
    service = HyperAgent(DeepAgentsMissionInterpreter(agent), transport=transport)

    with pytest.raises(ValueError, match=message):
        service.freeze_mission(
            MissionInput(spec.mission_id, "Survey", "mission-control")
        )

    assert len(agent.calls) == 1
    assert service.authority(spec.mission_id) is None
    assert transport.latest_event("mission-specifications", spec.mission_id) is None


def test_already_frozen_conflict_does_not_retry_or_publish() -> None:
    spec = _symbolic_spec()
    changed = _symbolic_spec().to_dict()
    changed["objective"] = "Changed mission"
    agent = _ResponseAgent(
        [
            {"structured_response": spec.to_dict()},
            {"structured_response": changed},
            {"structured_response": spec.to_dict()},
        ]
    )
    transport = InProcessTransport()
    service = HyperAgent(DeepAgentsMissionInterpreter(agent), transport=transport)
    mission_input = MissionInput(spec.mission_id, "Survey", "mission-control")
    frozen = service.freeze_mission(mission_input)
    event = transport.latest_event("mission-specifications", spec.mission_id)

    with pytest.raises(ValueError, match="already frozen"):
        service.freeze_mission(mission_input)

    assert len(agent.calls) == 2
    assert service.authority(spec.mission_id) is frozen
    assert transport.latest_event("mission-specifications", spec.mission_id) is event


def test_deep_agent_accepts_mission_spec_response_schema() -> None:
    model = ChatOpenAI(
        model="test-model",
        base_url="http://127.0.0.1:11411/v1",
        api_key=SecretStr("EMPTY"),
    )

    agent = create_hyper_agent(model=model)

    assert agent is not None


def test_symbolic_heartbeat_uses_the_symbolic_planner_contract() -> None:
    spec = _symbolic_spec()
    service = HyperAgent(lambda _: spec, planner=Planner())
    service.freeze_mission(MissionInput(spec.mission_id, "Survey", "mission-control"))
    result = service.heartbeat(_snapshot(spec.mission_id))
    assert result.outcome is PlanningOutcome.SOLVED
    assert result.plan is not None and result.plan.symbolic_steps[0].maneuver_id == "survey"


def test_failed_replan_preserves_active_authority_and_retries() -> None:
    transport = InProcessTransport()
    service = HyperAgent(
        lambda _: _spec(),
        planner=Planner([PlanningOutcome.SOLVED, PlanningOutcome.UNSOLVABLE, PlanningOutcome.SOLVED]),
        transport=transport,
    )
    service.freeze_mission(MissionInput("mission-1", "Survey", "mission-control"))
    first = service.heartbeat(_snapshot())
    service.submit_replan(
        ReplanRequest("retry-1", "mission-1", "new observation", "maneuver-control", 2)
    )
    failed = service.heartbeat(_snapshot(revision=2))
    assert failed.outcome is PlanningOutcome.UNSOLVABLE
    assert failed.plan is first.plan
    assert failed.statechart is first.statechart
    assert failed.plan_revision == 1
    retried = service.heartbeat(_snapshot(revision=2))
    assert retried.outcome is PlanningOutcome.SOLVED
    assert retried.plan_revision == 2
    assert transport.next_event_sequence("normalized-plans", "mission-1") == 2


def test_coalesced_replans_merge_highest_source_revisions_losslessly() -> None:
    service = HyperAgent(lambda _: _spec(), planner=Planner([PlanningOutcome.UNSOLVABLE]))
    service.freeze_mission(MissionInput("mission-1", "Survey", "mission-control"))
    service.submit_replan(
        ReplanRequest("source-1", "mission-1", "scene", "maneuver-control", 1, {"scene": 2, "belief": None})
    )
    service.submit_replan(
        ReplanRequest("source-2", "mission-1", "belief", "maneuver-control", 2, {"scene": 5, "belief": 3})
    )
    result = service.heartbeat(_snapshot())
    assert result.request is not None
    assert dict(result.request.source_revisions) == {"belief": 3, "scene": 5}
    assert result.request.coalesced_request_ids == ("source-1", "source-2")


def test_refreeze_is_idempotent_or_rejected_without_state_mutation() -> None:
    current = [_spec()]
    transport = InProcessTransport()
    service = HyperAgent(lambda _: current[0], planner=Planner(), transport=transport)
    input_record = MissionInput("mission-1", "Survey", "mission-control")
    record = service.freeze_mission(input_record)
    service.heartbeat(_snapshot())
    event_count = transport.next_event_sequence("mission-specifications", "mission-1")
    assert service.freeze_mission(input_record) is record
    assert service.active_plan("mission-1") is not None
    current[0] = _spec(objective="A changed mission")
    with pytest.raises(ValueError):
        service.freeze_mission(input_record)
    assert service.authority("mission-1") is record
    assert transport.next_event_sequence("mission-specifications", "mission-1") == event_count


def test_replan_transport_event_can_be_ingested_without_republication() -> None:
    subscription = Subscription("hyper", "mission-1", "replan-requests")
    transport = InProcessTransport((subscription,))
    mission_input = MissionInput("mission-1", "Survey", "mission-control")
    first = HyperAgent(lambda _: _spec(), transport=transport)
    second = HyperAgent(lambda _: _spec())
    first.freeze_mission(mission_input)
    second.freeze_mission(mission_input)
    first.submit_replan(
        ReplanRequest("wire-1", "mission-1", "wire observation", "maneuver-control", 1)
    )
    consumer = transport.open_consumer(subscription)
    try:
        accepted = second.run_once(consumer)
    finally:
        consumer.close()
    assert accepted is not None and accepted.request_id == "wire-1"
    assert transport.next_event_sequence("replan-requests", "mission-1") == 1
