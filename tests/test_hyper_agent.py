import asyncio

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.hyper_agent import HyperAgent
from onr.agents.hyper_agent import DeepAgentsMissionInterpreter
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
