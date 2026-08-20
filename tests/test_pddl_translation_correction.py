from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from onr.application.pddl_translation import PDDLProblem, PDDLTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planner_translation import (
    PlannerCorrectionStage,
    PlannerGenerationContext,
    PlanningTranslationOutcome,
    operational_scene_graph_sha256,
)
from onr.contracts.planning import (
    ManeuverIntent,
    PlannerChoice,
    PlannerExecutionEvidence,
    PlanningOutcome,
    SymbolicActionCall,
    SymbolicManeuver,
    SymbolicPlannerExecutionResult,
)
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.transport import TransportEvent


class RecordingPDDLGenerator:
    def __init__(self, problems: list[PDDLProblem]) -> None:
        self.problems = problems
        self.requests: list[PlannerGenerationContext] = []

    def generate(self, request: PlannerGenerationContext) -> PDDLProblem:
        self.requests.append(request)
        return self.problems.pop(0)


class FakeFastDownwardPlanner:
    def __init__(
        self,
        checks: list[bool],
        executions: list[SymbolicPlannerExecutionResult],
    ) -> None:
        self.checks = checks
        self.executions = executions

    def check(self, assets: Mapping[str, bytes]) -> bool:
        _ = assets
        return self.checks.pop(0)

    def execute(self, assets: Mapping[str, bytes]) -> SymbolicPlannerExecutionResult:
        _ = assets
        return self.executions.pop(0)


class RecordingValidator:
    def __init__(self, results: list[bool]) -> None:
        self.results = results
        self.evidence: list[PlannerExecutionEvidence] = []

    def validate(self, evidence: PlannerExecutionEvidence) -> bool:
        self.evidence.append(evidence)
        result = self.results.pop(0)
        (evidence.artifact_directory / "validator.stdout").write_text(
            "Plan valid\n" if result else "Plan invalid\n",
            encoding="utf-8",
        )
        (evidence.artifact_directory / "validator.stderr").write_text(
            "",
            encoding="utf-8",
        )
        return result


def _planning_context() -> tuple[
    MissionInput,
    PlannerChoiceRecord,
    MissionSnapshot,
    TransportEvent,
]:
    mission_input = MissionInput(
        "mission-symbolic-1",
        "Survey the harbor, then return.",
        "mission-control",
    )
    choice = PlannerChoiceRecord(
        decision_id="choice-symbolic-1",
        mission_id=mission_input.mission_id,
        mission_input_sha256=hashlib.sha256(
            mission_input.to_canonical_json().encode("utf-8")
        ).hexdigest(),
        planning_intent_sha256="d" * 64,
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        rationale="This Mission is symbolic reachability.",
    )
    scene = TransportEvent(
        schema_version=1,
        event_id="scene-symbolic-1",
        mission_id=mission_input.mission_id,
        sequence=0,
        event_kind="operational_scene_graph",
        payload={"graph": {"entities": [{"entity_id": "drone-1"}]}},
    )
    snapshot = MissionSnapshot(
        mission_id=mission_input.mission_id,
        version=3,
        created_at="time-3",
        operational_scene_graph=scene.event_id,
        source_revisions={"operational_scene_graph": 3},
        source_hashes={
            "operational_scene_graph": operational_scene_graph_sha256(scene)
        },
        source_health={"operational_scene_graph": "healthy"},
        source_freshness={"operational_scene_graph": True},
    )
    return mission_input, choice, snapshot, scene


def _problem(domain: bytes = b"(define (domain generated))") -> PDDLProblem:
    return PDDLProblem(
        assets={
            "domain.pddl": domain,
            "problem.pddl": b"(define (problem generated))",
        },
        maneuvers=(
            SymbolicManeuver(
                "return-to-base",
                ManeuverIntent("return-to-base"),
                ("survey",),
                2,
            ),
            SymbolicManeuver(
                "survey",
                ManeuverIntent("survey"),
                (),
                5,
            ),
        ),
        domain_revision=1,
        translator_id="hyper-pddl",
        translator_version="1.0.0",
    )


def _solved_execution(tmp_path: Path) -> SymbolicPlannerExecutionResult:
    directory = tmp_path / "planner-run"
    directory.mkdir(exist_ok=True)
    (directory / "domain.pddl").write_bytes(b"(define (domain generated))")
    (directory / "problem.pddl").write_bytes(b"(define (problem generated))")
    (directory / "solver.stdout").write_bytes(b"survey\nreturn-to-base\n")
    (directory / "solver.stderr").write_bytes(b"")
    (directory / "sas_plan").write_bytes(
        b"(survey)\n(return-to-base)\n; cost = 7 (unit cost)\n"
    )
    return SymbolicPlannerExecutionResult(
        PlanningOutcome.SOLVED,
        (
            SymbolicActionCall("survey"),
            SymbolicActionCall("return-to-base"),
        ),
        7,
        PlannerExecutionEvidence(
            directory,
            (
                directory / "domain.pddl",
                directory / "problem.pddl",
                directory / "sas_plan",
            ),
            directory / "solver.stdout",
            directory / "solver.stderr",
        ),
    )


def test_static_pddl_rejection_gets_sanitized_feedback_before_val_verified_plan(
    tmp_path: Path,
) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingPDDLGenerator([_problem(b"invalid"), _problem()])
    planner = FakeFastDownwardPlanner(
        checks=[False, True],
        executions=[_solved_execution(tmp_path)],
    )
    validator = RecordingValidator([True])

    result = PDDLTranslation(
        planner,
        validator,
        tmp_path / "generation-attempts",
        max_corrections=1,
    ).plan(
        mission_input,
        choice,
        snapshot,
        scene,
        generator,
        plan_revision=1,
    )

    assert result.outcome is PlanningTranslationOutcome.VERIFIED
    assert result.attempt_count == 2
    assert [str(item.outcome) for item in result.generation_attempts] == [
        "rejected",
        "accepted",
    ]
    for attempt in result.generation_attempts:
        assert set(attempt.asset_references) == {"domain.pddl", "problem.pddl"}
        for name, reference in attempt.asset_references.items():
            content = Path(reference).read_bytes()
            assert hashlib.sha256(content).hexdigest() == attempt.asset_sha256[name]
    assert result.normalized_plan is not None
    assert result.normalized_plan.mission_snapshot_id == "mission-symbolic-1:snapshot:3"
    assert len(generator.requests) == 2
    feedback = generator.requests[1].correction_feedback
    assert feedback is not None
    assert feedback.stage is PlannerCorrectionStage.STATIC
    assert "invalid" not in feedback.message
    assert len(validator.evidence) == 1


def test_val_rejection_gets_sanitized_feedback_before_regeneration(
    tmp_path: Path,
) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingPDDLGenerator([_problem(), _problem()])
    execution = _solved_execution(tmp_path)
    planner = FakeFastDownwardPlanner(
        checks=[True, True],
        executions=[execution, execution],
    )
    validator = RecordingValidator([False, True])

    result = PDDLTranslation(
        planner,
        validator,
        tmp_path / "generation-attempts",
        max_corrections=1,
    ).plan(
        mission_input,
        choice,
        snapshot,
        scene,
        generator,
        plan_revision=1,
    )

    assert result.outcome is PlanningTranslationOutcome.VERIFIED
    assert result.attempt_count == 2
    assert result.normalized_plan is not None
    feedback = generator.requests[1].correction_feedback
    assert feedback is not None
    assert feedback.stage is PlannerCorrectionStage.SOLUTION_CHECKER
    assert feedback.message == "Planner output failed independent solution validation."
    assert len(validator.evidence) == 2


def test_val_correction_stops_at_configured_bound(tmp_path: Path) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingPDDLGenerator([_problem(), _problem(), _problem()])
    execution = _solved_execution(tmp_path)
    planner = FakeFastDownwardPlanner(
        checks=[True, True, True],
        executions=[execution, execution],
    )
    validator = RecordingValidator([False, False, True])

    result = PDDLTranslation(
        planner,
        validator,
        tmp_path / "generation-attempts",
        max_corrections=1,
    ).plan(
        mission_input,
        choice,
        snapshot,
        scene,
        generator,
        plan_revision=1,
    )

    assert result.outcome is PlanningTranslationOutcome.REPAIR_EXHAUSTED
    assert result.attempt_count == 2
    assert result.normalized_plan is None
    assert len(generator.requests) == 2
    assert [item.stage for item in result.correction_feedback] == [
        PlannerCorrectionStage.SOLUTION_CHECKER,
        PlannerCorrectionStage.SOLUTION_CHECKER,
    ]
    assert len(validator.evidence) == 2


def test_unsolvable_pddl_result_never_reaches_val_or_normalized_plan(
    tmp_path: Path,
) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingPDDLGenerator([_problem()])
    planner = FakeFastDownwardPlanner(
        checks=[True],
        executions=[
            SymbolicPlannerExecutionResult(
                PlanningOutcome.UNSOLVABLE,
                evidence=_solved_execution(tmp_path).evidence,
            )
        ],
    )
    validator = RecordingValidator([])

    result = PDDLTranslation(
        planner,
        validator,
        tmp_path / "generation-attempts",
    ).plan(
        mission_input,
        choice,
        snapshot,
        scene,
        generator,
        plan_revision=1,
    )

    assert result.outcome is PlanningTranslationOutcome.UNSOLVABLE
    assert result.attempt_count == 1
    assert result.normalized_plan is None
    assert validator.evidence == []
