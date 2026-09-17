from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from types import SimpleNamespace
from threading import Lock

import pytest

import onr.application.context_coordination as coordination_module
from onr.application.context_coordination import (
    ActivePlanRevision,
    ContextCoordination,
    InferenceWindow,
)
from onr.application.mission2_planning import (
    Mission2GateDecision,
    Mission2ReplanGate,
    Mission2RiskRevision,
)
from onr.contracts.fsm import FSMStatus, Statechart, StatechartTransition
from onr.contracts.hyper_agent import HyperHeartbeatDecision
from onr.contracts.planning import PlannerChoice, PlannerPlan, PlanningOutcome


MISSION_ID = "mission-2"


def _environment_payload(*, mode: str = "mission2") -> dict[str, object]:
    return {
        "mission_time_seconds": 0.0,
        "controlled_vehicle": {
            "position": {"x": 0.0, "y": 0.0, "z": -25.0},
            "max_velocity": 20.0,
            "fov_radius": 100.0,
        },
        "world_model_info": {
            "mission_mode": mode,
            "mission_end_time_s": 100.0,
            "visible_ship_ids": [],
            "perception_predictions": {
                "schema_version": 1,
                "run_id": "risk-run",
                "sequence": 1,
                "source": "simulated",
                "status": "ready",
                "valid_until_s": 100.0,
                "active_pairs": [
                    {
                        "ship_ids": [1, 2],
                        "probability": 0.5,
                        "predicted_contact_at_s": 20.0,
                    }
                ],
                "alerts": [],
                "trajectories": {
                    str(ship): {
                        "ready": True,
                        "sampled_at_s": 0.0,
                        "points": [
                            {
                                "time_s": float(second),
                                "position": {
                                    "x": 80.0 + ship + second,
                                    "y": 0.0,
                                    "z": None,
                                },
                            }
                            for second in range(31)
                        ],
                    }
                    for ship in (1, 2)
                },
            },
        },
    }


def _status(revision: int = 1, state: str = "collision-monitoring-active") -> FSMStatus:
    return FSMStatus(
        mission_id=MISSION_ID,
        plan_revision=revision,
        statechart_revision=revision,
        active_state=state,
    )


def _active_revision(revision: int) -> ActivePlanRevision:
    snapshot_id = f"{MISSION_ID}:snapshot:{revision}"
    plan = PlannerPlan(
        MISSION_ID,
        "operator",
        revision,
        snapshot_id,
        PlannerChoice("temporal", "minizinc"),
        PlanningOutcome.SOLVED,
        f"mission2-{revision}.plan",
    )
    chart = Statechart(
        mission_id=MISSION_ID,
        plan_revision=revision,
        mission_snapshot_id=snapshot_id,
        planning_profile="temporal",
        entry_state="collision-monitoring-active",
        states=("collision-monitoring-active", "scenario-recording-end"),
        transitions=(
            StatechartTransition(
                "mission-end",
                "collision-monitoring-active",
                "scenario-recording-end",
                {},
            ),
        ),
        terminal_states=("scenario-recording-end",),
        state_context={
            "collision-monitoring-active": {},
            "scenario-recording-end": {},
        },
    )
    return ActivePlanRevision(
        plan,
        f"planner-plan-{revision}.json",
        chart,
        f"statechart-{revision}.json",
    )


def _decision(
    *,
    trigger: bool = True,
    revision: int = 1,
    sequence: int = 1,
    reason: str = "new_serviceable_risk",
    current_candidate_id: str | None = None,
    advisory_candidate_ids: tuple[str, ...] = ("collision-view:1:2:1",),
) -> Mission2GateDecision:
    risk = Mission2RiskRevision(
        revision=revision,
        run_id="risk-run",
        prediction_sequence=sequence,
        forecast_state="ready",
        active_pairs=(),
        feasible_candidate_ids=advisory_candidate_ids,
        fresh_alert_ids=(),
        material_causes=(),
        mission_end_reached=False,
        material_changed=False,
    )
    return Mission2GateDecision(
        trigger=trigger,
        reason=reason,
        risk_revision=risk,
        affected_pairs=((1, 2),),
        advisory_candidate_ids=advisory_candidate_ids,
        current_candidate_id=current_candidate_id,
    )


class _StaticGate:
    def __init__(self, decision: Mission2GateDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[float, int, str]] = []

    def assess(self, environment, status):  # type: ignore[no-untyped-def]
        self.calls.append(
            (
                float(environment["mission_time_seconds"]),
                status.plan_revision,
                status.active_state,
            )
        )
        return self.decision


class _NoopGate:
    last_decision = None

    def assess(self, _environment):  # type: ignore[no-untyped-def]
        return None


class _Environment:
    update_ownership = "coordinator_driven"
    mission_id = MISSION_ID
    cadence_seconds = 1.0
    feedback_topic = "feedback"
    perception_topic = "perception"

    def __init__(
        self,
        payload: dict[str, object],
        *,
        on_advance: Callable[["_Environment"], None] | None = None,
    ) -> None:
        self.payload = payload
        self.on_advance = on_advance
        self.planning_view_count = 0

    @property
    def current_time(self) -> float:
        return float(self.payload["mission_time_seconds"])

    @property
    def latest_environment_event(self):  # type: ignore[no-untyped-def]
        return None

    @property
    def has_current_maneuver(self) -> bool:
        return False

    def planning_view(self):  # type: ignore[no-untyped-def]
        self.planning_view_count += 1
        return SimpleNamespace(
            environment_event=SimpleNamespace(payload=self.payload)
        )

    def drain_updates(self) -> tuple[()]:
        return ()

    def advance(self) -> None:
        self.payload["mission_time_seconds"] = self.current_time + 1.0
        if self.on_advance is not None:
            self.on_advance(self)

    def start(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        return None

    def stop(self) -> None:
        return None

    def join(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None


class _Transport:
    def open_consumer(self, _subscription):  # type: ignore[no-untyped-def]
        return nullcontext(object())

    def next_event_sequence(self, _topic, _mission_id):  # type: ignore[no-untyped-def]
        return 0


class _TransitionIntents:
    def __init__(self) -> None:
        self.invalidations = 0

    def current(self, _status, **_kwargs):  # type: ignore[no-untyped-def]
        return None

    def invalidate_latest(self, _mission_id) -> None:  # type: ignore[no-untyped-def]
        self.invalidations += 1


class _Hyper:
    def __init__(self, dispositions: tuple[str, ...] = ("no_change",)) -> None:
        self.dispositions = list(dispositions)
        self.invocations: list[object] = []
        self.pending: list[str] = []

    def has_pending(self, _mission_id: str) -> bool:
        return bool(self.pending)

    def pending_request_identities(self, _mission_id: str) -> tuple[str, ...]:
        return tuple(self.pending)

    def heartbeat(self, invocation):  # type: ignore[no-untyped-def]
        self.invocations.append(invocation)
        requests = tuple(self.pending)
        self.pending.clear()
        disposition = self.dispositions.pop(0) if self.dispositions else "no_change"
        return HyperHeartbeatDecision(
            MISSION_ID,
            invocation.plan_revision,
            disposition,
            "test decision",
            invocation.trigger_identities,
            requests,
        )


class _CoordinationHarness(ContextCoordination):
    def __init__(
        self,
        environment: _Environment,
        hyper: _Hyper,
        *,
        replan: Callable[..., ActivePlanRevision | None] | None = None,
        maneuver_seconds: float = 100.0,
        belief_service: object | None = None,
        reliability: object | None = None,
        maneuver_hook: Callable[["_CoordinationHarness", tuple[str, ...]], None]
        | None = None,
    ) -> None:
        self._transport = _Transport()
        self.subscription = SimpleNamespace(mission_id=MISSION_ID)
        self._environment_source = environment
        self._fsm_runner = object()
        self._maneuver_control = object()
        self._hyper_supervisor = hyper
        self._belief_service = belief_service
        self._replan_workflow = replan or (lambda *_args: None)
        self._maneuver_seconds = maneuver_seconds
        self._hyper_seconds = 100.0
        self._simulation_limit_seconds = 1.0
        self._pending_perceptions = []
        self._pending_maneuver_triggers = []
        self._pending_trigger_lock = Lock()
        self._transition_intents = _TransitionIntents()
        self.status = _status()
        self.snapshot = object()
        self.reliability = reliability
        self.maneuver_hook = maneuver_hook
        self.maneuver_invocations: list[tuple[float, int, tuple[str, ...]]] = []

    def _require_runtime(self, _active_revision: ActivePlanRevision) -> None:
        return None

    def _status_or_activate(self, _chart: Statechart) -> FSMStatus:
        return self.status

    def _activate(self, chart: Statechart) -> FSMStatus:
        self.status = _status(chart.plan_revision, chart.entry_state)
        return self.status

    def publish_planner_revision(self, _evidence):  # type: ignore[no-untyped-def]
        return None

    def _publish_runtime_source_facts(self, _status: FSMStatus) -> None:
        return None

    def _drain_required(self, _consumer, *, fallback=None):  # type: ignore[no-untyped-def]
        return fallback or self.snapshot

    def _drain_environment_updates(self, _environment, _consumer, snapshot):  # type: ignore[no-untyped-def]
        return snapshot, 0

    def _append_belief_revisions(self, _revisions: list[int]) -> None:
        return None

    def _resolve_environment(self, _snapshot):  # type: ignore[no-untyped-def]
        return self._environment_source.payload

    def _resolve_belief(self, _snapshot):  # type: ignore[no-untyped-def]
        return self.reliability

    def _hyper_invocation(self, _snapshot, revision, triggers):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            mission_id=MISSION_ID,
            plan_revision=revision.planner_plan.plan_revision,
            trigger_identities=triggers,
            environment_data={
                "mission_time_seconds": self._environment_source.current_time
            },
        )

    def _run_maneuver_heartbeat(
        self,
        snapshot,
        active_revision,
        _count,
        triggers,
        _hyper_outcomes,
        _consumer,
        _physical_actions,
        _belief_revisions,
        environment,
        *,
        start_environment,
    ):  # type: ignore[no-untyped-def]
        if start_environment:
            environment.start(simulation_limit_seconds=self._simulation_limit_seconds)
        self.maneuver_invocations.append(
            (
                environment.current_time,
                active_revision.planner_plan.plan_revision,
                triggers,
            )
        )
        if self.maneuver_hook is not None:
            self.maneuver_hook(self, triggers)
        now = environment.current_time
        return snapshot, self.status, 0, InferenceWindow("maneuver", now, now)


def _install_other_noop_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordination_module, "Mission3ReplanGate", _NoopGate)
    monkeypatch.setattr(coordination_module, "Mission4ReplanGate", _NoopGate)


def _run_with_gate(
    monkeypatch: pytest.MonkeyPatch,
    gate: object,
    *,
    environment: _Environment | None = None,
    hyper: _Hyper | None = None,
    replan: Callable[..., ActivePlanRevision | None] | None = None,
    maneuver_seconds: float = 100.0,
    maneuver_hook: Callable[[_CoordinationHarness, tuple[str, ...]], None]
    | None = None,
) -> tuple[object, _CoordinationHarness, _Environment, _Hyper]:
    _install_other_noop_gates(monkeypatch)
    monkeypatch.setattr(coordination_module, "Mission2ReplanGate", lambda: gate)
    selected_environment = environment or _Environment(_environment_payload())
    selected_hyper = hyper or _Hyper()
    coordinator = _CoordinationHarness(
        selected_environment,
        selected_hyper,
        replan=replan,
        maneuver_seconds=maneuver_seconds,
        maneuver_hook=maneuver_hook,
    )
    result = coordinator.run(_active_revision(1))
    return result, coordinator, selected_environment, selected_hyper


def test_repeated_identical_m2_signature_invokes_supervisor_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _StaticGate(_decision())
    result, _coordinator, environment, hyper = _run_with_gate(monkeypatch, gate)

    assert result.hyper_heartbeat_count == 1
    assert len(gate.calls) >= 2
    assert len(hyper.invocations) == 1
    assert environment.planning_view_count == len(gate.calls)


def test_subthreshold_forecast_update_does_not_add_supervisor_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _environment_payload()

    def update(environment: _Environment) -> None:
        prediction = environment.payload["world_model_info"]["perception_predictions"]
        prediction["sequence"] = 2
        prediction["active_pairs"][0]["predicted_contact_at_s"] = 20.49

    environment = _Environment(payload, on_advance=update)
    gate = Mission2ReplanGate()
    _result, _coordinator, _environment, hyper = _run_with_gate(
        monkeypatch, gate, environment=environment
    )

    assert [item.environment_data["mission_time_seconds"] for item in hyper.invocations] == [
        0.0
    ]
    assert gate._revision == 1


def test_material_risk_revision_change_invokes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _environment_payload()

    def update(environment: _Environment) -> None:
        prediction = environment.payload["world_model_info"]["perception_predictions"]
        prediction["sequence"] = 2
        prediction["active_pairs"][0]["predicted_contact_at_s"] = 20.5

    environment = _Environment(payload, on_advance=update)
    gate = Mission2ReplanGate()
    _result, _coordinator, _environment, hyper = _run_with_gate(
        monkeypatch, gate, environment=environment
    )

    assert [item.environment_data["mission_time_seconds"] for item in hyper.invocations] == [
        0.0,
        1.0,
    ]
    assert gate._revision == 2


def test_explicit_maneuver_request_bypasses_m2_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hyper = _Hyper()

    def request_on_advance(_environment: _Environment) -> None:
        hyper.pending.append("maneuver-request-1")

    environment = _Environment(_environment_payload(), on_advance=request_on_advance)
    gate = _StaticGate(_decision())
    _result, _coordinator, _environment, selected_hyper = _run_with_gate(
        monkeypatch, gate, environment=environment, hyper=hyper
    )

    assert len(selected_hyper.invocations) == 2
    assert selected_hyper.invocations[1].trigger_identities == (
        "mission2-gate:new_serviceable_risk:risk-run:1",
        "maneuver-request-1",
    )


def test_accepted_replacement_preserves_m2_gate_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Mission2ReplanGate] = []

    class TrackingGate(Mission2ReplanGate):
        def __init__(self) -> None:
            super().__init__()
            self.observed_revisions: list[int] = []
            instances.append(self)

        def assess(self, environment, status):  # type: ignore[no-untyped-def]
            decision = super().assess(environment, status)
            self.observed_revisions.append(decision.risk_revision.revision)
            return decision

    _install_other_noop_gates(monkeypatch)
    monkeypatch.setattr(coordination_module, "Mission2ReplanGate", TrackingGate)
    hyper = _Hyper(("replan", "no_change"))
    coordinator = _CoordinationHarness(
        _Environment(_environment_payload()),
        hyper,
        replan=lambda *_args: _active_revision(2),
    )
    result = coordinator.run(_active_revision(1))

    assert result.plan_revisions == (1, 2)
    assert len(instances) == 1
    assert instances[0]._run_id == "risk-run"
    assert set(instances[0].observed_revisions) == {1}
    assert coordinator._transition_intents.invalidations == 1


def test_same_risk_is_reevaluated_once_for_new_plan_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _StaticGate(_decision())
    hyper = _Hyper(("replan", "no_change"))
    result, _coordinator, _environment, selected_hyper = _run_with_gate(
        monkeypatch,
        gate,
        hyper=hyper,
        replan=lambda *_args: _active_revision(2),
    )

    assert result.plan_revisions == (1, 2)
    assert [item.plan_revision for item in selected_hyper.invocations] == [1, 2]


def test_failed_replan_does_not_immediately_repeat_same_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _StaticGate(_decision())
    hyper = _Hyper(("replan",))
    result, _coordinator, _environment, selected_hyper = _run_with_gate(
        monkeypatch, gate, hyper=hyper, replan=lambda *_args: None
    )

    assert result.plan_revisions == (1,)
    assert len(gate.calls) >= 2
    assert len(selected_hyper.invocations) == 1


def test_joint_m1_and_m2_triggers_coalesce_into_one_hyper_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reliability:
        input_revision = 7

    class BeliefService:
        belief_kind = "reporting_reliability"

    class Mission1Gate:
        def assess(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return (
                SimpleNamespace(trigger=True, reason="m1-risk"),
                SimpleNamespace(candidates=(SimpleNamespace(candidate_id="m1"),)),
            )

    _install_other_noop_gates(monkeypatch)
    gate = _StaticGate(_decision())
    monkeypatch.setattr(coordination_module, "Mission2ReplanGate", lambda: gate)
    monkeypatch.setattr(coordination_module, "Mission1ReplanGate", Mission1Gate)
    monkeypatch.setattr(coordination_module, "ReportingReliabilitySnapshot", Reliability)
    hyper = _Hyper()
    coordinator = _CoordinationHarness(
        _Environment(_environment_payload(mode="joint")),
        hyper,
        belief_service=BeliefService(),
        reliability=Reliability(),
    )
    coordinator.run(_active_revision(1))

    assert len(hyper.invocations) == 1
    assert hyper.invocations[0].trigger_identities == (
        "mission1-gate:m1-risk:belief-7;"
        "mission2-gate:new_serviceable_risk:risk-run:1",
    )


def test_mission_end_uses_maneuver_fsm_without_final_supervisor_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _environment_payload()
    payload["world_model_info"]["mission_end_time_s"] = 1.0
    environment = _Environment(payload)

    def finish_at_mission_end(
        coordinator: _CoordinationHarness, _triggers: tuple[str, ...]
    ) -> None:
        if environment.current_time >= 1.0:
            coordinator.status = _status(1, "scenario-recording-end")

    gate = Mission2ReplanGate()
    result, coordinator, _environment, hyper = _run_with_gate(
        monkeypatch,
        gate,
        environment=environment,
        maneuver_seconds=1.0,
        maneuver_hook=finish_at_mission_end,
    )

    assert result.terminal
    assert [item[0] for item in coordinator.maneuver_invocations] == [0.0, 1.0]
    assert hyper.invocations == []


def test_replan_reconciliation_stays_at_same_mission_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replan_times: list[float] = []
    environment = _Environment(_environment_payload())

    def replan(*_args):  # type: ignore[no-untyped-def]
        replan_times.append(environment.current_time)
        return _active_revision(2)

    gate = _StaticGate(_decision())
    hyper = _Hyper(("replan", "no_change"))
    _result, coordinator, _environment, _hyper = _run_with_gate(
        monkeypatch, gate, environment=environment, hyper=hyper, replan=replan
    )

    reconciliation = next(
        item
        for item in coordinator.maneuver_invocations
        if item[2] == ("replan-activated:2",)
    )
    assert replan_times == [0.0]
    assert reconciliation[:2] == (0.0, 2)
