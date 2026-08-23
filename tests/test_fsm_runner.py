from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from onr.adapters.fsm_store import JsonFSMStateStore
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.python_statemachine import PythonStateMachineFactory
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.contracts.fsm import (
    FSMExecutionRecord,
    FSMStatus,
    ManeuverDecision,
    Statechart,
    StatechartTransition,
    TransitionCandidate,
)
from onr.contracts.transport import TransportEvent
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


def _chart(revision: int = 1) -> Statechart:
    return Statechart(
        mission_id="mission-fsm",
        plan_revision=revision,
        mission_snapshot_id=f"snapshot-{revision}",
        planning_profile="temporal",
        entry_state="state-0",
        terminal_states=("state-2",),
        states=("state-0", "state-1", "state-2"),
        state_context={
            "state-0": {"renamed": {"phase": "waiting"}},
            "state-1": {"nested": {"desired": ["observe", {"sector": 3}]}},
            "state-2": {"completion": True},
        },
        transitions=(
            StatechartTransition(
                "edge-alpha",
                "state-0",
                "state-1",
                {"readiness": {"clock": {"minimum": 2.5, "unit": "seconds"}}},
            ),
            StatechartTransition(
                "edge-beta",
                "state-1",
                "state-2",
                {"evidence": {"kind": "operator-authored", "values": [1, 2]}},
            ),
        ),
    )


def _wire_event(chart: Statechart) -> TransportEvent:
    return TransportEvent(
        1, "statechart-event", chart.mission_id, 0, "statechart", chart.to_dict()
    )


def _decision(chart: Statechart, event: str, identity: str = "decision-1") -> ManeuverDecision:
    return ManeuverDecision(
        identity,
        chart.mission_id,
        transition_event=event,
        payload={"plan_revision": chart.plan_revision},
    )


def test_schema_v2_round_trips_flexible_contexts_unchanged() -> None:
    chart = _chart()
    round_trip = Statechart.from_json(chart.to_canonical_json())

    assert round_trip == chart
    assert round_trip.schema_version == 2
    assert round_trip.context_for("state-1") == chart.context_for("state-1")
    assert round_trip.transitions[0].context == chart.transitions[0].context
    assert "planner_native_plan_artifact_reference" not in round_trip.to_dict()
    assert "maneuver_id" not in round_trip.transitions[0].to_dict()


@pytest.mark.parametrize(
    "extra",
    [
        {"timers": {}},
        {"deadlines": {}},
        {"trusted": False},
    ],
)
def test_schema_v2_rejects_retired_statechart_fields(extra: dict[str, object]) -> None:
    document = _chart().to_dict()
    document.update(extra)
    with pytest.raises(ValueError, match="unknown or missing"):
        Statechart.from_dict(document)


@pytest.mark.parametrize(
    "transition_extra",
    [
        {"conditions": []},
        {"maneuver_id": "physical-1"},
        {"requires_lifecycle_fact": True},
        {"requires_decision": True},
    ],
)
def test_schema_v2_rejects_version_1_transition_fields(
    transition_extra: dict[str, object],
) -> None:
    document = _chart().to_dict()
    transitions = cast(list[dict[str, Any]], document["transitions"])
    transition = dict(transitions[0])
    transition.update(transition_extra)
    transitions[0] = transition
    with pytest.raises(ValueError, match="unknown or missing"):
        Statechart.from_dict(document)


def test_schema_v2_rejects_non_finite_context_numbers() -> None:
    with pytest.raises(ValueError, match="finite"):
        StatechartTransition("edge", "a", "b", {"value": float("nan")})


def test_schema_v2_rejects_malformed_json() -> None:
    with pytest.raises(ValueError, match="JSON is invalid"):
        Statechart.from_json('{"schema_version":2,')


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        (lambda value: value["state_context"].pop("state-1"), "every state"),
        (
            lambda value: value["transitions"][0].update(target="undeclared"),
            "declared states",
        ),
        (
            lambda value: value["transitions"][1].update(event="edge-alpha"),
            "globally unique",
        ),
        (
            lambda value: value["transitions"].pop(0),
            "reachable from the entry",
        ),
        (
            lambda value: (
                value["states"].append("dead-end"),
                value["state_context"].update({"dead-end": {}}),
                value["transitions"].append(
                    {
                        "event": "edge-dead-end",
                        "source": "state-0",
                        "target": "dead-end",
                        "context": {},
                    }
                ),
            ),
            "reach a terminal",
        ),
    ],
)
def test_schema_v2_rejects_universal_graph_integrity_failures(
    mutation: Any,
    diagnostic: str,
) -> None:
    document = _chart().to_dict()
    mutation(document)
    with pytest.raises(ValueError, match=diagnostic):
        Statechart.from_dict(document)


def test_execution_and_candidate_contracts_round_trip_three_contexts() -> None:
    chart = _chart()
    candidate = TransitionCandidate(
        event="edge-alpha",
        source="state-0",
        target="state-1",
        transition_context=chart.transitions[0].context,
        source_state_context=chart.context_for("state-0"),
        target_state_context=chart.context_for("state-1"),
    )
    assert TransitionCandidate.from_json(candidate.to_canonical_json()) == candidate

    record = FSMExecutionRecord(
        mission_id=chart.mission_id,
        plan_revision=1,
        statechart_revision=1,
        active_state="state-1",
        active_configuration=("state-1",),
        last_applied_event="edge-alpha",
        transition_history=("edge-alpha",),
    )
    assert FSMExecutionRecord.from_json(record.to_canonical_json()) == record

    status = FSMStatus(
        mission_id=chart.mission_id,
        plan_revision=1,
        statechart_revision=1,
        active_state="state-0",
        transition_candidates=(candidate,),
        active_state_context=chart.context_for("state-0"),
    )
    assert FSMStatus.from_json(status.to_canonical_json()) == status


def test_runner_exposes_context_without_interpreting_it_and_requires_decision() -> None:
    chart = _chart()
    runner = FSMRunner(
        InProcessTransport(),
        store=InMemoryFSMStateStore(),
        machine_factory=PythonStateMachineFactory(),
    )
    initial = asyncio.run(runner.activate(chart))
    candidate = initial.transition_candidates[0]

    assert candidate.transition_context == chart.transitions[0].context
    assert candidate.source_state_context == chart.context_for("state-0")
    assert candidate.target_state_context == chart.context_for("state-1")
    assert initial.enabled_events == ("edge-alpha",)

    wrong_mission = ManeuverDecision(
        "wrong-mission",
        "other",
        transition_event=candidate.event,
        payload={"plan_revision": chart.plan_revision},
    )
    assert asyncio.run(runner.apply(candidate, wrong_mission)).active_state == "state-0"

    wrong_event = _decision(chart, "edge-beta", "wrong-event")
    assert asyncio.run(runner.apply(candidate, wrong_event)).active_state == "state-0"

    stale_revision = ManeuverDecision(
        "stale-revision",
        chart.mission_id,
        transition_event=candidate.event,
        payload={"plan_revision": 0},
    )
    assert asyncio.run(runner.apply(candidate, stale_revision)).active_state == "state-0"

    applied = asyncio.run(runner.apply(candidate, _decision(chart, candidate.event)))
    assert applied.active_state == "state-1"


def test_runner_rejects_stale_candidate_and_replayed_decision() -> None:
    chart = _chart()
    runner = FSMRunner(InProcessTransport(), machine_factory=PythonStateMachineFactory())
    first = asyncio.run(runner.activate(chart))
    candidate = first.transition_candidates[0]
    decision = _decision(chart, candidate.event)

    moved = asyncio.run(runner.apply(candidate, decision))
    assert moved.active_state == "state-1"
    assert asyncio.run(runner.apply(candidate, decision)).active_state == "state-1"
    record = runner.store.load_execution_record()
    assert record is not None and record.applied_event_identities == ("decision-1",)


def test_runner_consumes_only_statechart_transport_events() -> None:
    chart = _chart()
    subscription = Subscription("fsm", chart.mission_id, "normalized-plans")
    transport = InProcessTransport((subscription,))
    transport.publish_event("normalized-plans", _wire_event(chart))
    consumer = transport.open_consumer(subscription)
    runner = FSMRunner(transport, store=InMemoryFSMStateStore())

    status = asyncio.run(runner.run_once(consumer))
    assert status is not None and status.mission_id == chart.mission_id
    assert consumer.receive() is None
    consumer.close()


def test_same_revision_activation_is_idempotent_without_publication() -> None:
    chart = _chart()
    transport = InProcessTransport()
    runner = FSMRunner(transport)
    asyncio.run(runner.activate(chart))
    next_sequence = transport.next_event_sequence("fsm-status", chart.mission_id)
    asyncio.run(runner.activate(chart))
    assert transport.next_event_sequence("fsm-status", chart.mission_id) == next_sequence


def test_unknown_statechart_transport_fields_are_rejected() -> None:
    chart = _chart()
    wire = _wire_event(chart)
    payload = dict(wire.payload)
    payload["planner_native_plan_artifact_reference"] = "forbidden"
    tampered = TransportEvent(
        wire.schema_version,
        wire.event_id,
        wire.mission_id,
        wire.sequence,
        wire.event_kind,
        payload,
    )
    with pytest.raises(ValueError):
        asyncio.run(FSMRunner(InProcessTransport()).activate(tampered))


def test_inconsistent_persisted_execution_record_is_rejected() -> None:
    chart = _chart()
    store = InMemoryFSMStateStore()
    runner = FSMRunner(InProcessTransport(), store=store)
    asyncio.run(runner.activate(chart))
    assert store.execution_record_json is not None
    record = json.loads(store.execution_record_json)
    record["active_state"] = "undeclared"
    record["active_configuration"] = ["undeclared"]
    store.execution_record_json = json.dumps(record)
    with pytest.raises(RuntimeError):
        FSMRunner(InProcessTransport(), store=store)


def test_restart_rebuilds_machine_at_recorded_state_and_tracks_plan_supersession() -> None:
    chart = _chart()
    store = InMemoryFSMStateStore()
    transport = InProcessTransport()
    first = FSMRunner(
        transport, store=store, machine_factory=PythonStateMachineFactory()
    )
    initial = asyncio.run(first.activate(chart))
    asyncio.run(
        first.apply(
            initial.transition_candidates[0],
            _decision(chart, "edge-alpha"),
        )
    )

    restarted = FSMRunner(
        transport, store=store, machine_factory=PythonStateMachineFactory()
    )
    restored = asyncio.run(restarted.status())
    assert restored is not None and restored.active_state == "state-1"
    assert restored.enabled_events == ("edge-beta",)

    replacement = _chart(2)
    swapped = asyncio.run(restarted.activate(replacement))
    assert swapped.active_state == replacement.entry_state
    assert swapped.superseded_plan_revision == 1


def test_file_store_restart_rebuilds_dynamic_machine(tmp_path: Path) -> None:
    chart = _chart()
    transport = InProcessTransport()
    first = FSMRunner(
        transport,
        store=JsonFSMStateStore(tmp_path / "fsm"),
        machine_factory=PythonStateMachineFactory(),
    )
    initial = asyncio.run(first.activate(chart))
    asyncio.run(first.apply(initial.transition_candidates[0], _decision(chart, "edge-alpha")))

    restarted = FSMRunner(
        transport,
        store=JsonFSMStateStore(tmp_path / "fsm"),
        machine_factory=PythonStateMachineFactory(),
    )
    status = asyncio.run(restarted.status())
    assert status is not None and status.active_state == "state-1"


def test_runtime_registers_fsm_runner_subscription(tmp_path: Path) -> None:
    config = RuntimeConfig(
        llm=LLMConfig("test", "http://127.0.0.1:14398/v1", "model", "test-key", 0),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1), PlannerConfig(Path(__file__), 1)
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", tmp_path / "transport"),
        storage=StorageConfig(tmp_path / "storage"),
        services=ServicesConfig(
            "hyper", "maneuver", "context", "fsm-service", "planner"
        ),
        debug=False,
        agent_name="test-agent",
    )
    transport = InProcessTransport()
    runner = RuntimeComposition(config, transport).create_fsm_runner(
        mission_id="mission-fsm"
    )
    assert runner.subscription is not None
    assert runner.subscription.service_id == "fsm-service"
    assert runner.subscription in transport.subscriptions
