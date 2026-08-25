"""Mission context aggregation and closed-loop coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from onr.agents.maneuver_tools import ManeuverHeartbeatExecutionRecord
from onr.application.transition_intents import TransitionIntentJournal
from onr.contracts.bayesian_belief import BayesianBeliefSnapshot
from onr.contracts.context_coordination import (
    MISSION_SNAPSHOT_SOURCES,
    MissionSnapshot,
    create_source_fact_event,
    mission_snapshot_from_transport_event,
    mission_snapshot_to_transport_event,
    normalize_source_name,
)
from onr.contracts.environment import Perception, perception_from_dict
from onr.contracts.fsm import FSMStatus, Statechart
from onr.contracts.hyper_agent import (
    HyperHeartbeatDecision,
    HyperHeartbeatDisposition,
    HyperHeartbeatInvocation,
)
from onr.contracts.maneuver_control import (
    ManeuverHeartbeatCompletion,
    ManeuverInvocation,
)
from onr.contracts.planning import PlannerPlan
from onr.contracts.planning_evidence import (
    PlannerRevisionEvidence,
    planner_revision_to_transport_event,
)
from onr.contracts.transport import (
    NormalizedPlanTransportEvent,
    TransportEvent,
    normalized_plan_transport_event_to_wire,
)
from onr.ports.operational_log import OperationalLog
from onr.ports.transport import Subscription


@dataclass(frozen=True, slots=True)
class _SourceFact:
    revision: int | None
    reference: str | None
    health: str
    fresh: bool


@dataclass(frozen=True, slots=True)
class ActivePlanRevision:
    """Verified planner and Statechart artifacts for one active revision."""

    planner_plan: PlannerPlan
    planner_plan_reference: str
    statechart: Statechart
    statechart_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.planner_plan, PlannerPlan):
            raise TypeError("active revision requires PlannerPlan")
        if not isinstance(self.statechart, Statechart):
            raise TypeError("active revision requires Statechart")
        if (
            self.planner_plan.mission_id != self.statechart.mission_id
            or self.planner_plan.plan_revision != self.statechart.plan_revision
            or self.planner_plan.mission_snapshot_id
            != self.statechart.mission_snapshot_id
        ):
            raise ValueError(
                "active planner and Statechart identities are inconsistent"
            )
        if not self.planner_plan_reference.strip():
            raise ValueError("PlannerPlan reference must be non-empty")
        if not self.statechart_reference.strip():
            raise ValueError("Statechart reference must be non-empty")


@dataclass(frozen=True, slots=True)
class ClosedLoopRunResult:
    """Safe final audit summary for one deterministic Mission simulation."""

    mission_id: str
    simulated_duration_seconds: float
    tick_count: int
    maneuver_heartbeat_count: int
    hyper_heartbeat_count: int
    physical_actions: tuple[str, ...]
    feedback_count: int
    perception_count: int
    belief_revisions: tuple[int, ...]
    hyper_outcomes: tuple[HyperHeartbeatDecision, ...]
    plan_revisions: tuple[int, ...]
    final_fsm_state: str
    terminal: bool
    environment_triggered_maneuver_heartbeat_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "simulated_duration_seconds": self.simulated_duration_seconds,
            "tick_count": self.tick_count,
            "maneuver_heartbeat_count": self.maneuver_heartbeat_count,
            "environment_triggered_maneuver_heartbeat_count": (
                self.environment_triggered_maneuver_heartbeat_count
            ),
            "hyper_heartbeat_count": self.hyper_heartbeat_count,
            "physical_actions": list(self.physical_actions),
            "feedback_count": self.feedback_count,
            "perception_count": self.perception_count,
            "belief_revisions": list(self.belief_revisions),
            "hyper_outcomes": [item.to_dict() for item in self.hyper_outcomes],
            "plan_revisions": list(self.plan_revisions),
            "final_fsm_state": self.final_fsm_state,
            "terminal": self.terminal,
        }


ReplanWorkflow = Callable[
    [HyperHeartbeatInvocation, int, MissionSnapshot, object],
    ActivePlanRevision | None,
]


class ContextCoordination:
    """Assemble Mission context and own one Mission's closed-loop runtime."""

    def __init__(
        self,
        transport: Any,
        mission_id: str,
        *,
        input_topic: str = "normalized-plans",
        snapshot_topic: str = "mission-snapshots",
        service_id: str = "context-coordination",
        max_retries: int = 3,
        clock: Callable[[], str] | None = None,
        subscription: Subscription | None = None,
        operational_log: OperationalLog | None = None,
        environment: object | None = None,
        fsm_runner: object | None = None,
        maneuver_control: object | None = None,
        hyper_supervisor: object | None = None,
        belief_service: object | None = None,
        replan_workflow: ReplanWorkflow | None = None,
        maneuver_seconds: float = 5,
        hyper_seconds: float = 10,
        simulation_limit_seconds: float = 600,
    ) -> None:
        self._transport = transport
        self.input_topic = input_topic
        self.snapshot_topic = snapshot_topic
        self._clock = clock or _utc_now
        self.operational_log = operational_log
        self.subscription = subscription or Subscription(
            service_id=service_id,
            mission_id=mission_id,
            topic=input_topic,
            max_retries=max_retries,
        )
        if self.subscription.mission_id != mission_id:
            raise ValueError(
                "context coordination subscription mission ID does not match"
            )
        self._facts: dict[str, _SourceFact] = {}
        self._last_snapshot: MissionSnapshot | None = None
        self._environment = environment
        self._fsm_runner = fsm_runner
        self._maneuver_control = maneuver_control
        self._hyper_supervisor = hyper_supervisor
        self._belief_service = belief_service
        self._replan_workflow = replan_workflow
        self._maneuver_seconds = self._positive(maneuver_seconds, "Maneuver interval")
        self._hyper_seconds = self._positive(hyper_seconds, "Hyper interval")
        self._simulation_limit_seconds = self._positive(
            simulation_limit_seconds, "simulation limit"
        )
        self._pending_perceptions: list[Perception] = []
        self._pending_maneuver_triggers: set[str] = set()
        self._transition_intents = TransitionIntentJournal(transport)
        self._restore_latest_snapshot()

    @property
    def latest_snapshot(self) -> MissionSnapshot | None:
        return self._last_snapshot

    @staticmethod
    def subscription_for(
        mission_id: str,
        *,
        input_topic: str = "normalized-plans",
        service_id: str = "context-coordination",
        max_retries: int = 3,
    ) -> Subscription:
        """Build the static subscription that must be registered on an adapter."""

        return Subscription(service_id, mission_id, input_topic, max_retries)

    def handle(
        self, event: TransportEvent | NormalizedPlanTransportEvent
    ) -> MissionSnapshot | None:
        """Consume one normalized-plan or source-fact event."""

        if isinstance(event, NormalizedPlanTransportEvent):
            event = normalized_plan_transport_event_to_wire(event)
        if (
            not isinstance(event, TransportEvent)
            or event.mission_id != self.subscription.mission_id
        ):
            return None
        fact = self._parse_context_event(event)
        if fact is None:
            return None
        source, next_fact = fact
        previous = self._facts.get(source)
        if next_fact.revision is None:
            if previous is None:
                raise _MalformedContextEvent(
                    "source fact is missing its initial revision"
                )
            next_fact = _SourceFact(
                previous.revision,
                previous.reference
                if next_fact.reference is None
                else next_fact.reference,
                next_fact.health,
                next_fact.fresh,
            )
        else:
            if previous is not None and previous.revision is not None:
                if next_fact.revision < previous.revision:
                    return None
                if (
                    source == "bayesian_belief_snapshot"
                    and next_fact.revision == previous.revision
                    and (
                        next_fact.reference is not None
                        and next_fact.reference != previous.reference
                    )
                ):
                    raise _MalformedContextEvent(
                        "belief revision cannot change reference provenance"
                    )
                if next_fact.reference is None:
                    next_fact = _SourceFact(
                        next_fact.revision,
                        previous.reference
                        if next_fact.reference is None
                        else next_fact.reference,
                        next_fact.health,
                        next_fact.fresh,
                    )
        if previous == next_fact:
            return None
        self._facts[source] = next_fact
        snapshot = self._snapshot()
        sequence = self._transport.next_event_sequence(
            self.snapshot_topic, snapshot.mission_id
        )
        self._transport.publish_event(
            self.snapshot_topic,
            mission_snapshot_to_transport_event(
                snapshot,
                event_id=f"mission-snapshot:{snapshot.mission_id}:{snapshot.version}",
                sequence=sequence,
            ),
        )
        self._last_snapshot = snapshot
        if self.operational_log is not None:
            self.operational_log.emit(
                snapshot.mission_id,
                "context-coordination",
                "heartbeat",
                "completed",
                details={"operation": "publish_snapshot", "revision": snapshot.version},
            )
        return snapshot

    handle_event = handle

    def run_once(self, consumer: Any) -> MissionSnapshot | None:
        """Deliver one event through a registered Consumer, like planning commands."""

        delivery = consumer.receive()
        if delivery is None:
            return None
        if not isinstance(
            delivery.message, (TransportEvent, NormalizedPlanTransportEvent)
        ):
            delivery.ack()
            return None
        try:
            snapshot = self.handle(delivery.message)
        except _MalformedContextEvent as exc:
            delivery.nack()
            if self.operational_log is not None:
                self.operational_log.emit(
                    self.subscription.mission_id,
                    "context-coordination",
                    "error",
                    "failed",
                    details={
                        "operation": "consume_event",
                        "error_type": type(exc).__name__,
                    },
                )
            return None
        delivery.ack()
        return snapshot

    def drain_to_latest(self, consumer: Any) -> MissionSnapshot | None:
        """Acknowledge every pending fact and expose only the newest snapshot."""

        latest: MissionSnapshot | None = None
        while True:
            delivery = consumer.receive()
            if delivery is None:
                return latest
            if not isinstance(
                delivery.message, (TransportEvent, NormalizedPlanTransportEvent)
            ):
                delivery.ack()
                continue
            try:
                snapshot = self.handle(delivery.message)
            except _MalformedContextEvent:
                delivery.nack()
                raise
            delivery.ack()
            if snapshot is not None:
                latest = snapshot

    def handle_agent_message(self, message: object) -> Mapping[str, object]:
        """Queue one direct invocation request for the next serialized heartbeat."""

        from onr.contracts.communication import AgentMessage, AgentMessageKind

        if not isinstance(message, AgentMessage):
            raise TypeError("Context Coordination communication requires AgentMessage")
        if (
            message.recipient != "maneuver-control"
            or message.kind is not AgentMessageKind.INVOKE
        ):
            raise ValueError(
                "Context Coordination accepts only Maneuver invoke messages"
            )
        if message.mission_id != self.subscription.mission_id:
            raise ValueError("direct Maneuver invocation belongs to another Mission")
        self._pending_maneuver_triggers.add(message.message_id)
        return {"status": "queued", "message_id": message.message_id}

    def run(self, active_revision: ActivePlanRevision) -> ClosedLoopRunResult:
        """Run the serialized closed loop from one accepted planner revision."""

        self._require_runtime(active_revision)
        environment = cast(Any, self._environment)
        hyper = cast(Any, self._hyper_supervisor)
        replan = cast(ReplanWorkflow, self._replan_workflow)
        mission_id = active_revision.planner_plan.mission_id
        status = self._status_or_activate(active_revision.statechart)
        plan_revisions = [active_revision.planner_plan.plan_revision]
        physical_actions: list[str] = []
        hyper_outcomes: list[HyperHeartbeatDecision] = []
        belief_revisions: list[int] = []
        pending_hyper_outcomes: tuple[HyperHeartbeatDecision, ...] = ()
        maneuver_count = 0
        environment_maneuver_count = 0
        hyper_count = 0
        tick_count = 0

        with self._transport.open_consumer(self.subscription) as context_consumer:
            self.publish_planner_revision(self._revision_evidence(active_revision))
            self._publish_runtime_source_facts(status)
            snapshot = self._drain_required(context_consumer)
            self._append_belief_revisions(belief_revisions)

            while True:
                now = float(environment.mission_time_seconds)
                periodic_maneuver = self._due(now, self._maneuver_seconds)
                triggers = set(self._pending_maneuver_triggers)
                if periodic_maneuver:
                    triggers.add(f"periodic:{now:g}")
                if triggers:
                    if any(item.startswith("environment:") for item in triggers):
                        environment_maneuver_count += 1
                    self._pending_maneuver_triggers.difference_update(triggers)
                    snapshot, status = self._run_maneuver_heartbeat(
                        snapshot,
                        active_revision,
                        maneuver_count,
                        tuple(sorted(triggers)),
                        pending_hyper_outcomes,
                        context_consumer,
                        physical_actions,
                        belief_revisions,
                    )
                    pending_hyper_outcomes = ()
                    maneuver_count += 1

                periodic_hyper = now > 0 and self._due(now, self._hyper_seconds)
                requested_hyper = bool(hyper.has_pending(mission_id))
                if periodic_hyper or requested_hyper:
                    hyper_triggers = []
                    if periodic_hyper:
                        hyper_triggers.append(f"periodic:{now:g}")
                    hyper_triggers.extend(hyper.pending_request_identities(mission_id))
                    hyper_invocation = self._hyper_invocation(
                        snapshot, active_revision, tuple(hyper_triggers)
                    )
                    decision = hyper.heartbeat(hyper_invocation)
                    if not isinstance(decision, HyperHeartbeatDecision):
                        raise TypeError("Hyper heartbeat returned invalid decision")
                    hyper_count += 1
                    hyper_outcomes.append(decision)
                    pending_hyper_outcomes += (decision,)
                    if decision.disposition == HyperHeartbeatDisposition.REPLAN:
                        next_revision = active_revision.planner_plan.plan_revision + 1
                        planning_view = environment.heartbeat()
                        snapshot = self._drain_required(
                            context_consumer, fallback=snapshot
                        )
                        replacement = replan(
                            hyper_invocation,
                            next_revision,
                            snapshot,
                            planning_view,
                        )
                        if replacement is not None:
                            if replacement.planner_plan.plan_revision != next_revision:
                                raise ValueError(
                                    "replan workflow returned wrong revision"
                                )
                            self.publish_planner_revision(
                                self._revision_evidence(replacement)
                            )
                            snapshot = self._drain_required(context_consumer)
                            status = self._activate(replacement.statechart)
                            self._transition_intents.invalidate_latest(mission_id)
                            active_revision = replacement
                            plan_revisions.append(next_revision)
                            snapshot, status = self._run_maneuver_heartbeat(
                                snapshot,
                                active_revision,
                                maneuver_count,
                                (f"replan-activated:{next_revision}",),
                                pending_hyper_outcomes,
                                context_consumer,
                                physical_actions,
                                belief_revisions,
                            )
                            pending_hyper_outcomes = ()
                            maneuver_count += 1

                if status.active_state in active_revision.statechart.terminal_states:
                    break
                if now >= self._simulation_limit_seconds:
                    break

                tick = environment.tick()
                tick_count += 1
                self._pending_perceptions.extend(
                    perception_from_dict(event.payload)
                    for event in tick.perception_events
                )
                self._pending_maneuver_triggers.update(
                    f"environment:{event.event_id}" for event in tick.feedback_events
                )
                snapshot = self._drain_required(context_consumer, fallback=snapshot)

        self._append_belief_revisions(belief_revisions)
        terminal = status.active_state in active_revision.statechart.terminal_states
        return ClosedLoopRunResult(
            mission_id=mission_id,
            simulated_duration_seconds=float(environment.mission_time_seconds),
            tick_count=tick_count,
            maneuver_heartbeat_count=maneuver_count,
            hyper_heartbeat_count=hyper_count,
            physical_actions=tuple(physical_actions),
            feedback_count=self._transport.next_event_sequence(
                environment.feedback_topic, mission_id
            ),
            perception_count=self._transport.next_event_sequence(
                environment.perception_topic, mission_id
            ),
            belief_revisions=tuple(belief_revisions),
            hyper_outcomes=tuple(hyper_outcomes),
            plan_revisions=tuple(plan_revisions),
            final_fsm_state=status.active_state,
            terminal=terminal,
            environment_triggered_maneuver_heartbeat_count=(environment_maneuver_count),
        )

    def _run_maneuver_heartbeat(
        self,
        snapshot: MissionSnapshot,
        active_revision: ActivePlanRevision,
        count: int,
        triggers: tuple[str, ...],
        hyper_outcomes: tuple[HyperHeartbeatDecision, ...],
        context_consumer: object,
        physical_actions: list[str],
        belief_revisions: list[int],
    ) -> tuple[MissionSnapshot, FSMStatus]:
        """Publish fresh authority, invoke Maneuver once, and drain its effects."""

        maneuver = cast(Any, self._maneuver_control)
        self._publish_runtime_source_facts(self._required_status())
        snapshot = self._drain_required(context_consumer, fallback=snapshot)
        invocation = self._maneuver_invocation(
            snapshot,
            active_revision,
            count,
            triggers,
            hyper_outcomes,
        )
        completion = maneuver.heartbeat(invocation)
        if not isinstance(completion, ManeuverHeartbeatCompletion):
            raise TypeError("Maneuver heartbeat returned invalid completion")
        record = maneuver.last_execution_record
        if isinstance(record, ManeuverHeartbeatExecutionRecord):
            physical_actions.extend(
                item.physical_intent.action
                for item in record.decisions
                if item.physical_intent is not None
            )
            if any(
                item.name == "ingest_perceptions" and item.successful
                for item in record.executions
            ):
                del self._pending_perceptions[: len(invocation.pending_perceptions)]
        status = self._required_status()
        self._publish_runtime_source_facts(status)
        snapshot = self._drain_required(context_consumer, fallback=snapshot)
        self._append_belief_revisions(belief_revisions)
        return snapshot, status

    @staticmethod
    def _positive(value: object, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{label} must be positive")
        result = float(value)
        if result <= 0:
            raise ValueError(f"{label} must be positive")
        return result

    @staticmethod
    def _due(now: float, interval: float) -> bool:
        quotient = now / interval
        return abs(quotient - round(quotient)) <= 1e-9

    def _require_runtime(self, active_revision: ActivePlanRevision) -> None:
        if not isinstance(active_revision, ActivePlanRevision):
            raise TypeError("Context Coordination requires ActivePlanRevision")
        required = {
            "environment": self._environment,
            "FSM Runner": self._fsm_runner,
            "Maneuver Control": self._maneuver_control,
            "Hyper supervisor": self._hyper_supervisor,
            "belief service": self._belief_service,
            "replan workflow": self._replan_workflow,
        }
        missing = [name for name, dependency in required.items() if dependency is None]
        if missing:
            raise RuntimeError(
                "Context Coordination runtime dependencies are missing: "
                + ", ".join(missing)
            )
        environment = cast(Any, self._environment)
        if environment.mission_id != active_revision.planner_plan.mission_id:
            raise ValueError("closed-loop Mission identities do not match")
        if abs(float(environment.tick_seconds) - 0.5) > 1e-9:
            raise ValueError("Context Coordination requires 0.5-second ticks")

    def _status_or_activate(self, chart: Statechart) -> FSMStatus:
        status = self._status()
        return status if status is not None else self._activate(chart)

    def _status(self) -> FSMStatus | None:
        method = getattr(self._fsm_runner, "status", None)
        if not callable(method):
            raise TypeError("Context Coordination FSM Runner must expose status")
        return cast(FSMStatus | None, asyncio.run(cast(Any, method())))

    def _required_status(self) -> FSMStatus:
        status = self._status()
        if not isinstance(status, FSMStatus):
            raise TypeError("Context Coordination FSM Runner is not active")
        return status

    def _activate(self, chart: Statechart) -> FSMStatus:
        method = getattr(self._fsm_runner, "activate", None)
        if not callable(method):
            raise TypeError("Context Coordination FSM Runner must expose activate")
        status = asyncio.run(cast(Any, method(chart)))
        if not isinstance(status, FSMStatus):
            raise TypeError("FSM activation returned invalid status")
        return status

    def _drain_required(
        self, consumer: object, *, fallback: MissionSnapshot | None = None
    ) -> MissionSnapshot:
        latest = self.drain_to_latest(consumer)
        snapshot = latest or fallback or self.latest_snapshot
        if not isinstance(snapshot, MissionSnapshot):
            raise TypeError("Context Coordination has no current Mission Snapshot")
        return snapshot

    def _resolve_event(
        self, reference: str | None, event_kind: str, label: str
    ) -> TransportEvent:
        if reference is None:
            raise RuntimeError(f"Mission Snapshot has no {label} reference")
        event = self._transport.get_event(reference)
        if not isinstance(event, TransportEvent) or event.event_kind != event_kind:
            raise RuntimeError(f"Mission Snapshot {label} reference is unresolved")
        return event

    def _resolve_status(self, snapshot: MissionSnapshot) -> FSMStatus:
        event = self._resolve_event(snapshot.fsm_status, "fsm-status", "FSM status")
        return FSMStatus.from_dict(event.payload)

    def _resolve_environment(self, snapshot: MissionSnapshot) -> Mapping[str, object]:
        event = self._resolve_event(
            snapshot.environment_data, "environment_data", "environment data"
        )
        if snapshot.active_maneuver is not None:
            active = self._resolve_event(
                snapshot.active_maneuver,
                "environment_data",
                "active maneuver",
            )
            if active.event_id != event.event_id:
                raise ValueError(
                    "Mission Snapshot environment and active maneuver are incoherent"
                )
        return {
            key: value for key, value in event.payload.items() if key != "perceptions"
        }

    def _resolve_belief(
        self, snapshot: MissionSnapshot
    ) -> BayesianBeliefSnapshot | None:
        reference = snapshot.bayesian_belief_snapshot
        if reference is None:
            return None
        belief = cast(Any, self._belief_service).load_snapshot_reference(reference)
        if belief.mission_id != snapshot.mission_id:
            raise ValueError("resolved belief Mission ID does not match snapshot")
        if (
            belief.belief_revision
            != snapshot.source_revisions["bayesian_belief_snapshot"]
        ):
            raise ValueError("resolved belief revision does not match snapshot")
        return cast(BayesianBeliefSnapshot, belief)

    def _maneuver_invocation(
        self,
        snapshot: MissionSnapshot,
        revision: ActivePlanRevision,
        count: int,
        triggers: tuple[str, ...],
        hyper_outcomes: tuple[HyperHeartbeatDecision, ...],
    ) -> ManeuverInvocation:
        if snapshot.plan_reference != revision.planner_plan_reference:
            raise ValueError("Mission Snapshot does not reference the active plan")
        status = self._resolve_status(snapshot)
        intent = self._transition_intents.current(
            status, invalidate_stale=True
        )
        return ManeuverInvocation(
            request_id=f"maneuver-heartbeat:{snapshot.mission_id}:{count}",
            correlation_id=f"mission-loop:{snapshot.mission_id}",
            mission_id=snapshot.mission_id,
            plan_revision=revision.planner_plan.plan_revision,
            statechart_reference=revision.statechart_reference,
            fsm_context=self._transition_intents.focused_context(status, intent),
            environment_data=self._resolve_environment(snapshot),
            trigger_identities=triggers,
            pending_perceptions=tuple(self._pending_perceptions),
            available_recipients=("hyper-agent",),
            planning_snapshot=snapshot,
            hyper_outcomes=hyper_outcomes,
        )

    def _hyper_invocation(
        self,
        snapshot: MissionSnapshot,
        revision: ActivePlanRevision,
        triggers: tuple[str, ...],
    ) -> HyperHeartbeatInvocation:
        if snapshot.plan_reference != revision.planner_plan_reference:
            raise ValueError("Mission Snapshot does not reference the active plan")
        return HyperHeartbeatInvocation(
            mission_id=snapshot.mission_id,
            plan_revision=revision.planner_plan.plan_revision,
            trigger_identities=triggers,
            mission_snapshot=snapshot,
            planner_plan_reference=revision.planner_plan_reference,
            statechart_reference=revision.statechart_reference,
            fsm_status=self._resolve_status(snapshot),
            environment_data=self._resolve_environment(snapshot),
            belief_snapshot=self._resolve_belief(snapshot),
        )

    def _append_belief_revisions(self, revisions: list[int]) -> None:
        current = cast(Any, self._belief_service).load_current_snapshot()
        if current is None:
            return
        previous = revisions[-1] if revisions else 0
        revisions.extend(range(previous + 1, current.belief_revision + 1))

    @staticmethod
    def _revision_evidence(revision: ActivePlanRevision) -> PlannerRevisionEvidence:
        return PlannerRevisionEvidence(
            planner_plan=revision.planner_plan,
            planner_plan_reference=revision.planner_plan_reference,
            accepted_statechart_reference=revision.statechart_reference,
            mission_snapshot_id=revision.planner_plan.mission_snapshot_id,
            plan_revision=revision.planner_plan.plan_revision,
        )

    def _publish_runtime_source_facts(self, status: FSMStatus) -> None:
        fsm_event = self._transport.latest_event(
            "fsm-status", status.mission_id, event_kind="fsm-status"
        )
        if fsm_event is not None:
            self.publish_source_fact(
                "fsm_status",
                fsm_event.sequence,
                reference=fsm_event.event_id,
            )
        environment = cast(Any, self._environment)
        environment_event = environment.latest_environment_event
        if environment_event is not None and environment.current_maneuver is not None:
            self.publish_source_fact(
                "active_maneuver",
                environment_event.sequence,
                reference=environment_event.event_id,
            )

    def publish_planner_revision(
        self, evidence: PlannerRevisionEvidence
    ) -> TransportEvent:
        """Publish accepted planner-native workflow evidence to this input stream."""

        if not isinstance(evidence, PlannerRevisionEvidence):
            raise TypeError("Context Coordination requires PlannerRevisionEvidence")
        if evidence.mission_id != self.subscription.mission_id:
            raise ValueError("planner revision evidence belongs to another Mission")
        sequence = self._transport.next_event_sequence(
            self.input_topic, evidence.mission_id
        )
        return self._transport.publish_event(
            self.input_topic,
            planner_revision_to_transport_event(
                evidence,
                event_id=(
                    f"planner-revision:{evidence.mission_id}:{evidence.plan_revision}"
                ),
                sequence=sequence,
            ),
        )

    def publish_source_fact(
        self,
        source: str,
        revision: int,
        *,
        reference: str | None = None,
        health: str | bool = "healthy",
        fresh: bool = True,
    ) -> TransportEvent:
        """Publish a revision/health fact for a non-plan authority source."""

        source = normalize_source_name(source)
        if source == "plan":
            raise ValueError("the plan source is published by normalized-plan events")
        sequence = self._transport.next_event_sequence(
            self.input_topic, self.subscription.mission_id
        )
        event = create_source_fact_event(
            self.subscription.mission_id,
            source,
            revision,
            event_id=f"source-fact:{self.subscription.mission_id}:{source}:{sequence}",
            sequence=sequence,
            reference=reference,
            health=health,
            fresh=fresh,
        )
        return self._transport.publish_event(self.input_topic, event)

    publish_source_revision = publish_source_fact
    publish_source_health = publish_source_fact

    def _parse_context_event(
        self, event: TransportEvent
    ) -> tuple[str, _SourceFact] | None:
        payload = event.payload
        if event.event_kind == "planner-revision":
            try:
                evidence = PlannerRevisionEvidence.from_dict(payload)
            except (TypeError, ValueError) as exc:
                raise _MalformedContextEvent(
                    "planner revision event is malformed"
                ) from exc
            if evidence.mission_id != event.mission_id:
                raise _MalformedContextEvent(
                    "planner revision event Mission ID does not match"
                )
            return "plan", _SourceFact(
                evidence.plan_revision,
                evidence.planner_plan_reference,
                "healthy",
                True,
            )
        if event.event_kind in {"normalized-plan", "normalized_plan"}:
            required_fields = {
                "mission_snapshot_id",
                "plan_revision",
                "planner_choice",
                "source_authority",
                "outcome",
                "normalized_plan",
            }
            if set(payload) not in (
                required_fields,
                required_fields | {"mission_id"},
                required_fields | {"correlation_id"},
                required_fields | {"mission_id", "correlation_id"},
            ):
                raise _MalformedContextEvent(
                    "normalized-plan event has invalid wire fields"
                )
            revision = payload.get("plan_revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise _MalformedContextEvent(
                    "normalized-plan event has an invalid plan revision"
                )
            reference = event.event_id
            if reference is not None and not isinstance(reference, str):
                raise _MalformedContextEvent(
                    "normalized-plan event has an invalid reference"
                )
            return "plan", _SourceFact(revision, reference, "healthy", True)
        if event.event_kind == "belief.updated":
            required = {
                "source",
                "revision",
                "reference",
                "content_sha256",
                "health",
                "fresh",
            }
            if (
                set(payload) != required
                or payload.get("source") != "bayesian_belief_snapshot"
            ):
                raise _MalformedContextEvent(
                    "belief.updated event has invalid public fields"
                )
            revision = payload.get("revision")
            reference = payload.get("reference")
            content_hash = payload.get("content_sha256")
            health = payload.get("health")
            fresh = payload.get("fresh")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise _MalformedContextEvent(
                    "belief.updated event has an invalid revision"
                )
            if not isinstance(reference, str) or not reference.strip():
                raise _MalformedContextEvent(
                    "belief.updated event has an invalid reference"
                )
            if (
                not isinstance(content_hash, str)
                or len(content_hash) != 64
                or any(
                    character not in "0123456789abcdef" for character in content_hash
                )
                or not reference.endswith(f"#sha256={content_hash}")
            ):
                raise _MalformedContextEvent(
                    "belief.updated event has an invalid content hash"
                )
            if (
                not isinstance(health, str)
                or not health.strip()
                or not isinstance(fresh, bool)
            ):
                raise _MalformedContextEvent(
                    "belief.updated event has invalid health or freshness"
                )
            return "bayesian_belief_snapshot", _SourceFact(
                revision, reference, health, fresh
            )
        if event.event_kind not in {
            "source-fact",
            "source-revision",
            "source-health",
            "environment_data",
            "bayesian_belief_snapshot",
            "fsm_status",
            "active_maneuver",
            "bayesian-belief-snapshot",
            "fsm-status",
            "active-maneuver",
        }:
            return None
        raw_source = payload.get("source", event.event_kind)
        try:
            source = normalize_source_name(raw_source)
        except ValueError:
            raise _MalformedContextEvent("source fact has an invalid source") from None
        raw_revision = payload.get("revision", payload.get("source_revision"))
        if raw_revision is not None and (
            isinstance(raw_revision, bool)
            or not isinstance(raw_revision, int)
            or raw_revision < 0
        ):
            raise _MalformedContextEvent("source fact has an invalid revision")
        reference = payload.get("reference", payload.get("source_reference"))
        if reference is not None and not isinstance(reference, str):
            raise _MalformedContextEvent("source fact has an invalid reference")
        raw_health = payload.get("health", "healthy")
        if isinstance(raw_health, bool):
            health = "healthy" if raw_health else "unhealthy"
        elif isinstance(raw_health, str) and raw_health.strip():
            health = raw_health
        else:
            raise _MalformedContextEvent("source fact has an invalid health")
        fresh = payload.get("fresh", payload.get("freshness", True))
        if not isinstance(fresh, bool):
            raise _MalformedContextEvent("source fact has invalid freshness")
        return source, _SourceFact(raw_revision, reference, health, fresh)

    def _restore_latest_snapshot(self) -> None:
        latest_event = getattr(self._transport, "latest_event", None)
        if not callable(latest_event):
            return
        event = latest_event(
            self.snapshot_topic,
            self.subscription.mission_id,
            event_kind="mission-snapshot",
        )
        if event is None:
            return
        if not isinstance(event, TransportEvent):
            return
        try:
            snapshot = mission_snapshot_from_transport_event(
                cast(TransportEvent, event)
            )
        except ValueError:
            return
        self._last_snapshot = snapshot
        for source in MISSION_SNAPSHOT_SOURCES:
            revision = snapshot.source_revisions[source]
            if revision is not None:
                self._facts[source] = _SourceFact(
                    revision,
                    snapshot.source_references[source],
                    snapshot.source_health[source],
                    snapshot.source_freshness[source],
                )

    def _snapshot(self) -> MissionSnapshot:
        revisions = {
            source: self._facts[source].revision if source in self._facts else None
            for source in MISSION_SNAPSHOT_SOURCES
        }
        references = {
            source: self._facts[source].reference if source in self._facts else None
            for source in MISSION_SNAPSHOT_SOURCES
        }
        health = {
            source: self._facts[source].health if source in self._facts else "missing"
            for source in MISSION_SNAPSHOT_SOURCES
        }
        freshness = {
            source: self._facts[source].fresh if source in self._facts else False
            for source in MISSION_SNAPSHOT_SOURCES
        }
        plan_revision = revisions["plan"]
        return MissionSnapshot(
            mission_id=self.subscription.mission_id,
            version=self._next_version(),
            created_at=self._clock(),
            plan_revision=plan_revision,
            plan_reference=references["plan"],
            environment_data=references["environment_data"],
            bayesian_belief_snapshot=references["bayesian_belief_snapshot"],
            fsm_status=references["fsm_status"],
            active_maneuver=references["active_maneuver"],
            source_revisions=revisions,
            source_references=references,
            source_health=health,
            source_freshness=freshness,
        )

    def _next_version(self) -> int:
        next_sequence = self._transport.next_event_sequence(
            self.snapshot_topic, self.subscription.mission_id
        )
        return max(
            next_sequence + 1,
            (self._last_snapshot.version + 1) if self._last_snapshot else 1,
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class _MalformedContextEvent(ValueError):
    """A recognized event whose fact cannot be safely applied."""


ContextCoordinationHandler = ContextCoordination

__all__ = [
    "ActivePlanRevision",
    "ClosedLoopRunResult",
    "ContextCoordination",
    "ContextCoordinationHandler",
]
