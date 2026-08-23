from __future__ import annotations

from collections.abc import Mapping

import pytest

from onr.adapters.inprocess_transport import InProcessTransport
from onr.agents.hyper_agent import DeepAgentsHyperHeartbeatProvider
from onr.application.hyper_supervisor import HyperSupervisor
from onr.contracts.communication import AgentMessage
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import FSMStatus
from onr.contracts.hyper_agent import (
    HyperHeartbeatDecision,
    HyperHeartbeatInvocation,
    ReplanRequest,
)


def _snapshot() -> MissionSnapshot:
    return MissionSnapshot(
        "mission-1",
        4,
        "2026-08-23T00:00:00+10:00",
        plan_revision=1,
        plan_reference="planner-plan.json",
        source_revisions={"plan": 1},
        source_health={"plan": "healthy"},
        source_freshness={"plan": True},
    )


def _invocation(*triggers: str) -> HyperHeartbeatInvocation:
    return HyperHeartbeatInvocation(
        mission_id="mission-1",
        plan_revision=1,
        trigger_identities=triggers or ("periodic:10",),
        mission_snapshot=_snapshot(),
        planner_plan_reference="planner-plan.json",
        statechart_reference="statechart.json",
        fsm_status=FSMStatus(
            mission_id="mission-1",
            plan_revision=1,
            statechart_revision=1,
            active_state="active",
        ),
        environment_data={"scene_graph": {"mission_time_seconds": 10}},
    )


def _request(identity: str, revision: int, reason: str) -> ReplanRequest:
    return ReplanRequest(
        identity,
        "mission-1",
        reason,
        "maneuver-control",
        1,
        {"environment_data": revision},
    )


def test_requests_and_periodic_trigger_coalesce_into_one_durable_decision() -> None:
    captured: list[HyperHeartbeatInvocation] = []

    def provider(invocation: HyperHeartbeatInvocation) -> HyperHeartbeatDecision:
        captured.append(invocation)
        return HyperHeartbeatDecision(
            invocation.mission_id,
            invocation.plan_revision,
            "no_change",
            "Current evidence remains compatible with the active plan.",
            invocation.trigger_identities,
            tuple(
                identity
                for request in invocation.maneuver_requests
                for identity in request.coalesced_request_ids
            ),
        )

    transport = InProcessTransport()
    supervisor = HyperSupervisor(provider, transport=transport)
    supervisor.queue_replan(_request("request-1", 2, "first"))
    supervisor.queue_replan(_request("request-2", 4, "second"))

    decision = supervisor.heartbeat(
        _invocation("periodic:10", "request-1", "request-2")
    )

    assert decision.disposition == "no_change"
    assert len(captured) == 1
    coalesced = captured[0].maneuver_requests[0]
    assert coalesced.coalesced_request_ids == ("request-1", "request-2")
    assert coalesced.coalesced_reasons == ("first", "second")
    assert coalesced.source_revisions["environment_data"] == 4
    assert not supervisor.has_pending("mission-1")
    event = transport.latest_event(
        "hyper-heartbeat-outcomes",
        "mission-1",
        event_kind="hyper-heartbeat-decision",
    )
    assert event is not None
    assert event.payload["disposition"] == "no_change"


def test_communication_is_queued_and_failed_evaluation_retains_request() -> None:
    class Failure:
        def decide(self, invocation: HyperHeartbeatInvocation) -> object:
            _ = invocation
            raise RuntimeError("evaluation failed")

    supervisor = HyperSupervisor(Failure())
    request = _request("request-1", 2, "new evidence")
    response = supervisor.handle_agent_message(
        AgentMessage(
            message_id=request.request_id,
            correlation_id="mission-loop",
            mission_id=request.mission_id,
            plan_revision=1,
            sender="maneuver-control",
            recipient="hyper-agent",
            kind="replan",
            payload={"message": request.reason, "replan_request": request.to_dict()},
        )
    )
    assert response["disposition"] == "pending_hyper_evaluation"
    with pytest.raises(RuntimeError, match="evaluation failed"):
        supervisor.heartbeat(_invocation("request-1"))
    assert supervisor.has_pending("mission-1")


def test_deep_provider_uses_independent_latest_only_agent_inputs() -> None:
    states: list[Mapping[str, object]] = []

    class Agent:
        def invoke(self, state: Mapping[str, object]) -> Mapping[str, object]:
            states.append(state)
            messages = state["messages"]
            assert isinstance(messages, list) and len(messages) == 1
            return {
                "structured_response": {
                    "mission_id": "mission-1",
                    "plan_revision": 1,
                    "disposition": "no_change",
                    "evidence_summary": "The latest evidence is stable.",
                }
            }

    provider = DeepAgentsHyperHeartbeatProvider(Agent(), max_retries=0)
    first = provider.decide(_invocation("periodic:10"))
    second = provider.decide(_invocation("periodic:20"))

    assert first.trigger_identities == ("periodic:10",)
    assert second.trigger_identities == ("periodic:20",)
    assert len(states) == 2
    assert states[0] is not states[1]
