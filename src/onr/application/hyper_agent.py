"""Deterministic Hyper Agent application service.

This module contains the authority and planning orchestration seam.  Model
construction belongs in :mod:`onr.agents.hyper_agent`; this layer only accepts
callables and planner-port-shaped objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Any, Callable, cast

from onr.application.bayesian_belief import belief_artifact_reference
from onr.contracts.bayesian_belief import BayesianBeliefSnapshot
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import (
    HumanQuestion,
    MissionInput,
    _issue_human_question,
)
from onr.contracts.planner_translation import validate_environment_data
from onr.contracts.planning_evidence import (
    PlannerChoiceRecord,
    PlannerGenerationAttempt,
)
from onr.contracts.planning_intent import PlanningIntent
from onr.contracts.transport import (
    TransportEvent,
)
from onr.ports.operational_log import OperationalLog


class PlanningHeartbeatOutcome(StrEnum):
    """Whether environment data allowed planner generation to start."""

    ATTEMPTED = "attempted"
    INSUFFICIENT_ENVIRONMENT_DATA = "insufficient_environment_data"


@dataclass(frozen=True, slots=True)
class HyperPlanningHeartbeatResult:
    """Planner selection and generation evidence from one environment heartbeat."""

    outcome: PlanningHeartbeatOutcome
    mission_snapshot_id: str
    planner_choice: PlannerChoiceRecord | None = None
    attempt: PlannerGenerationAttempt | None = None
    belief_snapshot: BayesianBeliefSnapshot | None = None


class HyperAgent:
    """Select planners and publish environment-backed generation evidence."""

    def __init__(
        self,
        interpreter: object,
        *,
        transport: Any | None = None,
        planning_evidence_topic: str = "planning-evidence",
        operational_log: OperationalLog | None = None,
    ) -> None:
        if not callable(interpreter) and not callable(
            getattr(interpreter, "interpret", None)
        ):
            raise TypeError(
                "PlanningIntent interpreter must be callable or expose interpret"
            )
        self.planning_intent_interpreter = interpreter
        self.transport = transport
        self.planning_evidence_topic = planning_evidence_topic
        self.operational_log = operational_log
        self._planning_inputs: dict[str, MissionInput] = {}
        self._planner_choices: dict[str, PlannerChoiceRecord] = {}
        self._generation_attempts: dict[tuple[str, str], PlannerGenerationAttempt] = {}
        self._locks: dict[str, RLock] = {}

    def choose_planner(self, mission_input: MissionInput) -> PlannerChoiceRecord:
        """Record a Planner Choice without creating an intermediate authority."""

        if not isinstance(mission_input, MissionInput):
            raise TypeError("choose_planner requires a MissionInput")
        mission_id = mission_input.mission_id
        lock = self._locks.setdefault(mission_id, RLock())
        with lock:
            previous_input = self._planning_inputs.get(mission_id)
            previous_choice = self._planner_choices.get(mission_id)
            if previous_input is not None:
                if previous_input == mission_input and previous_choice is not None:
                    return previous_choice
                raise ValueError(
                    "Mission already has a Planner Choice for another input"
                )

            raw = self._interpret_planning_intent(mission_input)
            if not isinstance(raw, PlanningIntent):
                raise ValueError(
                    "planner selection interpreter must return a PlanningIntent"
                )
            if raw.mission_id != mission_id:
                raise ValueError(
                    "PlanningIntent Mission ID does not match MissionInput"
                )
            if raw.source_authority != mission_input.source_authority:
                raise ValueError(
                    "PlanningIntent source authority does not match MissionInput"
                )
            choice = PlannerChoiceRecord.from_planning_intent(raw)
            if self.transport is not None:
                sequence = self.transport.next_event_sequence(
                    self.planning_evidence_topic, mission_id
                )
                self.transport.publish_event(
                    self.planning_evidence_topic,
                    TransportEvent(
                        schema_version=1,
                        event_id=choice.decision_id,
                        mission_id=mission_id,
                        sequence=sequence,
                        event_kind="planner-choice",
                        payload=choice.to_dict(),
                    ),
                )
            self._planning_inputs[mission_id] = mission_input
            self._planner_choices[mission_id] = choice
            self._emit(
                mission_id,
                "planner-choice",
                "selected",
                {
                    "decision_id": choice.decision_id,
                    "planning_profile": str(choice.planner_choice.planning_profile),
                    "planner_id": choice.planner_choice.planner_id,
                    "rationale": choice.rationale,
                },
            )
            return choice

    def planner_choice(self, mission_id: str) -> PlannerChoiceRecord | None:
        """Return the opt-in Planner Choice recorded for one Mission."""

        return self._planner_choices.get(mission_id)

    def planning_heartbeat(
        self,
        mission_input: MissionInput,
        snapshot: MissionSnapshot,
        environment_event: TransportEvent | None,
        generate: Callable[
            [PlannerChoiceRecord, MissionSnapshot, TransportEvent],
            PlannerGenerationAttempt,
        ],
        belief_snapshot: BayesianBeliefSnapshot | None = None,
    ) -> HyperPlanningHeartbeatResult:
        """Select and generate from snapshot-authorized environment data."""

        if not isinstance(snapshot, MissionSnapshot):
            raise TypeError("planning heartbeat requires a MissionSnapshot")
        if snapshot.mission_id != mission_input.mission_id:
            raise ValueError("planning heartbeat Mission IDs do not match")
        validated_belief = self.validate_belief_provenance(snapshot, belief_snapshot)
        snapshot_id = f"{mission_input.mission_id}:snapshot:{snapshot.version}"
        source = "environment_data"
        if (
            environment_event is None
            or snapshot.source_references[source] is None
            or snapshot.source_revisions[source] is None
            or snapshot.source_health[source] != "healthy"
            or not snapshot.source_freshness[source]
        ):
            self._emit(
                mission_input.mission_id,
                "planning-environment-data",
                str(PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA),
                {"mission_snapshot_id": snapshot_id},
            )
            return HyperPlanningHeartbeatResult(
                outcome=PlanningHeartbeatOutcome.INSUFFICIENT_ENVIRONMENT_DATA,
                mission_snapshot_id=snapshot_id,
            )

        if (
            not isinstance(environment_event, TransportEvent)
            or environment_event.event_kind != "environment_data"
            or environment_event.mission_id != mission_input.mission_id
        ):
            raise ValueError("planning heartbeat requires the Mission environment event")
        validate_environment_data(
            mission_input.mission_id, snapshot, environment_event
        )
        if snapshot.source_references[source] != environment_event.event_id:
            raise ValueError(
                "MissionSnapshot does not reference the supplied environment event"
            )
        if snapshot.source_health[source] != "healthy":
            raise ValueError("MissionSnapshot environment data is not healthy")
        if not callable(generate):
            raise TypeError("planning heartbeat requires a generation callback")

        choice = self.choose_planner(mission_input)
        attempt = generate(choice, snapshot, environment_event)
        if not isinstance(attempt, PlannerGenerationAttempt):
            raise TypeError("generation callback must return PlannerGenerationAttempt")
        if attempt.mission_snapshot_id != snapshot_id:
            raise ValueError("generation attempt references another MissionSnapshot")
        if (
            attempt.mission_id != mission_input.mission_id
            or attempt.decision_id != choice.decision_id
            or attempt.planner_choice != choice.planner_choice
            or attempt.rationale != choice.rationale
        ):
            raise ValueError(
                "generation attempt does not match the current Planner Choice"
            )
        published = self.publish_generation_attempt(attempt, choice)
        return HyperPlanningHeartbeatResult(
            outcome=PlanningHeartbeatOutcome.ATTEMPTED,
            planner_choice=choice,
            attempt=published,
            mission_snapshot_id=snapshot_id,
            belief_snapshot=validated_belief,
        )

    @staticmethod
    def validate_belief_provenance(
        snapshot: MissionSnapshot,
        belief: BayesianBeliefSnapshot | None,
    ) -> BayesianBeliefSnapshot | None:
        source = "bayesian_belief_snapshot"
        revision = snapshot.source_revisions[source]
        reference = snapshot.source_references[source]
        if revision is None and reference is None:
            if belief is not None:
                raise ValueError(
                    "belief artifact is not authorized by the MissionSnapshot"
                )
            return None
        if revision is None or reference is None:
            raise ValueError("MissionSnapshot belief provenance is incomplete")
        if (
            snapshot.source_health[source] != "healthy"
            or not snapshot.source_freshness[source]
        ):
            raise ValueError(
                "MissionSnapshot belief provenance is not healthy and fresh"
            )
        if not isinstance(belief, BayesianBeliefSnapshot):
            raise ValueError(
                "MissionSnapshot belief reference requires a typed artifact"
            )
        if belief.mission_id != snapshot.mission_id:
            raise ValueError("belief artifact mission does not match MissionSnapshot")
        if belief.belief_revision != revision:
            raise ValueError("belief artifact revision does not match MissionSnapshot")
        if reference != belief_artifact_reference(
            belief.mission_id, belief.content_sha256
        ):
            raise ValueError("belief artifact reference does not match MissionSnapshot")
        return belief

    def publish_generation_attempt(
        self,
        attempt: PlannerGenerationAttempt,
        choice: PlannerChoiceRecord,
    ) -> PlannerGenerationAttempt:
        """Validate and publish immutable evidence for one generated asset set."""

        if (
            attempt.mission_id != choice.mission_id
            or attempt.decision_id != choice.decision_id
            or attempt.planner_choice != choice.planner_choice
            or attempt.rationale != choice.rationale
        ):
            raise ValueError(
                "generation attempt does not match the current Planner Choice"
            )
        return self._publish_generation_attempt(attempt, choice)

    def _publish_generation_attempt(
        self,
        attempt: PlannerGenerationAttempt,
        choice: PlannerChoiceRecord,
    ) -> PlannerGenerationAttempt:
        """Publish evidence already validated by the planning heartbeat."""

        if not isinstance(attempt, PlannerGenerationAttempt):
            raise TypeError("generation evidence must be a PlannerGenerationAttempt")
        mission_id = choice.mission_id
        if self._planner_choices.get(mission_id) != choice:
            raise ValueError("planning heartbeat Planner Choice is not recorded")

        key = (mission_id, attempt.attempt_id)
        lock = self._locks.setdefault(mission_id, RLock())
        with lock:
            previous = self._generation_attempts.get(key)
            if previous is not None:
                if previous == attempt:
                    return previous
                raise ValueError(
                    "generation attempt ID already identifies a different generation attempt"
                )
            if self.transport is not None:
                sequence = self.transport.next_event_sequence(
                    self.planning_evidence_topic, mission_id
                )
                self.transport.publish_event(
                    self.planning_evidence_topic,
                    TransportEvent(
                        schema_version=1,
                        event_id=(
                            f"planner-generation-attempt:{mission_id}:"
                            f"{attempt.attempt_id}"
                        ),
                        mission_id=mission_id,
                        sequence=sequence,
                        event_kind="planner-generation-attempt",
                        payload=attempt.to_dict(),
                    ),
                )
            self._generation_attempts[key] = attempt
            self._emit(
                mission_id,
                "planner-generation-attempt",
                str(attempt.outcome),
                {
                    "attempt_id": attempt.attempt_id,
                    "decision_id": attempt.decision_id,
                    "mission_snapshot_id": attempt.mission_snapshot_id,
                    "translator_id": attempt.translator_id,
                    "translator_version": attempt.translator_version,
                    "asset_references": dict(attempt.asset_references),
                },
            )
            return attempt

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

    def _interpret_planning_intent(self, mission_input: MissionInput) -> object:
        interpreter = self.planning_intent_interpreter
        if interpreter is None:
            raise ValueError("Hyper Agent has no PlanningIntent interpreter")
        method = getattr(interpreter, "interpret", None)
        if callable(method):
            return method(mission_input)
        return cast(Callable[[MissionInput], object], interpreter)(mission_input)

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


__all__ = [
    "HyperAgent",
    "HyperPlanningHeartbeatResult",
    "PlanningHeartbeatOutcome",
]
