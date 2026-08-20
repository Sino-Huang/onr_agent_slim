from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from onr.adapters.fsm_store import JsonFSMStateStore
from onr.adapters.inprocess_transport import InProcessTransport
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.contracts.fsm import (
    FSMEvent,
    FSMExecutionRecord,
    FSMStatus,
    ManeuverDecision,
    ManeuverFeedback,
    Statechart,
    TransitionCandidate,
)
from onr.contracts.planning import (
    ManeuverIntent,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanProvenance,
    ScheduledManeuver,
    SymbolicPlanStep,
    VerifiableReference,
)
from onr.contracts.transport import (
    TransportEvent,
    create_normalized_plan_transport_event,
    normalized_plan_transport_event_to_wire,
)
from onr.ports.transport import Subscription
from onr.runtime.composition import RuntimeComposition
from onr.runtime.config import (
    HeartbeatsConfig,
    LLMConfig,
    PlannerConfig,
    PlannersConfig,
    RuntimeConfig,
    ServicesConfig,
    StorageConfig,
    TransportConfig,
)


def _provenance(mission_id: str) -> PlanProvenance:
    return PlanProvenance(
        mission_id=mission_id,
        source_authority="authority",
        mission_intent=VerifiableReference(f"mission-input:{mission_id}", "1" * 64),
        planning_decision=VerifiableReference(f"planner-choice:{mission_id}", "2" * 64),
        environment_data=VerifiableReference(f"scene:{mission_id}", "3" * 64),
        generated_assets={
            "planner-input": VerifiableReference("planner-input", "4" * 64),
        },
        solver_evidence={
            "planner-result": VerifiableReference("planner-result", "5" * 64),
        },
    )


def _temporal_plan(revision: int = 1) -> NormalizedPlan:
    choice = PlannerChoice("temporal", "minizinc")
    return NormalizedPlan(
        plan_revision=revision,
        mission_snapshot_id=f"snapshot-{revision}",
        planner_choice=choice,
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            ScheduledManeuver("survey", ManeuverIntent("survey"), (), 0, 2),
            ScheduledManeuver("report", ManeuverIntent("report"), ("survey",), 2, 1),
        ),
        provenance=_provenance("mission-fsm"),
    )


def _symbolic_plan(revision: int = 1) -> NormalizedPlan:
    choice = PlannerChoice("symbolic", "fast-downward")
    return NormalizedPlan(
        plan_revision=revision,
        mission_snapshot_id=f"snapshot-symbolic-{revision}",
        planner_choice=choice,
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            SymbolicPlanStep(0, "survey", ManeuverIntent("survey"), (), 1),
            SymbolicPlanStep(1, "report", ManeuverIntent("report"), ("survey",), 1),
        ),
        provenance=_provenance("mission-fsm-symbolic"),
    )


def _event(plan: NormalizedPlan, event_id: str = "plan-event"):
    return create_normalized_plan_transport_event(plan, event_id=event_id, sequence=0)


def test_statechart_is_deterministic_immutable_and_untrusted() -> None:
    chart = Statechart.from_normalized_plan(_temporal_plan())
    assert chart.trusted is False
    assert chart.to_canonical_json() == Statechart.from_json(chart.to_canonical_json()).to_canonical_json()
    assert isinstance(chart.to_dict()["states"], list)

    untrusted = json.loads(chart.to_canonical_json())
    frozen = Statechart.from_dict(untrusted)
    untrusted["states"].append("attacker-state")
    assert "attacker-state" not in chart.states
    assert "attacker-state" not in frozen.states
    with pytest.raises(ValueError):
        Statechart.from_dict({**json.loads(chart.to_canonical_json()), "trusted": True})
    with pytest.raises(ValueError):
        Statechart.from_json('{"states": ["ok"], "entry_state": "ok", "transitions": [], "trusted": false, "schema_version": 1, "mission_id": "m", "plan_revision": 1, "mission_snapshot_id": "s", "planning_profile": "temporal", "deadlines": {"ok": NaN}}')


def test_execution_and_transition_contracts_have_canonical_round_trips() -> None:
    candidate = TransitionCandidate("advance:survey", "state-0", "state-1")
    assert TransitionCandidate.from_json(candidate.to_canonical_json()) == candidate
    record = FSMExecutionRecord(
        mission_id="mission-fsm",
        plan_revision=1,
        statechart_revision=1,
        active_state="state-1",
        active_configuration=("state-1",),
        last_applied_event="advance:survey",
        transition_history=("advance:survey",),
    )
    assert FSMExecutionRecord.from_json(record.to_canonical_json()) == record
    status = FSMStatus(
        mission_id="mission-fsm",
        plan_revision=1,
        statechart_revision=1,
        active_state="state-1",
        transition_candidates=(candidate,),
    )
    assert FSMStatus.from_json(status.to_canonical_json()) == status


def test_public_event_decision_feedback_contracts_are_strict_and_immutable() -> None:
    event = FSMEvent("event-1", "transition", {"event": "advance:survey", "nested": [1]})
    decision = ManeuverDecision(
        "decision-1", "mission-fsm", transition_event="advance:survey"
    )
    feedback = ManeuverFeedback(
        "feedback-1", "mission-fsm", "survey", "completed", {"source": "environment"}
    )
    assert FSMEvent.from_json(event.to_canonical_json()) == event
    assert ManeuverDecision.from_json(decision.to_canonical_json()) == decision
    assert ManeuverFeedback.from_json(feedback.to_canonical_json()) == feedback
    with pytest.raises(TypeError):
        event.payload["nested"] = (2,)  # type: ignore[index]
    with pytest.raises(ValueError):
        ManeuverDecision(
            "decision-2",
            "mission-fsm",
            transition_event="advance:survey",
            physical_maneuver={"action": "survey"},
        )
    with pytest.raises(ValueError):
        ManeuverDecision(
            "decision-3",
            "mission-fsm",
            maneuver_id="survey",
            payload={"status": "completed"},
        )


def test_runner_applies_only_enabled_events_and_publishes_status() -> None:
    plan = _temporal_plan()
    transport = InProcessTransport()
    runner = FSMRunner(transport, store=InMemoryFSMStateStore(), clock=lambda: 0)

    initial = asyncio.run(runner.handle(_event(plan)))
    assert initial.active_state == "state-0"
    unchanged = asyncio.run(runner.transition("not-enabled"))
    assert unchanged.active_state == "state-0"
    updated = asyncio.run(runner.transition("advance:survey"))
    assert updated.active_state == "state-1"
    assert transport.latest_event("fsm-status", plan.mission_id) is not None


def test_runner_consumes_normalized_plan_transport_wire_event() -> None:
    plan = _temporal_plan()
    subscription = Subscription("fsm", plan.mission_id, "normalized-plans")
    transport = InProcessTransport((subscription,))
    transport.publish_event("normalized-plans", _event(plan))
    consumer = transport.open_consumer(subscription)
    runner = FSMRunner(transport, store=InMemoryFSMStateStore(), clock=lambda: 0)
    status = asyncio.run(runner.run_once(consumer))
    assert status is not None and status.mission_id == plan.mission_id
    assert consumer.receive() is None
    consumer.close()


def test_temporal_deadline_publishes_timer_due_without_transition() -> None:
    plan = _temporal_plan()
    transport = InProcessTransport()
    runner = FSMRunner(transport, store=InMemoryFSMStateStore(), clock=lambda: 2)

    status = asyncio.run(runner.handle(_event(plan)))
    assert status.timer_due is True
    assert status.active_state == "state-0"


def test_temporal_event_is_not_enabled_before_its_deadline() -> None:
    now = [-1]
    plan = _temporal_plan()
    runner = FSMRunner(InProcessTransport(), store=InMemoryFSMStateStore(), clock=lambda: now[0])
    initial = asyncio.run(runner.handle(_event(plan)))
    assert initial.transition_candidates == ()
    assert asyncio.run(runner.transition("advance:survey")).active_state == "state-0"
    now[0] = 0
    assert asyncio.run(runner.transition("advance:survey")).active_state == "state-1"


def test_runner_public_activate_apply_tick_and_event_idempotency() -> None:
    now = [-1]
    plan = _temporal_plan()
    transport = InProcessTransport()
    runner = FSMRunner(transport, store=InMemoryFSMStateStore(), clock=lambda: now[0])
    initial = asyncio.run(runner.activate(_event(plan)))
    assert initial.enabled_transition_candidates == ()
    before = transport.next_event_sequence("fsm-status", plan.mission_id)
    due = asyncio.run(runner.tick(0))
    assert due is not None and due.timer_due is True and due.active_state == "state-0"
    after_first_tick = transport.next_event_sequence("fsm-status", plan.mission_id)
    assert after_first_tick == before + 1
    asyncio.run(runner.tick(0))
    assert transport.next_event_sequence("fsm-status", plan.mission_id) == after_first_tick
    candidate = due.enabled_transition_candidates[0]
    event = FSMEvent("transition-1", "transition", {"event": candidate.event})
    applied = asyncio.run(runner.apply(candidate, event))
    assert applied.active_state == "state-1"
    duplicate = asyncio.run(runner.apply(candidate, event))
    assert duplicate.active_state == "state-1"
    assert len(runner.store.load_execution_record().applied_event_identities) == 1  # type: ignore[union-attr]
    restarted = FSMRunner(transport, store=runner.store, clock=lambda: 0)
    replayed = asyncio.run(restarted.apply(candidate, event))
    assert replayed.active_state == "state-1"
    assert len(restarted.store.load_execution_record().applied_event_identities) == 1  # type: ignore[union-attr]


def test_same_revision_activation_is_idempotent_without_status_publication() -> None:
    plan = _temporal_plan()
    transport = InProcessTransport()
    runner = FSMRunner(transport, store=InMemoryFSMStateStore(), clock=lambda: 0)
    asyncio.run(runner.activate(_event(plan, "plan-1")))
    next_sequence = transport.next_event_sequence("fsm-status", plan.mission_id)
    asyncio.run(runner.activate(_event(plan, "plan-2")))
    assert transport.next_event_sequence("fsm-status", plan.mission_id) == next_sequence


def test_timer_due_marker_remains_authoritative_after_clock_change_and_restart() -> None:
    now = [-1]
    plan = _temporal_plan()
    store = InMemoryFSMStateStore()
    transport = InProcessTransport()
    runner = FSMRunner(transport, store=store, clock=lambda: now[0])
    asyncio.run(runner.activate(_event(plan)))
    due = asyncio.run(runner.tick(0))
    assert due is not None and due.timer_due
    now[0] = -1
    restarted = FSMRunner(transport, store=store, clock=lambda: now[0])
    restored = asyncio.run(restarted.status())
    assert restored is not None and restored.timer_due
    applied = asyncio.run(
        restarted.apply(
            restored.enabled_transition_candidates[0],
            FSMEvent("after-restart", "transition", {"event": "advance:survey"}),
        )
    )
    assert applied.active_state == "state-1"


@pytest.mark.parametrize("field", ("normalized_plan_document", "normalized_plan_sha256"))
def test_generic_normalized_plan_event_validates_wire_provenance(field: str) -> None:
    plan = _temporal_plan()
    typed = create_normalized_plan_transport_event(plan, event_id="wire-plan", sequence=0)
    wire = normalized_plan_transport_event_to_wire(typed)
    payload = dict(wire.payload)
    payload[field] = "tampered"
    tampered = TransportEvent(
        schema_version=wire.schema_version,
        event_id=wire.event_id,
        mission_id=wire.mission_id,
        sequence=wire.sequence,
        event_kind=wire.event_kind,
        payload=payload,
    )
    with pytest.raises(ValueError):
        asyncio.run(FSMRunner(InProcessTransport()).activate(tampered))


def test_inconsistent_persisted_execution_record_is_rejected() -> None:
    plan = _temporal_plan()
    store = InMemoryFSMStateStore()
    runner = FSMRunner(InProcessTransport(), store=store, clock=lambda: 0)
    asyncio.run(runner.activate(_event(plan)))
    record = json.loads(store.execution_record_json)  # type: ignore[arg-type]
    record["active_state"] = "undeclared"
    record["active_configuration"] = ["undeclared"]
    store.execution_record_json = json.dumps(record)
    with pytest.raises(RuntimeError):
        FSMRunner(InProcessTransport(), store=store)


def test_runtime_registers_fsm_runner_subscription() -> None:
    config = RuntimeConfig(
        llm=LLMConfig("test", "http://127.0.0.1:14398/v1", "model", "test-key", 0),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1), PlannerConfig(Path(__file__), 1)
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", Path(__file__).parent / "transport"),
        storage=StorageConfig(Path(__file__).parent / "storage"),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm-service", "planner"),
        debug=False,
        agent_name="test-agent",
    )
    transport = InProcessTransport()
    runtime = RuntimeComposition(config, transport)
    runner = runtime.create_fsm_runner(mission_id="mission-fsm")
    assert runner.subscription is not None
    assert runner.subscription.service_id == "fsm-service"
    assert runner.subscription in transport.subscriptions
    consumer = transport.open_consumer(runner.subscription)
    consumer.close()


def test_symbolic_progression_requires_feedback_and_decision() -> None:
    plan = _symbolic_plan()
    runner = FSMRunner(InProcessTransport(), store=InMemoryFSMStateStore(), clock=lambda: 0)
    asyncio.run(runner.handle(_event(plan)))

    assert asyncio.run(runner.transition("advance:survey")).active_state == "state-0"
    assert asyncio.run(
        runner.transition(
            "advance:survey",
            lifecycle_facts={"survey": "completed"},
        )
    ).active_state == "state-0"
    status = asyncio.run(
        runner.transition(
            "advance:survey",
            lifecycle_facts={"survey": "completed"},
            maneuver_decision={"event": "advance:survey"},
        )
    )
    assert status.active_state == "state-1"


def test_symbolic_apply_requires_authoritative_feedback_and_matching_decision() -> None:
    plan = _symbolic_plan()
    runner = FSMRunner(InProcessTransport(), store=InMemoryFSMStateStore(), clock=lambda: 0)
    initial = asyncio.run(runner.activate(_event(plan)))
    candidate = initial.enabled_transition_candidates[0]
    feedback = ManeuverFeedback("feedback-1", plan.mission_id, "survey", "completed")
    wrong_mission = ManeuverDecision("decision-wrong", "other-mission", transition_event=candidate.event)
    assert asyncio.run(runner.apply(candidate, feedback, wrong_mission)).active_state == "state-0"
    decision = ManeuverDecision("decision-1", plan.mission_id, transition_event=candidate.event)
    assert asyncio.run(runner.apply(candidate, feedback)).active_state == "state-0"
    assert asyncio.run(runner.apply(candidate, ManeuverFeedback("feedback-2", plan.mission_id, "survey", "completed"), decision)).active_state == "state-1"


def test_restart_reconstructs_from_persisted_json_and_plan_swap_is_visible() -> None:
    plan = _temporal_plan()
    store = InMemoryFSMStateStore()
    transport = InProcessTransport()
    first = FSMRunner(transport, store=store, clock=lambda: 0)
    asyncio.run(first.handle(_event(plan)))
    asyncio.run(first.transition("advance:survey"))
    assert store.statechart_json.startswith("{")
    assert store.execution_record_json.startswith("{")

    restarted = FSMRunner(transport, store=store, clock=lambda: 0)
    assert asyncio.run(restarted.status()).active_state == "state-1"
    replacement = _temporal_plan(2)
    swapped = asyncio.run(restarted.handle(_event(replacement, "replacement")))
    assert swapped.plan_revision == 2
    assert swapped.active_state == "state-0"
    assert swapped.superseded_plan_revision == 1
    assert swapped.retained_maneuver_visibility == ("survey", "report")


def test_file_json_store_reconstructs_without_python_runtime_state(tmp_path) -> None:
    plan = _temporal_plan()
    transport = InProcessTransport()
    store = JsonFSMStateStore(tmp_path / "fsm")
    first = FSMRunner(transport, store=store, clock=lambda: 0)
    asyncio.run(first.handle(_event(plan)))
    asyncio.run(first.transition("advance:survey"))
    restarted = FSMRunner(transport, store=JsonFSMStateStore(tmp_path / "fsm"), clock=lambda: 0)
    assert asyncio.run(restarted.status()).active_state == "state-1"


def _provenance_plan() -> NormalizedPlan:
    choice = PlannerChoice("temporal", "minizinc")
    return NormalizedPlan(
        plan_revision=1,
        mission_snapshot_id="mission-fsm:snapshot:1",
        planner_choice=choice,
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            ScheduledManeuver("survey", ManeuverIntent("survey"), (), 0, 2),
        ),
        provenance=PlanProvenance(
            mission_id="mission-fsm",
            source_authority="authority",
            mission_intent=VerifiableReference("mission-input:1", "1" * 64),
            planning_decision=VerifiableReference("planner-choice:1", "2" * 64),
            environment_data=VerifiableReference("scene:1", "3" * 64),
            generated_assets={
                "model.mzn": VerifiableReference("model.mzn", "4" * 64),
            },
            solver_evidence={
                "stdout": VerifiableReference("solver.stdout", "5" * 64),
            },
        ),
    )


def test_statechart_accepts_provenance_only_normalized_plan() -> None:
    plan = _provenance_plan()

    chart = Statechart.from_normalized_plan(plan)

    assert chart.mission_id == plan.mission_id
    assert chart.plan_revision == plan.plan_revision
    assert chart.mission_snapshot_id == plan.mission_snapshot_id
    assert chart.states == ("state-0", "state-1")
