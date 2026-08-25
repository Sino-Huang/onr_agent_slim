from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, MutableMapping
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents.middleware import TodoListMiddleware

import onr.agents.maneuver_control as maneuver_control_agent
from onr.adapters.inprocess_transport import InProcessTransport, InProcessTransportState
from onr.agents.maneuver_control import (
    MANEUVER_HEARTBEAT_COMPLETION_SCHEMA,
    DeepAgentsDecisionProvider,
    create_maneuver_control_agent,
)
from onr.agents.structured_output import StructuredOutputRetriesExhausted
from onr.application.fsm import FSMRunner, InMemoryFSMStateStore
from onr.application.maneuver_control import ManeuverControl, ManeuverHeartbeatResult
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import (
    FSMStatus,
    ManeuverDecision,
    ManeuverFeedback,
    Statechart,
    StatechartTransition,
    TransitionCandidate,
)
from onr.contracts.maneuver_control import (
    InvocationOverlay,
    ManeuverCommand,
    ManeuverControlDecision,
    NonPhysicalChoice,
    PhysicalAction,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
)
from onr.contracts.transport import (
    Command,
    CommandOutcome,
    TransportEvent,
)
from onr.ports.transport import Subscription


class RecordingAdapter:
    def __init__(self) -> None:
        self.commands: list[ManeuverCommand] = []

    def submit(self, command: ManeuverCommand) -> object:
        self.commands.append(command)
        return {"adapter_receipt": command.command_id}


class FailingAdapter:
    def __init__(self) -> None:
        self.attempts = 0

    def submit(self, command: ManeuverCommand) -> object:
        self.attempts += 1
        raise RuntimeError("adapter unavailable")


class InterruptingAdapter:
    def __init__(self) -> None:
        self.commands: list[ManeuverCommand] = []

    def submit(self, command: ManeuverCommand) -> object:
        self.commands.append(command)
        raise KeyboardInterrupt("process interrupted")


class FixedDecisionProvider:
    def __init__(self, decision: ManeuverControlDecision) -> None:
        self.decision = decision
        self.calls = 0

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
    ) -> ManeuverControlDecision:
        self.calls += 1
        return self.decision


def test_deep_agents_decision_provider_unwraps_message_state() -> None:
    expected = ManeuverControlDecision(
        decision_id="decision-1",
        mission_id="mission-1",
        plan_revision=1,
        maneuver_id="survey",
        physical_intent=ManeuverIntent(PhysicalAction.NAVIGATE),
    )

    class Agent:
        def invoke(self, _: object) -> dict[str, object]:
            return {
                "messages": [SimpleNamespace(content=json.dumps(expected.to_dict()))],
                "files": {},
            }

    provider = DeepAgentsDecisionProvider(Agent())
    result = provider.decide(
        cast(MissionSnapshot, cast(object, SimpleNamespace(to_dict=lambda: {}))),
        cast(FSMStatus, cast(object, SimpleNamespace(to_dict=lambda: {}))),
    )

    assert result == expected


class RecordingDecisionAgent:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[Mapping[str, object]] = []

    def invoke(self, value: Mapping[str, object]) -> object:
        self.calls.append(value)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def _model_decision(
    *, mission_id: str = "mission-recovery", plan_revision: int = 4
) -> ManeuverControlDecision:
    return ManeuverControlDecision(
        "decision-recovery",
        mission_id,
        plan_revision,
        maneuver_id="survey",
        action="navigate",
    )


def _message_contents(call: Mapping[str, object]) -> list[str]:
    messages = call["messages"]
    assert isinstance(messages, list)
    contents = [getattr(message, "content", None) for message in messages]
    assert all(isinstance(content, str) for content in contents)
    return cast(list[str], contents)


def test_structural_failure_retries_with_original_context_and_safe_feedback() -> None:
    snapshot = _snapshot("mission-recovery", 4)
    status = _status("mission-recovery", 4)
    overlay = InvocationOverlay("mission-recovery", "request-4", {"hint": "hold"})
    expected = _model_decision()
    agent = RecordingDecisionAgent(
        [
            {"structured_response": {"raw-secret-field": "raw-secret-value"}},
            {"structured_response": expected.to_dict()},
        ]
    )

    result = DeepAgentsDecisionProvider(agent, max_retries=1).decide(
        snapshot, status, overlay
    )

    assert result == expected
    assert len(agent.calls) == 2
    original = {
        "snapshot": snapshot.to_dict(),
        "fsm_status": status.to_dict(),
        "overlay": overlay.to_dict(),
    }
    first = _message_contents(agent.calls[0])
    second = _message_contents(agent.calls[1])
    assert json.loads(first[0]) == original
    assert second[0] == first[0]
    feedback = json.loads(second[1])
    assert feedback == {
        "errors": [
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.choice",
                "retries_remaining": 1,
            },
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.decision_id",
                "retries_remaining": 1,
            },
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.maneuver_id",
                "retries_remaining": 1,
            },
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.mission_id",
                "retries_remaining": 1,
            },
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.payload",
                "retries_remaining": 1,
            },
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.physical_intent",
                "retries_remaining": 1,
            },
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.plan_revision",
                "retries_remaining": 1,
            },
            {
                "attempt": 1,
                "code": "missing_required_field",
                "expected": "required field",
                "path": "$.schema_version",
                "retries_remaining": 1,
            },
        ],
        "additional_errors_omitted": True,
    }
    assert "raw-secret" not in second[1]


@pytest.mark.parametrize("max_retries", [0, 1, 3])
def test_structured_output_retry_budget_is_initial_call_plus_retries(
    max_retries: int,
) -> None:
    agent = RecordingDecisionAgent([{"structured_response": None}])
    provider = DeepAgentsDecisionProvider(agent, max_retries=max_retries)

    with pytest.raises(StructuredOutputRetriesExhausted) as caught:
        provider.decide(
            _snapshot("mission-recovery", 4), _status("mission-recovery", 4)
        )

    assert caught.value.code == "output_structure_retries_exhausted"
    assert len(agent.calls) == max_retries + 1


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(
            lambda value: {
                key: item for key, item in value.items() if key != "payload"
            },
            id="missing-field",
        ),
        pytest.param(lambda value: {**value, "extra": True}, id="extra-field"),
        pytest.param(
            lambda value: {**value, "plan_revision": "4"}, id="wrong-primitive"
        ),
        pytest.param(lambda value: {**value, "payload": []}, id="wrong-container"),
        pytest.param(lambda value: {**value, "choice": "launch"}, id="invalid-choice"),
        pytest.param(
            lambda value: {
                **value,
                "physical_intent": {"action": "launch", "parameters": {}},
            },
            id="invalid-action",
        ),
    ],
)
def test_structural_decision_errors_retry(invalid: Any) -> None:
    expected = _model_decision()
    agent = RecordingDecisionAgent(
        [
            {"structured_response": invalid(expected.to_dict())},
            {"structured_response": expected.to_dict()},
        ]
    )

    assert (
        DeepAgentsDecisionProvider(agent, max_retries=1).decide(
            _snapshot("mission-recovery", 4), _status("mission-recovery", 4)
        )
        == expected
    )
    assert len(agent.calls) == 2


@pytest.mark.parametrize(
    ("invalid", "path", "allowed"),
    [
        pytest.param(
            lambda value: {**value, "choice": "raw-invalid-choice"},
            "$.choice",
            tuple(choice.value for choice in NonPhysicalChoice),
            id="choice",
        ),
        pytest.param(
            lambda value: {
                **value,
                "physical_intent": {
                    "action": "raw-invalid-action",
                    "parameters": {},
                },
            },
            "$.physical_intent.action",
            tuple(action.value for action in PhysicalAction),
            id="physical-action",
        ),
    ],
)
def test_invalid_enums_receive_allowed_values_in_safe_feedback(
    invalid: Any, path: str, allowed: tuple[str, ...]
) -> None:
    expected = _model_decision()
    agent = RecordingDecisionAgent(
        [
            {"structured_response": invalid(expected.to_dict())},
            {"structured_response": expected},
        ]
    )

    assert (
        DeepAgentsDecisionProvider(agent, max_retries=1).decide(
            _snapshot("mission-recovery", 4), _status("mission-recovery", 4)
        )
        == expected
    )

    feedback_text = _message_contents(agent.calls[1])[1]
    feedback = json.loads(feedback_text)
    issue = next(error for error in feedback["errors"] if error["path"] == path)
    assert issue["code"] == "invalid_value"
    assert all(f'"{value}"' in issue["expected"] for value in allowed)
    assert "raw-invalid" not in feedback_text


@pytest.mark.parametrize(
    "invalid_response",
    [
        object(),
        {"messages": [SimpleNamespace(content="{raw-secret")]},
        {"messages": [SimpleNamespace(content=[{"type": "tool_call"}])]},
    ],
)
def test_nonmapping_malformed_json_and_tool_call_responses_retry(
    invalid_response: object,
) -> None:
    expected = _model_decision()
    agent = RecordingDecisionAgent(
        [invalid_response, {"structured_response": expected.to_dict()}]
    )

    assert (
        DeepAgentsDecisionProvider(agent, max_retries=1).decide(
            _snapshot("mission-recovery", 4), _status("mission-recovery", 4)
        )
        == expected
    )
    assert len(agent.calls) == 2
    assert "raw-secret" not in _message_contents(agent.calls[1])[1]


def test_nonmapping_response_exhausts_without_escaping_to_application() -> None:
    agent = RecordingDecisionAgent([object()])

    with pytest.raises(StructuredOutputRetriesExhausted):
        DeepAgentsDecisionProvider(agent, max_retries=0).decide(
            _snapshot("mission-recovery", 4), _status("mission-recovery", 4)
        )

    assert len(agent.calls) == 1


def test_structured_output_exhaustion_has_no_maneuver_control_side_effect() -> None:
    mission_id = "mission-exhaustion"
    snapshot = _snapshot(mission_id, 1)
    status = _status(mission_id, 1)
    original_status = status.to_dict()
    agent = RecordingDecisionAgent(
        [{"structured_response": {"choice": "raw-invalid-choice"}}]
    )
    transport = InProcessTransport()
    adapter = RecordingAdapter()
    control = ManeuverControl(
        cast(Any, transport),
        adapter,
        DeepAgentsDecisionProvider(agent, max_retries=1),
    )

    with pytest.raises(StructuredOutputRetriesExhausted):
        control.heartbeat(snapshot, status, event_id="exhausted-input")

    assert len(agent.calls) == 2
    assert (
        transport.latest_event("maneuver-invocations/exhausted-input", mission_id)
        is None
    )
    assert transport.state.events == {}
    assert transport.state.commands == {}
    assert transport.state.receipts == {}
    assert transport.state.outcomes == {}
    assert status.to_dict() == original_status
    assert adapter.commands == []


def test_typed_decision_response_shapes_return_without_retry() -> None:
    expected = _model_decision()
    for response in (expected, {"structured_response": expected}):
        agent = RecordingDecisionAgent([response])
        assert (
            DeepAgentsDecisionProvider(agent, max_retries=3).decide(
                _snapshot("mission-recovery", 4), _status("mission-recovery", 4)
            )
            == expected
        )
        assert len(agent.calls) == 1


def test_agent_factory_receives_strict_heartbeat_completion_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_create(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(maneuver_control_agent, "_create_deep_agent", fake_create)

    create_maneuver_control_agent(model="model")

    assert captured["response_format"] is MANEUVER_HEARTBEAT_COMPLETION_SCHEMA
    assert MANEUVER_HEARTBEAT_COMPLETION_SCHEMA["additionalProperties"] is False
    assert set(MANEUVER_HEARTBEAT_COMPLETION_SCHEMA["required"]) == {
        "mission_id",
        "request_id",
        "outcome",
        "summary",
    }
    assert [item.name for item in captured["tools"]] == [
        "set_transition_target",
        "transition_fsm",
        "navigate",
        "takeoff",
        "land",
        "search_area",
        "pursue",
        "investigate",
        "ingest_perceptions",
        "communicate",
    ]
    assert [type(item) for item in captured["middleware"]] == [TodoListMiddleware]


def test_typed_decision_application_validation_failure_is_not_retried_or_effectful() -> (
    None
):
    mission_id = "mission-validation"
    decision = _model_decision(mission_id="wrong-mission", plan_revision=1)
    agent = RecordingDecisionAgent([decision])
    provider = DeepAgentsDecisionProvider(agent, max_retries=4)
    transport = InProcessTransport()
    adapter = RecordingAdapter()
    control = ManeuverControl(cast(Any, transport), adapter, provider)

    with pytest.raises(ValueError, match="mission ID does not match context"):
        control.heartbeat(
            _snapshot(mission_id, 1),
            _status(mission_id, 1),
            event_id="invalid-input",
        )

    assert len(agent.calls) == 1
    assert (
        transport.latest_event("maneuver-invocations/invalid-input", mission_id) is None
    )
    assert transport.state.commands == {}
    assert adapter.commands == []


def test_stale_typed_decision_is_not_retried_or_effectful() -> None:
    mission_id = "mission-stale-validation"
    decision = _model_decision(mission_id=mission_id, plan_revision=0)
    agent = RecordingDecisionAgent([decision])
    transport = InProcessTransport()
    adapter = RecordingAdapter()
    status = _status(mission_id, 1)
    original_status = status.to_dict()
    control = ManeuverControl(
        cast(Any, transport),
        adapter,
        DeepAgentsDecisionProvider(agent, max_retries=4),
    )

    with pytest.raises(ValueError, match="plan revision does not match FSM status"):
        control.heartbeat(_snapshot(mission_id, 1), status, event_id="stale-input")

    assert len(agent.calls) == 1
    assert (
        transport.latest_event("maneuver-invocations/stale-input", mission_id) is None
    )
    assert transport.state.events == {}
    assert transport.state.commands == {}
    assert status.to_dict() == original_status
    assert adapter.commands == []


def test_structural_correction_then_typed_decision_has_one_decision_and_command_path() -> (
    None
):
    mission_id = "mission-success"
    expected = _model_decision(mission_id=mission_id, plan_revision=1)
    agent = RecordingDecisionAgent(
        [
            {"structured_response": {**expected.to_dict(), "choice": "invalid"}},
            {"structured_response": expected},
        ]
    )
    transport = InProcessTransport()
    adapter = RecordingAdapter()
    control = ManeuverControl(
        cast(Any, transport),
        adapter,
        DeepAgentsDecisionProvider(agent, max_retries=2),
    )

    result = control.heartbeat(
        _snapshot(mission_id, 1), _status(mission_id, 1), event_id="valid-input"
    )

    assert result.decision == expected
    assert result.command is not None
    assert len(agent.calls) == 2
    marker = transport.latest_event(
        "maneuver-invocations/valid-input", mission_id, event_kind="maneuver-invocation"
    )
    assert marker is not None
    assert marker.payload["decision"] == expected.to_dict()
    assert (
        sum(
            event.event_kind == "maneuver-invocation"
            for events in transport.state.events.values()
            for event in events
        )
        == 1
    )
    queued = transport.state.commands[("maneuver-adapter", mission_id)]
    assert sum(isinstance(message, Command) for _, message in queued) == 1
    control.handle_command(result.command)
    assert len(adapter.commands) == 1


def test_deep_agents_decision_provider_passes_attached_debug_callback() -> None:
    expected = ManeuverControlDecision(
        decision_id="decision-debug",
        mission_id="mission-1",
        plan_revision=1,
        maneuver_id="survey",
        physical_intent=ManeuverIntent(PhysicalAction.NAVIGATE),
    )
    callback = object()

    class Agent:
        _onr_debug_callback = callback

        def __init__(self) -> None:
            self.config: object = None

        def invoke(self, _: object, *, config: object = None) -> object:
            self.config = config
            return expected

    agent = Agent()
    result = DeepAgentsDecisionProvider(agent).decide(
        cast(MissionSnapshot, SimpleNamespace(to_dict=lambda: {})),
        cast(FSMStatus, SimpleNamespace(to_dict=lambda: {})),
    )

    assert result == expected
    assert agent.config == {"callbacks": [callback]}


class AlternatingDecisionProvider:
    def __init__(self, decisions: tuple[ManeuverControlDecision, ...]) -> None:
        self.decisions = decisions
        self.calls = 0

    def decide(
        self,
        snapshot: MissionSnapshot,
        status: FSMStatus,
        overlay: InvocationOverlay | None = None,
    ) -> ManeuverControlDecision:
        _ = snapshot, status, overlay
        decision = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return decision


def _snapshot(mission_id: str, revision: int) -> MissionSnapshot:
    return MissionSnapshot(
        mission_id=mission_id,
        version=revision,
        created_at=f"t-{revision}",
        plan_revision=revision,
        plan_reference=f"plan-{revision}",
        source_revisions={"plan": revision},
        source_references={"plan": f"plan-{revision}"},
        source_health={"plan": "ok"},
        source_freshness={"plan": True},
    )


def _status(mission_id: str, revision: int) -> FSMStatus:
    return FSMStatus(
        mission_id=mission_id,
        plan_revision=revision,
        statechart_revision=revision,
        active_state="ready",
        transition_candidates=(
            TransitionCandidate("advance:survey", "ready", "active"),
        ),
    )


def test_maneuver_control_validates_only_mission_revision_and_enabled_transition() -> (
    None
):
    mission_id = "mission-context"
    snapshot = _snapshot(mission_id, 1)
    status = _status(mission_id, 1)
    stale = ManeuverControlDecision(
        "stale",
        mission_id,
        1,
        choice=NonPhysicalChoice.TRANSITION,
        transition_event="not-enabled",
    )
    control = ManeuverControl(
        cast(Any, InProcessTransport()),
        RecordingAdapter(),
        FixedDecisionProvider(stale),
    )
    with pytest.raises(ValueError, match="not enabled"):
        control.decide(snapshot, status)


def test_adapter_failure_publishes_failed_outcome_and_retries_once() -> None:
    mission_id = "mission-failure"
    subscription = Subscription("maneuver-adapter", mission_id, "maneuver")
    transport = InProcessTransport((subscription,))
    adapter = FailingAdapter()
    decision = ManeuverControlDecision(
        "failure-decision", mission_id, 1, maneuver_id="survey", action="navigate"
    )
    control = ManeuverControl(
        cast(Any, transport), adapter, FixedDecisionProvider(decision)
    )
    result = control.heartbeat(_snapshot(mission_id, 1), _status(mission_id, 1))
    assert result.command is not None
    consumer = transport.open_consumer(subscription)
    with pytest.raises(RuntimeError, match="adapter unavailable"):
        asyncio.run(control.run_once(consumer))
    failed = transport.get_command_outcome(result.command.command_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.command_id == result.command.command_id
    assert failed.correlation_id == result.command.correlation_id
    assert failed.mission_id == mission_id
    assert failed.payload == {
        "adapter_submission": "failed",
        "error": "adapter unavailable",
        "source": "maneuver-adapter-transport",
    }
    assert asyncio.run(control.run_once(consumer)) == failed
    assert asyncio.run(control.run_once(consumer)) == failed
    assert adapter.attempts == 1
    assert transport.latest_event("maneuver-feedback", mission_id) is None
    consumer.close()


def test_adapter_submission_marker_survives_maneuver_control_restart() -> None:
    mission_id = "mission-restart"
    revision = 3
    snapshot = _snapshot(mission_id, revision)
    status = _status(mission_id, revision)
    first_decision = ManeuverControlDecision(
        "decision-first", mission_id, revision, maneuver_id="survey", action="navigate"
    )
    adapter = RecordingAdapter()
    state = InProcessTransportState()
    first_transport = InProcessTransport(state=state)
    first = ManeuverControl(
        cast(Any, first_transport), adapter, FixedDecisionProvider(first_decision)
    )
    first_result = first.heartbeat(snapshot, status)
    assert first_result.command is not None
    generic = first_result.command.to_command("maneuver-adapter")
    first_outcome = first.handle_command(generic)
    assert isinstance(first_outcome, CommandOutcome)
    assert first_outcome.status == "accepted"

    second_decision = ManeuverControlDecision(
        "decision-second", mission_id, revision, maneuver_id="survey", action="navigate"
    )
    marker_topic = f"maneuver-submissions/{generic.command_id}"
    second_transport = InProcessTransport(
        (
            Subscription("maneuver-adapter", mission_id, "maneuver"),
            Subscription("maneuver-control", mission_id, marker_topic),
        ),
        state=state,
    )
    second = ManeuverControl(
        cast(Any, second_transport), adapter, FixedDecisionProvider(second_decision)
    )
    second_result = second.heartbeat(snapshot, status)
    assert second_result.command is not None
    assert second_result.command.command_id == first_result.command.command_id
    assert second.handle_command(generic) == first_outcome
    consumer = second_transport.open_consumer(
        Subscription("maneuver-adapter", mission_id, "maneuver")
    )
    assert asyncio.run(second.run_once(consumer)) == first_outcome
    assert asyncio.run(second.run_once(consumer)) == first_outcome
    consumer.close()
    marker_consumer = second_transport.open_consumer(
        Subscription("maneuver-control", mission_id, marker_topic)
    )
    assert isinstance(asyncio.run(second.run_once(marker_consumer)), TransportEvent)
    assert asyncio.run(second.run_once(marker_consumer)) is None
    marker_consumer.close()
    assert len(adapter.commands) == 1


def test_submission_intent_prevents_resubmit_after_crash_window() -> None:
    mission_id = "mission-crash-window"
    revision = 1
    state = InProcessTransportState()
    first_transport = InProcessTransport(state=state)
    interrupted = InterruptingAdapter()
    decision = ManeuverControlDecision(
        "crash-window-decision",
        mission_id,
        revision,
        maneuver_id="survey",
        action="navigate",
    )
    first = ManeuverControl(
        cast(Any, first_transport), interrupted, FixedDecisionProvider(decision)
    )
    command = ManeuverCommand(
        "crash-window-command",
        "crash-window-command",
        mission_id,
        revision,
        "survey",
        ManeuverIntent("navigate"),
    )
    generic = command.to_command("maneuver-adapter")

    with pytest.raises(KeyboardInterrupt, match="process interrupted"):
        first.handle_command(generic)
    assert len(interrupted.commands) == 1
    assert (
        first_transport.latest_event(
            "maneuver-submissions-intents/crash-window-command",
            mission_id,
            event_kind="maneuver-submission-intent",
        )
        is not None
    )
    assert first_transport.get_command_outcome(command.command_id) is None

    restarted_adapter = RecordingAdapter()
    second_transport = InProcessTransport(state=state)
    second = ManeuverControl(
        cast(Any, second_transport), restarted_adapter, FixedDecisionProvider(decision)
    )
    outcome = second.handle_command(generic)

    assert outcome.status == "failed"
    assert outcome.payload == {
        "adapter_submission": "unknown",
        "error": "prior adapter submission outcome is unknown; command will not be submitted again",
        "source": "maneuver-adapter-transport",
    }
    assert second_transport.get_command_outcome(command.command_id) == outcome
    assert restarted_adapter.commands == []
    assert (
        second_transport.latest_event(
            "maneuver-submissions/crash-window-command",
            mission_id,
            event_kind="maneuver-submitted",
        )
        is None
    )


def test_adapter_submission_is_independent_from_explicit_fsm_decision() -> None:
    mission_id = "mission-symbolic"
    revision = 7
    chart = Statechart(
        mission_id=mission_id,
        plan_revision=revision,
        mission_snapshot_id="snapshot-7",
        planning_profile="symbolic",
        entry_state="ready",
        states=("ready", "complete"),
        transitions=(
            StatechartTransition(
                "advance:survey",
                "ready",
                "complete",
                {"desired_outcome": "survey evidence has been judged complete"},
            ),
        ),
        terminal_states=("complete",),
        state_context={"ready": {}, "complete": {}},
    )
    transport = InProcessTransport()
    runner = FSMRunner(cast(Any, transport), store=InMemoryFSMStateStore())
    initial = asyncio.run(runner.activate(chart))
    candidate = initial.transition_candidates[0]
    snapshot = _snapshot(mission_id, revision)
    intent = ManeuverIntent(
        PhysicalAction.NAVIGATE,
        (ManeuverParameter("waypoint", "area-7"),),
    )
    decision = ManeuverControlDecision(
        "physical-decision",
        mission_id,
        revision,
        maneuver_id="survey",
        physical_intent=intent,
    )
    adapter = RecordingAdapter()
    control = ManeuverControl(
        cast(Any, transport), adapter, FixedDecisionProvider(decision)
    )
    result = control.heartbeat(snapshot, initial)
    assert result.command is not None
    control.handle_command(result.command.to_command("maneuver-adapter"))
    assert asyncio.run(runner.status()).active_state == initial.active_state  # type: ignore[union-attr]
    assert transport.latest_event("maneuver-feedback", mission_id) is None

    moved = asyncio.run(
        runner.apply(
            candidate,
            ManeuverDecision(
                "transition-decision",
                mission_id,
                transition_event=candidate.event,
                payload={"plan_revision": revision},
            ),
        )
    )
    assert moved.active_state == candidate.target
    assert len(adapter.commands) == 1


def test_cancel_maneuver_is_non_physical_until_feedback() -> None:
    mission_id = "mission-cancel"
    revision = 1
    decision = ManeuverControlDecision(
        "cancel-request",
        mission_id,
        revision,
        maneuver_id="survey",
        choice=NonPhysicalChoice.CANCEL_MANEUVER,
    )
    adapter = RecordingAdapter()
    control = ManeuverControl(
        cast(Any, InProcessTransport()), adapter, FixedDecisionProvider(decision)
    )
    result = control.heartbeat(
        _snapshot(mission_id, revision), _status(mission_id, revision)
    )
    assert result.command is None
    assert adapter.commands == []
    assert "cancelled" not in result.decision.payload


def test_decisions_validate_physical_actions_and_non_physical_choices() -> None:
    for action in PhysicalAction:
        decision = ManeuverControlDecision(
            f"decision-{action}",
            "mission",
            1,
            maneuver_id="m",
            physical_intent=ManeuverIntent(action),
        )
        assert decision.physical_intent is not None

    for choice in NonPhysicalChoice:
        if choice is NonPhysicalChoice.TRANSITION:
            with pytest.raises(ValueError):
                ManeuverControlDecision(
                    f"decision-{choice}", "mission", 1, choice=choice
                )
            continue
        if choice is NonPhysicalChoice.CANCEL_MANEUVER:
            with pytest.raises(ValueError):
                ManeuverControlDecision(
                    f"decision-{choice}", "mission", 1, choice=choice
                )
            decision = ManeuverControlDecision(
                f"decision-{choice}", "mission", 1, maneuver_id="m", choice=choice
            )
        else:
            decision = ManeuverControlDecision(
                f"decision-{choice}", "mission", 1, choice=choice
            )
        assert decision.choice is choice
    transition = ManeuverControlDecision(
        "transition",
        "mission",
        1,
        choice=NonPhysicalChoice.TRANSITION,
        transition_event="advance",
    )
    assert transition.event == "advance"
    normalized = ManeuverControlDecision(
        "normalized", "mission", 1, transition_event="advance"
    )
    assert normalized.choice is NonPhysicalChoice.TRANSITION

    advisory = ManeuverControlDecision(
        "advisory",
        "mission",
        1,
        choice=NonPhysicalChoice.REPLAN,
        maneuver_id="m",
        physical_intent=ManeuverIntent("navigate"),
    )
    assert (
        advisory.physical_intent is not None
        and advisory.choice is NonPhysicalChoice.REPLAN
    )
    with pytest.raises(ValueError):
        ManeuverControlDecision(
            "physical-transition",
            "mission",
            1,
            choice=NonPhysicalChoice.TRANSITION,
            transition_event="advance",
            maneuver_id="m",
            physical_intent=ManeuverIntent("navigate"),
        )
    with pytest.raises(ValueError):
        ManeuverControlDecision(
            "physical-cancel",
            "mission",
            1,
            choice=NonPhysicalChoice.CANCEL_MANEUVER,
            maneuver_id="m",
            physical_intent=ManeuverIntent("navigate"),
        )
    for payload in (
        {"nested": {"status": "completed"}},
        {"items": [{"lifecycle": "active"}]},
    ):
        with pytest.raises(ValueError):
            ManeuverControlDecision(
                "nested-lifecycle",
                "mission",
                1,
                choice=NonPhysicalChoice.REPORT,
                payload=payload,
            )


def test_overlay_is_immutable_and_transient() -> None:
    snapshot = _snapshot("mission", 1)
    status = _status("mission", 1)
    overlay = InvocationOverlay(
        "mission",
        "request-1",
        {"snapshot": snapshot.to_dict(), "fsm_status": status.to_dict()},
    )
    assert overlay.to_dict()["values"] == {
        "snapshot": snapshot.to_dict(),
        "fsm_status": status.to_dict(),
    }
    with pytest.raises((FrozenInstanceError, TypeError)):
        setattr(cast(object, overlay), "request_id", "request-2")
    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], overlay.values)["new"] = "not-authority"
    assert snapshot.plan_revision == 1
    assert status.active_state == "ready"


def test_replaying_event_rehydrates_the_stored_maneuver_decision_and_command() -> None:
    mission_id = "mission-replay"
    snapshot = _snapshot(mission_id, 1)
    status = _status(mission_id, 1)
    first = ManeuverControlDecision(
        "decision-first", mission_id, 1, maneuver_id="survey", action="navigate"
    )
    second = ManeuverControlDecision(
        "decision-second", mission_id, 1, choice=NonPhysicalChoice.REPORT
    )
    provider = AlternatingDecisionProvider((first, second))
    transport = InProcessTransport()
    control = ManeuverControl(cast(Any, transport), RecordingAdapter(), provider)
    event = TransportEvent(
        1,
        "maneuver-input-1",
        mission_id,
        0,
        "maneuver-input",
        {"snapshot": snapshot.to_dict(), "fsm_status": status.to_dict()},
    )

    original = asyncio.run(control.run_once(event))
    replay = asyncio.run(control.run_once(event))

    assert isinstance(original, ManeuverHeartbeatResult)
    assert isinstance(replay, ManeuverHeartbeatResult)
    assert original.decision == first
    assert replay.decision == original.decision
    assert replay.command == original.command
    assert replay.receipt == original.receipt
    assert provider.calls == 1


def test_maneuver_command_converts_to_generic_command_without_feedback() -> None:
    command = ManeuverCommand(
        "maneuver-command",
        "correlation",
        "mission",
        3,
        "survey",
        ManeuverIntent("investigate"),
    )
    generic = command.to_command("maneuver-adapter")
    restored = ManeuverCommand.from_command(generic)
    assert restored == command
    assert ManeuverCommand.from_json(command.to_canonical_json()) == command
    assert generic.command_kind == "maneuver"
