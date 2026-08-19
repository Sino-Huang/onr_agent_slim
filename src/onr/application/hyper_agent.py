"""Deterministic Hyper Agent application service.

This module contains the authority and planning orchestration seam.  Model
construction belongs in :mod:`onr.agents.hyper_agent`; this layer only accepts
callables and planner-port-shaped objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, cast

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.fsm import Statechart
from onr.contracts.hyper_agent import (
    FrozenMissionSpec,
    HumanQuestion,
    MissionInput,
    ReplanRequest,
    _issue_human_question,
)
from onr.contracts.planning import (
    MissionSpec,
    NormalizedPlan,
    PlanningOutcome,
    SymbolicMissionSpec,
)
from onr.contracts.transport import (
    TransportEvent,
    create_normalized_plan_transport_event,
)
from onr.ports.operational_log import OperationalLog


MissionSpecType = MissionSpec | SymbolicMissionSpec


@dataclass(frozen=True, slots=True)
class HyperHeartbeatResult:
    """Immutable outcome of one serialized mission heartbeat."""

    mission_id: str
    outcome: PlanningOutcome
    plan: NormalizedPlan | None = None
    statechart: Statechart | None = None
    request: ReplanRequest | None = None
    entry_state: str | None = None
    retained_maneuver_ids: tuple[str, ...] = ()
    superseded_plan_revision: int | None = None
    snapshot_id: str | None = None

    @property
    def normalized_plan(self) -> NormalizedPlan | None:
        return self.plan

    @property
    def active_plan(self) -> NormalizedPlan | None:
        return self.plan

    @property
    def plan_revision(self) -> int | None:
        return self.plan.plan_revision if self.plan is not None else None

    @property
    def statechart_revision(self) -> int | None:
        return self.statechart.plan_revision if self.statechart is not None else None

    @property
    def request_id(self) -> str | None:
        return self.request.request_id if self.request is not None else None

    @property
    def retained_maneuvers(self) -> tuple[str, ...]:
        return self.retained_maneuver_ids


@dataclass
class _MissionState:
    authority: FrozenMissionSpec
    active_plan: NormalizedPlan | None = None
    active_statechart: Statechart | None = None
    active_source_revisions: dict[str, int | None] | None = None
    active_snapshot_id: str | None = None
    pending: list[ReplanRequest] | None = None
    seen_request_ids: set[str] | None = None

    def __post_init__(self) -> None:
        self.pending = []
        self.seen_request_ids = set()


class HyperAgent:
    """Own mission-spec authority, replan intake, and deterministic heartbeats."""

    def __init__(
        self,
        interpreter: object,
        planner: object | None = None,
        *,
        planners: Mapping[object, object] | None = None,
        transport: Any | None = None,
        mission_spec_topic: str = "mission-specifications",
        normalized_plan_topic: str = "normalized-plans",
        replan_topic: str = "replan-requests",
        operational_log: OperationalLog | None = None,
    ) -> None:
        if not callable(interpreter) and not callable(getattr(interpreter, "interpret", None)):
            raise TypeError("mission interpreter must be callable or expose interpret")
        self.interpreter = interpreter
        self.transport = transport
        self.mission_spec_topic = mission_spec_topic
        self.normalized_plan_topic = normalized_plan_topic
        self.replan_topic = replan_topic
        self.operational_log = operational_log
        self._planner = planner
        self._planners = dict(planners) if planners is not None else None
        self._states: dict[str, _MissionState] = {}
        self._locks: dict[str, RLock] = {}
        if planners is None and isinstance(planner, Mapping):
            self._planners = dict(planner)
            self._planner = None

    def freeze_mission(self, mission_input: MissionInput) -> FrozenMissionSpec:
        """Interpret and atomically publish one validated Mission Specification."""

        if not isinstance(mission_input, MissionInput):
            raise TypeError("freeze_mission requires a MissionInput")
        raw = self._interpret(mission_input)
        spec = self._validated_spec(raw)
        if spec.mission_id != mission_input.mission_id:
            raise ValueError("interpreter Mission ID does not match MissionInput")
        if spec.source_authority != mission_input.source_authority:
            raise ValueError("interpreter source authority does not match MissionInput")

        previous = self._states.get(mission_input.mission_id)
        if previous is not None:
            if previous.authority.mission_input == mission_input and previous.authority.mission_spec == spec:
                return previous.authority
            raise ValueError("Mission is already frozen with a different authority")
        revision = previous.authority.revision + 1 if previous is not None else 1
        record = FrozenMissionSpec(
            mission_input=mission_input,
            mission_spec=spec,
            revision=revision,
            canonical_document=spec.to_canonical_json(),
        )
        if self.transport is not None:
            sequence = self.transport.next_event_sequence(
                self.mission_spec_topic, mission_input.mission_id
            )
            event = TransportEvent(
                schema_version=1,
                event_id=f"mission-spec:{mission_input.mission_id}:{revision}",
                mission_id=mission_input.mission_id,
                sequence=sequence,
                event_kind="mission-specification",
                payload=record.to_dict(),
            )
            self.transport.publish_event(self.mission_spec_topic, event)

        self._states[mission_input.mission_id] = _MissionState(record)
        self._locks.setdefault(mission_input.mission_id, RLock())
        self._emit(
            mission_input.mission_id,
            "agent",
            "success",
            {"operation": "freeze_mission", "revision": record.revision},
        )
        return record

    intake_mission = freeze_mission

    def authority(self, mission_id: str) -> FrozenMissionSpec | None:
        state = self._states.get(mission_id)
        return state.authority if state is not None else None

    @property
    def authorities(self) -> Mapping[str, FrozenMissionSpec]:
        return {mission_id: state.authority for mission_id, state in self._states.items()}

    def submit_replan(self, request: ReplanRequest) -> ReplanRequest:
        if not isinstance(request, ReplanRequest):
            raise TypeError("submit_replan requires a ReplanRequest")
        return self._submit_replan(request, publish=True)

    def _submit_replan(self, request: ReplanRequest, *, publish: bool) -> ReplanRequest:
        state = self._states.get(request.mission_id)
        if state is None:
            raise ValueError("cannot request replanning for an unknown Mission")
        with self._locks.setdefault(request.mission_id, RLock()):
            assert state.pending is not None and state.seen_request_ids is not None
            if request.request_id in state.seen_request_ids:
                return request
            if publish and self.transport is not None:
                sequence = self.transport.next_event_sequence(
                    self.replan_topic, request.mission_id
                )
                event = TransportEvent(
                    schema_version=1,
                    event_id=f"replan:{request.mission_id}:{request.request_id}",
                    mission_id=request.mission_id,
                    sequence=sequence,
                    event_kind="replan-request",
                    payload=request.to_dict(),
                )
                self.transport.publish_event(self.replan_topic, event)
            state.pending.append(request)
            state.seen_request_ids.add(request.request_id)
        return request

    request_replan = submit_replan
    queue_replan = submit_replan

    def handle_replan_event(self, event: TransportEvent | str) -> ReplanRequest:
        """Ingest one replan event without publishing it again."""

        if isinstance(event, str):
            event = TransportEvent.from_json(event)
        if not isinstance(event, TransportEvent) or event.event_kind != "replan-request":
            raise ValueError("expected a replan-request TransportEvent")
        request = ReplanRequest.from_dict(event.payload)
        if request.mission_id != event.mission_id:
            raise ValueError("replan event mission ID does not match its request")
        return self._submit_replan(request, publish=False)

    def run_once(self, consumer_or_event: object) -> ReplanRequest | None:
        """Process one replan delivery, acknowledging only after validation."""

        if hasattr(consumer_or_event, "receive"):
            delivery = cast(Any, consumer_or_event).receive()
            if delivery is None:
                return None
            try:
                result = self.handle_replan_event(delivery.message)
            except Exception:
                delivery.nack()
                raise
            delivery.ack()
            return result
        return self.handle_replan_event(
            cast(TransportEvent | str, consumer_or_event)
        )

    def heartbeat(
        self,
        snapshot: MissionSnapshot,
        *,
        mission_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> HyperHeartbeatResult:
        """Evaluate one authoritative snapshot and, when needed, publish a plan."""

        mission, current_snapshot_id, revisions = self._snapshot_values(
            snapshot, mission_id=mission_id, snapshot_id=snapshot_id
        )
        state = self._states.get(mission)
        if state is None:
            raise ValueError("heartbeat received an unknown Mission")
        self._emit(
            mission,
            "heartbeat",
            "started",
            {"operation": "hyper_heartbeat", "snapshot_id": current_snapshot_id},
        )
        lock = self._locks.setdefault(mission, RLock())
        with lock:
            pending = self._coalesce(state, mission)
            source_changed = state.active_source_revisions != revisions
            initial = state.active_plan is None
            if not initial and pending is None and not source_changed:
                assert state.active_plan is not None
                result = self._result(
                    state, PlanningOutcome(state.active_plan.outcome), None, current_snapshot_id
                )
                self._emit(mission, "heartbeat", "completed", {"operation": "hyper_heartbeat"})
                return result
            next_revision = (state.active_plan.plan_revision + 1) if state.active_plan else 1
            planner = self._select_planner(state.authority.mission_spec)
            if (
                planner is None
                and isinstance(state.authority.mission_spec, SymbolicMissionSpec)
                and state.authority.mission_spec.planner_choice.planner_id is None
            ):
                result = self._result(
                    state, PlanningOutcome.UNSUPPORTED, pending, current_snapshot_id
                )
                self._emit(mission, "heartbeat", "unsupported", {"operation": "hyper_heartbeat"})
                return result
            attempted: NormalizedPlan | None = None
            outcome = PlanningOutcome.ERROR
            try:
                if planner is None:
                    raise ValueError("no planner is configured for the Mission Specification")
                raw_result = self._plan(planner, state.authority.mission_spec, next_revision, current_snapshot_id)
                attempted = raw_result if isinstance(raw_result, NormalizedPlan) else getattr(raw_result, "normalized_plan", None)
                if not isinstance(attempted, NormalizedPlan):
                    raise ValueError("planner did not return a NormalizedPlan")
                if attempted.mission_spec != state.authority.mission_spec:
                    raise ValueError("planner returned a plan for another Mission Specification")
                if attempted.plan_revision != next_revision:
                    raise ValueError("planner returned an unexpected plan revision")
                if attempted.mission_snapshot_id != current_snapshot_id:
                    raise ValueError("planner returned an unexpected Mission Snapshot ID")
                outcome = PlanningOutcome(attempted.outcome)
            except Exception:
                outcome = PlanningOutcome.ERROR
                self._emit(
                    mission,
                    "solver",
                    "failed",
                    {"operation": "heartbeat", "error_type": "planner_error"},
                )

            if outcome is not PlanningOutcome.SOLVED or attempted is None:
                self._emit(mission, "heartbeat", outcome.value, {"operation": "hyper_heartbeat"})
                self._emit(
                    mission,
                    "planning",
                    outcome.value,
                    {"operation": "heartbeat", "plan_revision": next_revision},
                )
                return self._result(state, outcome, pending, current_snapshot_id)

            chart = Statechart.from_normalized_plan(attempted)
            previous_plan = state.active_plan
            retained = self._maneuver_ids(previous_plan)
            if self.transport is not None:
                sequence = self.transport.next_event_sequence(
                    self.normalized_plan_topic, mission
                )
                event = create_normalized_plan_transport_event(
                    attempted,
                    event_id=f"normalized-plan:{mission}:{next_revision}",
                    sequence=sequence,
                )
                self.transport.publish_event(self.normalized_plan_topic, event)

            state.active_plan = attempted
            state.active_statechart = chart
            state.active_source_revisions = dict(revisions)
            state.active_snapshot_id = current_snapshot_id
            self._clear_committed_requests(state, pending)
            self._emit(
                mission,
                "solver",
                "solved",
                {"operation": "heartbeat", "plan_revision": next_revision},
            )
            self._emit(
                mission,
                "planning",
                outcome.value,
                {"operation": "heartbeat", "plan_revision": next_revision},
            )
            self._emit(mission, "heartbeat", "completed", {"operation": "hyper_heartbeat"})
            return HyperHeartbeatResult(
                mission_id=mission,
                outcome=outcome,
                plan=attempted,
                statechart=chart,
                request=pending,
                entry_state=chart.entry_state,
                retained_maneuver_ids=retained,
                superseded_plan_revision=(
                    previous_plan.plan_revision if previous_plan is not None else None
                ),
                snapshot_id=current_snapshot_id,
            )

    def ask_human(
        self,
        mission_id: str,
        question_id: str,
        text: str,
        context: Mapping[str, object] | None = None,
    ) -> HumanQuestion:
        return _issue_human_question(
            question_id,
            mission_id,
            text,
            {} if context is None else context,
        )

    emit_human_question = ask_human

    def active_plan(self, mission_id: str) -> NormalizedPlan | None:
        state = self._states.get(mission_id)
        return state.active_plan if state is not None else None

    def _interpret(self, mission_input: MissionInput) -> object:
        method = getattr(self.interpreter, "interpret", None)
        if callable(method):
            return method(mission_input)
        return cast(Callable[[MissionInput], object], self.interpreter)(mission_input)

    @staticmethod
    def _validated_spec(value: object) -> MissionSpecType:
        if isinstance(value, (MissionSpec, SymbolicMissionSpec)):
            return value
        raise ValueError("interpreter must return a validated MissionSpec or SymbolicMissionSpec")

    def _select_planner(self, spec: MissionSpecType) -> object | None:
        if self._planners is not None:
            profile = spec.planner_choice.planning_profile
            return self._planners.get(profile, self._planners.get(str(profile)))
        return self._planner

    def _emit(
        self,
        mission_id: str,
        event_kind: str,
        outcome: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if self.operational_log is not None:
            self.operational_log.emit(
                mission_id,
                "hyper-agent",
                event_kind,
                outcome,
                details=details,
            )

    @staticmethod
    def _plan(planner: object, spec: MissionSpecType, revision: int, snapshot_id: str) -> object:
        method = getattr(planner, "plan", None)
        if callable(method):
            return method(spec, revision, snapshot_id)
        if callable(planner):
            return planner(spec, revision, snapshot_id)
        raise TypeError("planner must be callable or expose plan")

    @staticmethod
    def _maneuver_ids(plan: NormalizedPlan | None) -> tuple[str, ...]:
        return tuple(item.maneuver_id for item in plan.maneuvers) if plan is not None else ()

    @staticmethod
    def _snapshot_values(
        snapshot: MissionSnapshot,
        *,
        mission_id: str | None,
        snapshot_id: str | None,
    ) -> tuple[str, str, dict[str, int | None]]:
        if not isinstance(snapshot, MissionSnapshot):
            raise TypeError("heartbeat requires an immutable MissionSnapshot")
        if mission_id is not None and mission_id != snapshot.mission_id:
            raise ValueError("heartbeat Mission ID does not match MissionSnapshot")
        current_snapshot_id = snapshot_id or f"snapshot-{snapshot.version}"
        if not current_snapshot_id.strip():
            raise ValueError("heartbeat requires a non-empty snapshot ID")
        return snapshot.mission_id, current_snapshot_id, dict(snapshot.source_revisions)

    @staticmethod
    def _coalesce(state: _MissionState, mission_id: str) -> ReplanRequest | None:
        assert state.pending is not None
        if not state.pending:
            return None
        requests = tuple(state.pending)
        latest = max(requests, key=lambda item: (item.observed_plan_revision, item.request_id))
        ids = tuple(sorted({item.request_id for item in requests}))
        reasons = tuple(sorted({item.reason for item in requests}))
        merged_revisions: dict[str, int | None] = {}
        for request in requests:
            for source, revision in request.source_revisions.items():
                if revision is None:
                    merged_revisions.setdefault(source, None)
                else:
                    current = merged_revisions.get(source)
                    if current is None or revision > current:
                        merged_revisions[source] = revision
        return ReplanRequest(
            request_id=latest.request_id,
            mission_id=mission_id,
            reason=latest.reason,
            requester=latest.requester,
            observed_plan_revision=latest.observed_plan_revision,
            source_revisions=merged_revisions,
            coalesced_request_ids=ids,
            coalesced_reasons=reasons,
        )

    @staticmethod
    def _clear_committed_requests(
        state: _MissionState, request: ReplanRequest | None
    ) -> None:
        if request is None or state.pending is None:
            return
        identities = set(request.coalesced_request_ids)
        state.pending[:] = [
            item for item in state.pending if item.request_id not in identities
        ]

    @staticmethod
    def _result(
        state: _MissionState,
        outcome: PlanningOutcome,
        request: ReplanRequest | None,
        snapshot_id: str,
    ) -> HyperHeartbeatResult:
        chart = state.active_statechart
        return HyperHeartbeatResult(
            mission_id=state.authority.mission_id,
            outcome=outcome,
            plan=state.active_plan,
            statechart=chart,
            request=request,
            entry_state=chart.entry_state if chart is not None else None,
            retained_maneuver_ids=(),
            superseded_plan_revision=None,
            snapshot_id=snapshot_id,
        )


class HyperHeartbeat(HyperAgent):
    """Named compatibility façade for the Hyper Agent heartbeat service."""


HeartbeatResult = HyperHeartbeatResult


__all__ = ["HyperAgent", "HyperHeartbeat", "HyperHeartbeatResult", "HeartbeatResult"]
