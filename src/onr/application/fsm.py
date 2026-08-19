"""Pure asynchronous-facing Statechart activation and execution service."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Callable, Protocol, cast

from onr.contracts.fsm import (
    FSMExecutionRecord,
    FSMEvent,
    FSMStatus,
    ManeuverDecision,
    ManeuverFeedback,
    Statechart,
    TransitionCandidate,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanningProfile,
    JsonScalar,
    ScheduledManeuver,
    SymbolicManeuver,
    SymbolicMissionSpec,
    SymbolicPlanStep,
    TemporalManeuver,
)
from onr.contracts.transport import (
    NormalizedPlanTransportEvent,
    TransportEvent,
)
from onr.ports.fsm import FSMStateStore, FSMTransport
from onr.ports.operational_log import OperationalLog
from onr.ports.transport import Consumer, Delivery, Subscription


class _Receivable(Protocol):
    def receive(self) -> Delivery | None: ...


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _text_field(value: Mapping[str, object], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return item


def _int_field(value: Mapping[str, object], key: str, label: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{label} must be an integer")
    return item


def _scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    raise ValueError(f"{label} must be a JSON scalar")


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings")
    return tuple(value)


def _objects(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an array")
    return tuple(_object(item, label) for item in value)


def _intent(value: object) -> ManeuverIntent:
    intent = _object(value, "normalized maneuver intent")
    if set(intent) != {"action", "parameters"}:
        raise ValueError("normalized maneuver intent is invalid")
    parameters = _object(intent["parameters"], "normalized maneuver parameters")
    return ManeuverIntent(
        action=_text_field(intent, "action", "maneuver action"),
        parameters=tuple(
            ManeuverParameter(name=name, value=_scalar(parameter, "maneuver parameter"))
            for name, parameter in parameters.items()
        ),
    )


def _normalized_plan(value: object) -> NormalizedPlan:
    """Rehydrate only the existing validated planning contract from transport JSON."""

    return NormalizedPlan.from_dict(_object(value, "normalized plan payload"))

    plan = _object(value, "normalized plan payload")
    expected = {
        "mission_spec",
        "plan_revision",
        "mission_snapshot_id",
        "planner_choice",
        "outcome",
        "maneuvers",
    }
    if set(plan) != expected:
        raise ValueError("normalized plan contains unknown or missing fields")
    spec = _object(plan["mission_spec"], "normalized plan mission specification")
    choice = PlannerChoice.from_dict(_object(plan["planner_choice"], "planner choice"))
    spec_choice = PlannerChoice.from_dict(_object(spec["planner_choice"], "mission planner choice"))
    if choice != spec_choice:
        raise ValueError("normalized plan planner choices do not match")
    intents = _objects(plan["maneuvers"], "normalized plan maneuvers")
    spec_maneuvers = _objects(spec["maneuvers"], "mission specification maneuvers")
    if choice.planning_profile is PlanningProfile.TEMPORAL:
        mission = MissionSpec(
            mission_id=_text_field(spec, "mission_id", "mission ID"),
            objective=_text_field(spec, "objective", "mission objective"),
            planner_choice=spec_choice,
            maneuvers=tuple(
                TemporalManeuver(
                    maneuver_id=_text_field(item, "maneuver_id", "maneuver ID"),
                    intent=_intent(item["intent"]),
                    dependencies=_strings(item["dependencies"], "maneuver dependencies"),
                    duration=_int_field(item, "duration", "maneuver duration"),
                )
                for item in spec_maneuvers
            ),
            horizon=_int_field(spec, "horizon", "mission horizon"),
            source_authority=_text_field(spec, "source_authority", "source authority"),
        )
        maneuvers = tuple(
            ScheduledManeuver(
                maneuver_id=_text_field(item, "maneuver_id", "scheduled maneuver ID"),
                intent=_intent(item["intent"]),
                dependencies=_strings(item["dependencies"], "scheduled dependencies"),
                start=_int_field(item, "start", "scheduled start"),
                duration=_int_field(item, "duration", "scheduled duration"),
            )
            for item in intents
        )
    else:
        mission = SymbolicMissionSpec(
            mission_id=_text_field(spec, "mission_id", "mission ID"),
            objective=_text_field(spec, "objective", "mission objective"),
            planner_choice=spec_choice,
            maneuvers=tuple(
                SymbolicManeuver(
                    maneuver_id=_text_field(item, "maneuver_id", "maneuver ID"),
                    intent=_intent(item["intent"]),
                    dependencies=_strings(item["dependencies"], "maneuver dependencies"),
                    cost=_int_field(item, "cost", "maneuver cost"),
                )
                for item in spec_maneuvers
            ),
            source_authority=_text_field(spec, "source_authority", "source authority"),
            domain_revision=(
                _int_field(spec, "domain_revision", "domain revision")
                if "domain_revision" in spec
                else 1
            ),
        )
        maneuvers = tuple(
            SymbolicPlanStep(
                step_index=_int_field(item, "step_index", "symbolic step index"),
                maneuver_id=_text_field(item, "maneuver_id", "symbolic maneuver ID"),
                intent=_intent(item["intent"]),
                dependencies=_strings(item["dependencies"], "symbolic dependencies"),
                cost=_int_field(item, "cost", "symbolic step cost"),
            )
            for item in intents
        )
    return NormalizedPlan(
        mission_spec=mission,
        plan_revision=_int_field(plan, "plan_revision", "plan revision"),
        mission_snapshot_id=_text_field(plan, "mission_snapshot_id", "mission snapshot ID"),
        planner_choice=choice,
        outcome=PlanningOutcome(_text_field(plan, "outcome", "planning outcome")),
        maneuvers=maneuvers,
    )


def _plan_from_message(message: object) -> NormalizedPlan:
    if isinstance(message, NormalizedPlanTransportEvent):
        return message.normalized_plan
    if isinstance(message, TransportEvent):
        if message.event_kind != "normalized-plan":
            raise ValueError("FSM Runner requires a normalized-plan event")
        payload = _object(message.payload, "normalized-plan transport payload")
        required = {
            "mission_id",
            "mission_snapshot_id",
            "plan_revision",
            "planner_choice",
            "source_authority",
            "outcome",
            "normalized_plan",
            "normalized_plan_document",
            "normalized_plan_sha256",
        }
        if not required.issubset(payload):
            raise ValueError("normalized-plan transport payload is missing provenance")
        plan = _normalized_plan(payload["normalized_plan"])
        document = payload["normalized_plan_document"]
        digest = payload["normalized_plan_sha256"]
        if not isinstance(document, str) or not isinstance(digest, str):
            raise ValueError("normalized-plan transport provenance has invalid types")
        if document != plan.to_canonical_json():
            raise ValueError("normalized-plan transport document does not match the plan")
        if digest != hashlib.sha256(document.encode("utf-8")).hexdigest():
            raise ValueError("normalized-plan transport hash does not match the document")
        if payload["mission_id"] != message.mission_id or plan.mission_id != message.mission_id:
            raise ValueError("normalized-plan transport mission ID does not match")
        if _int_field(payload, "plan_revision", "plan revision") != plan.plan_revision:
            raise ValueError("normalized-plan transport plan revision does not match")
        if _text_field(payload, "mission_snapshot_id", "mission snapshot ID") != plan.mission_snapshot_id:
            raise ValueError("normalized-plan transport snapshot ID does not match")
        if PlannerChoice.from_dict(_object(payload["planner_choice"], "transport planner choice")) != plan.planner_choice:
            raise ValueError("normalized-plan transport planner choice does not match")
        if _text_field(payload, "source_authority", "source authority") != plan.source_authority:
            raise ValueError("normalized-plan transport source authority does not match")
        if _text_field(payload, "outcome", "planning outcome") != str(plan.outcome):
            raise ValueError("normalized-plan transport outcome does not match")
        return plan
    if isinstance(message, NormalizedPlan):
        return message
    raise TypeError("FSM Runner requires a Normalized Plan transport event")


@dataclass
class InMemoryFSMStateStore:
    """Small JSON-backed store useful for application tests and composition."""

    statechart_json: str | None = None
    execution_record_json: str | None = None

    def load_statechart(self) -> Statechart | None:
        return Statechart.from_json(self.statechart_json) if self.statechart_json else None

    def load_execution_record(self) -> FSMExecutionRecord | None:
        return (
            FSMExecutionRecord.from_json(self.execution_record_json)
            if self.execution_record_json
            else None
        )

    def save_statechart(self, statechart: Statechart) -> None:
        self.statechart_json = statechart.to_canonical_json()

    def save_execution_record(self, record: FSMExecutionRecord) -> None:
        self.execution_record_json = record.to_canonical_json()


class FSMRunner:
    """Mechanically activates plans and applies explicitly enabled events.

    ``lifecycle_facts`` and ``maneuver_decision`` are evidence supplied by
    other services.  The runner only checks that both are present for a
    symbolic edge; it never derives or mutates lifecycle state.
    """

    def __init__(
        self,
        transport: FSMTransport,
        *,
        store: FSMStateStore | None = None,
        status_topic: str = "fsm-status",
        clock: Callable[[], int | float] | None = None,
        subscription: Subscription | None = None,
        operational_log: OperationalLog | None = None,
    ) -> None:
        self.transport = transport
        self.store = store or InMemoryFSMStateStore()
        self.status_topic = status_topic
        self.clock = clock or (lambda: 0)
        self.subscription = subscription
        self.operational_log = operational_log
        self._chart = self.store.load_statechart()
        self._record = self.store.load_execution_record()
        if (self._chart is None) != (self._record is None):
            raise RuntimeError("FSM Statechart and Execution Record must be persisted together")
        if self._chart is not None and self._record is not None:
            if self._chart.mission_id != self._record.mission_id:
                raise RuntimeError("persisted FSM mission IDs do not match")
            if self._chart.plan_revision != self._record.plan_revision:
                raise RuntimeError("persisted FSM plan revisions do not match")
            if self._chart.statechart_revision != self._record.statechart_revision:
                raise RuntimeError("persisted FSM Statechart revisions do not match")
            configuration = self._record.active_configuration
            if (
                not configuration
                or self._record.active_state not in configuration
                or any(state not in self._chart.states for state in configuration)
                or self._record.active_state not in self._chart.states
            ):
                raise RuntimeError("persisted FSM configuration references undeclared state")
            if self._record.superseded_plan_revision is None:
                if self._record.retained_maneuver_ids or self._record.superseded_maneuver_ids:
                    raise RuntimeError("persisted FSM retained maneuvers lack a superseded revision")
            elif self._record.superseded_plan_revision >= self._record.plan_revision:
                raise RuntimeError("persisted FSM superseded revision is not older than active plan")
            if self._record.retained_maneuver_ids != self._record.superseded_maneuver_ids:
                raise RuntimeError("persisted FSM retained maneuver visibility is inconsistent")
        if self.subscription is not None and self._chart is not None and self.subscription.mission_id != self._chart.mission_id:
            raise ValueError("FSM subscription mission ID does not match persisted state")
        if self.subscription is None and self._chart is not None:
            self.subscription = self.subscription_for(self._chart.mission_id)
        self._superseded_plan_revision: int | None = (
            self._record.superseded_plan_revision if self._record else None
        )
        self._superseded_maneuver_ids: tuple[str, ...] = (
            self._record.retained_maneuver_ids if self._record else ()
        )
        self._lock = asyncio.Lock()
        self._clock_override: int | float | None = None

    @staticmethod
    def subscription_for(
        mission_id: str,
        *,
        service_id: str = "fsm-runner",
        topic: str = "normalized-plans",
    ) -> Subscription:
        return Subscription(service_id=service_id, mission_id=mission_id, topic=topic)

    async def handle(self, message: object) -> FSMStatus:
        """Activate a Normalized Plan event or return the current status."""

        async with self._lock:
            plan = _plan_from_message(message)
            if self.subscription is None:
                self.subscription = self.subscription_for(plan.mission_id)
            elif self.subscription.mission_id != plan.mission_id:
                raise ValueError("FSM subscription mission ID does not match plan")
            chart = Statechart.from_normalized_plan(plan)
            if self._chart is not None:
                if chart.plan_revision < self._chart.plan_revision:
                    raise ValueError("FSM Runner cannot regress to an older plan revision")
                if chart.plan_revision == self._chart.plan_revision:
                    if chart != self._chart:
                        raise ValueError("same plan revision has different Statechart content")
                    return await self._publish_status("updated", publish=False)
                self._superseded_plan_revision = self._chart.plan_revision
                self._superseded_maneuver_ids = tuple(
                    item.maneuver_id
                    for item in self._chart.transitions
                    if item.maneuver_id is not None
                )
            self._chart = chart
            self._record = FSMExecutionRecord(
                mission_id=chart.mission_id,
                plan_revision=chart.plan_revision,
                statechart_revision=chart.plan_revision,
                active_state=chart.entry_state,
                active_configuration=(chart.entry_state,),
                superseded_plan_revision=self._superseded_plan_revision,
                superseded_maneuver_ids=self._superseded_maneuver_ids,
                retained_maneuver_ids=self._superseded_maneuver_ids,
                record_revision=(self._record.record_revision + 1 if self._record else 1),
            )
            self.store.save_statechart(chart)
            self.store.save_execution_record(self._record)
            return await self._publish_status(
                "superseded" if self._superseded_plan_revision is not None else "initialized"
            )

    async def activate(self, message: object) -> FSMStatus:
        return await self.handle(message)

    async def run_once(self, consumer_or_message: Consumer | object) -> FSMStatus | None:
        """Process one transport delivery, acknowledging only after activation."""

        if hasattr(consumer_or_message, "receive"):
            receiver = cast(_Receivable, consumer_or_message)
            delivery = receiver.receive()
            if delivery is None:
                return None
            try:
                result = await self.handle(delivery.message)
            except Exception:
                delivery.nack()
                if self.operational_log is not None:
                    mission_id = self.subscription.mission_id if self.subscription is not None else "unknown"
                    self.operational_log.emit(
                        mission_id,
                        "fsm-runner",
                        "error",
                        "failed",
                        details={"operation": "run_once", "error_type": "fsm_error"},
                    )
                raise
            delivery.ack()
            return result
        return await self.handle(consumer_or_message)

    async def transition(
        self,
        event: str,
        *,
        lifecycle_facts: object = None,
        maneuver_decision: object = None,
        feedback: object = None,
        decision: object = None,
    ) -> FSMStatus:
        """Apply one enabled event and publish the resulting status."""
        return await self.apply(
            event,
            lifecycle_facts=lifecycle_facts or feedback,
            maneuver_decision=maneuver_decision or decision,
        )

    async def apply(
        self,
        candidate: TransitionCandidate | str,
        *inputs: object,
        event: FSMEvent | None = None,
        decision: object = None,
        feedback: object = None,
        lifecycle_facts: object = None,
        maneuver_decision: object = None,
    ) -> FSMStatus:
        """Apply one currently enabled candidate and supplied evidence."""

        async with self._lock:
            if self._chart is None or self._record is None:
                raise RuntimeError("FSM Runner is not initialized")
            event_input = event
            decision_input = maneuver_decision if maneuver_decision is not None else decision
            feedback_input = lifecycle_facts if lifecycle_facts is not None else feedback
            for item in inputs:
                if isinstance(item, FSMEvent):
                    event_input = item
                elif isinstance(item, ManeuverDecision):
                    decision_input = item
                elif isinstance(item, ManeuverFeedback):
                    feedback_input = item
            event_name = None
            if event_input is not None:
                event_name = event_input.payload.get(
                    "event", event_input.payload.get("transition")
                )
            if event_name is None and isinstance(decision_input, ManeuverDecision):
                event_name = decision_input.transition_event
            if event_name is None:
                event_name = candidate.event if isinstance(candidate, TransitionCandidate) else candidate
            current = next(
                (
                    item
                    for item in self._chart.transitions
                    if item.source == self._record.active_state and item.event == event_name
                ),
                None,
            )
            expected_candidate = (
                TransitionCandidate(
                    current.event,
                    current.source,
                    current.target,
                    current.requires_lifecycle_fact,
                    current.requires_decision,
                )
                if current is not None
                else None
            )
            if current is None or (
                isinstance(candidate, TransitionCandidate) and candidate != expected_candidate
            ):
                return await self._publish_status("unchanged")
            event_identity = (
                event_input.event_id
                if event_input is not None
                else getattr(decision_input, "event_id", None)
                or f"transition:{current.event}"
            )
            if event_identity in self._record.applied_event_identities:
                return await self._publish_status("unchanged")
            if self._record.active_state in self._chart.deadlines and not self._timer_due_authoritative():
                return await self._publish_status("unchanged")
            if current.requires_lifecycle_fact and not (
                self._has_lifecycle_fact(current.maneuver_id, feedback_input)
                or self._has_lifecycle_fact(current.maneuver_id, self._record.lifecycle_facts)
            ):
                return await self._publish_status("unchanged")
            if current.requires_decision and not self._decision_authorizes(
                current.event, decision_input, self._record.mission_id
            ):
                return await self._publish_status("unchanged")
            facts = dict(self._record.lifecycle_facts)
            if isinstance(feedback_input, ManeuverFeedback):
                if (
                    feedback_input.mission_id != self._record.mission_id
                    or feedback_input.maneuver_id != current.maneuver_id
                ):
                    return await self._publish_status("unchanged")
                facts[feedback_input.maneuver_id] = feedback_input.lifecycle
            elif isinstance(feedback_input, Mapping):
                facts.update(feedback_input)
            self._record = replace(
                self._record,
                record_revision=self._record.record_revision + 1,
                active_state=current.target,
                active_configuration=(current.target,),
                last_applied_event=current.event,
                last_applied_event_identity=event_identity,
                applied_event_identities=self._record.applied_event_identities + (event_identity,),
                transition_history=self._record.transition_history + (current.event,),
                lifecycle_facts=facts,
            )
            self.store.save_execution_record(self._record)
            return await self._publish_status("transitioned")

    async def status(self) -> FSMStatus | None:
        """Return and publish current status, including newly due timers."""

        async with self._lock:
            if self._chart is None or self._record is None:
                return None
            return await self._publish_status("updated")

    async def tick(self, now: int | float) -> FSMStatus | None:
        """Publish a timer-due marker once without auto-transitioning."""

        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("FSM tick time must be a number")
        async with self._lock:
            if self._chart is None or self._record is None:
                return None
            self._clock_override = now
            marker = self._timer_marker()
            already_published = marker is not None and marker in self._record.timer_due_markers
            return await self._publish_status("timer_due", publish=not already_published)

    async def _publish_status(self, reason: str, *, publish: bool = True) -> FSMStatus:
        assert self._chart is not None and self._record is not None
        deadline = self._chart.deadlines.get(self._record.active_state)
        timer_marker = self._timer_marker_for_state()
        timer_due = self._timer_due() or (
            timer_marker is not None and timer_marker in self._record.timer_due_markers
        )
        marker = timer_marker if timer_due else None
        if marker is not None and marker not in self._record.timer_due_markers:
            self._record = replace(
                self._record,
                record_revision=self._record.record_revision + 1,
                timer_due_markers=self._record.timer_due_markers + (marker,),
            )
            self.store.save_execution_record(self._record)
        candidates = tuple(
            TransitionCandidate(
                event=item.event,
                source=item.source,
                target=item.target,
                requires_lifecycle_fact=item.requires_lifecycle_fact,
                requires_decision=item.requires_decision,
            )
            for item in self._chart.transitions
            if item.source == self._record.active_state and (deadline is None or timer_due)
        )
        status = FSMStatus(
            mission_id=self._record.mission_id,
            plan_revision=self._record.plan_revision,
            statechart_revision=self._record.statechart_revision,
            active_state=self._record.active_state,
            transition_candidates=candidates,
            timer_due=timer_due,
            status="timer_due" if timer_due else reason,
            superseded_plan_revision=self._superseded_plan_revision,
            superseded_maneuver_ids=self._superseded_maneuver_ids,
            last_applied_event=self._record.last_applied_event,
            timer_due_markers=self._record.timer_due_markers,
            lifecycle_facts=self._record.lifecycle_facts,
            retained_maneuver_ids=self._record.retained_maneuver_ids,
        )
        if not publish:
            return status
        sequence = self.transport.next_event_sequence(self.status_topic, status.mission_id)
        self.transport.publish_event(
            self.status_topic,
            TransportEvent(
                schema_version=1,
                event_id=f"fsm-status:{status.mission_id}:{sequence}",
                mission_id=status.mission_id,
                sequence=sequence,
                event_kind="fsm-status",
                payload=status.to_dict(),
            ),
        )
        if self.operational_log is not None:
            self.operational_log.emit(
                status.mission_id,
                "fsm-runner",
                "fsm",
                status.status,
                details={
                    "operation": "publish_status",
                    "plan_revision": status.plan_revision,
                    "state": status.active_state,
                    "status": status.status,
                    "transport_sequence": sequence,
                },
            )
        return status

    def _timer_due(self) -> bool:
        assert self._chart is not None and self._record is not None
        deadline = self._chart.deadlines.get(self._record.active_state)
        if deadline is None:
            return False
        now = self._clock_override if self._clock_override is not None else self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("FSM Runner clock must return a number")
        return now >= deadline

    def _timer_marker(self) -> str | None:
        assert self._chart is not None and self._record is not None
        marker = self._timer_marker_for_state()
        if marker is None or not self._timer_due():
            return None
        return marker

    def _timer_marker_for_state(self) -> str | None:
        assert self._chart is not None and self._record is not None
        deadline = self._chart.deadlines.get(self._record.active_state)
        return f"{self._record.active_state}@{deadline}" if deadline is not None else None

    def _timer_due_authoritative(self) -> bool:
        assert self._record is not None
        marker = self._timer_marker_for_state()
        return self._timer_due() or (
            marker is not None and marker in self._record.timer_due_markers
        )

    @staticmethod
    def _has_lifecycle_fact(
        maneuver_id: str | None,
        facts: object,
    ) -> bool:
        if maneuver_id is None or facts is None:
            return False
        values: list[object] = []
        if isinstance(facts, Mapping):
            if maneuver_id in facts:
                values.append(facts[maneuver_id])
            if facts.get("maneuver_id") == maneuver_id:
                values.append(facts)
        elif isinstance(facts, (list, tuple)):
            values.extend(facts)
        elif getattr(facts, "maneuver_id", None) == maneuver_id:
            values.append(facts)
        for value in values:
            if isinstance(value, Mapping):
                value = value.get("status", value.get("lifecycle"))
            elif hasattr(value, "status"):
                value = getattr(value, "status")
            if value == "completed":
                return True
        return False

    @staticmethod
    def _decision_authorizes(
        event: str,
        decision: object,
        mission_id: str,
    ) -> bool:
        if isinstance(decision, str):
            return decision == event
        if isinstance(decision, Mapping):
            decision_mission = decision.get("mission_id")
            if decision_mission is not None and decision_mission != mission_id:
                return False
            return decision.get(
                "event", decision.get("transition_event", decision.get("transition"))
            ) == event
        decision_mission = getattr(decision, "mission_id", None)
        if decision_mission is not None and decision_mission != mission_id:
            return False
        return getattr(decision, "event", getattr(decision, "transition", None)) == event
