from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from onr.application.minizinc_translation import MiniZincProblem, MiniZincTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planner_translation import (
    PlannerCorrectionStage,
    PlannerGenerationContext,
    PlanningTranslationOutcome,
)
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    PlannerChoice,
    PlannerExecutionEvidence,
    PlannerExecutionResult,
    PlannerStaticCheckResult,
    PlanningOutcome,
    TemporalAssignment,
    TemporalManeuver,
)
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.transport import TransportEvent


class RecordingGenerator:
    def __init__(self, problems: list[MiniZincProblem]) -> None:
        self.problems = problems
        self.requests: list[PlannerGenerationContext] = []

    def generate(self, request: PlannerGenerationContext) -> MiniZincProblem:
        self.requests.append(request)
        return self.problems.pop(0)


class FakeMiniZincPlanner:
    def __init__(
        self,
        checks: list[bool],
        executions: list[PlannerExecutionResult],
    ) -> None:
        self.checks = checks
        self.executions = executions
        self.executed_assets: list[Mapping[str, bytes]] = []

    def check(self, assets: Mapping[str, bytes]) -> PlannerStaticCheckResult:
        _ = assets
        accepted = self.checks.pop(0)
        return PlannerStaticCheckResult(
            accepted,
            0 if accepted else 1,
            stderr="MiniZinc syntax error: invalid model" if not accepted else "",
        )

    def execute(self, assets: Mapping[str, bytes]) -> PlannerExecutionResult:
        self.executed_assets.append(assets)
        return self.executions.pop(0)


def _planning_context() -> tuple[
    MissionInput,
    PlannerChoiceRecord,
    MissionSnapshot,
    TransportEvent,
]:
    mission_input = MissionInput(
        "mission-1",
        "Survey the harbor, then return.",
        "mission-control",
    )
    choice = PlannerChoiceRecord(
        decision_id="choice-1",
        mission_id=mission_input.mission_id,
        planner_choice=PlannerChoice("temporal", "minizinc"),
        rationale="This Mission requires temporal optimization.",
    )
    scene = TransportEvent(
        schema_version=1,
        event_id="scene-1",
        mission_id=mission_input.mission_id,
        sequence=0,
        event_kind="environment_data",
        payload={"graph": {"entities": [{"entity_id": "drone-1"}]}},
    )
    snapshot = MissionSnapshot(
        mission_id=mission_input.mission_id,
        version=2,
        created_at="time-2",
        environment_data=scene.event_id,
        source_revisions={"environment_data": 2},
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    return mission_input, choice, snapshot, scene


def _problem(model: bytes = b"solve minimize 0;") -> MiniZincProblem:
    return MiniZincProblem(
        assets={"model.mzn": model, "data.dzn": b"horizon = 3;"},
        maneuvers=(
            TemporalManeuver(
                "survey",
                ManeuverIntent("survey"),
                (),
                2,
            ),
        ),
        horizon=3,
        translator_id="hyper-minizinc",
        translator_version="1.0.0",
    )


def _evidence(tmp_path: Path) -> PlannerExecutionEvidence:
    directory = tmp_path / "planner-run"
    directory.mkdir(exist_ok=True)
    stdout = directory / "solver.stdout"
    stderr = directory / "solver.stderr"
    model = directory / "model.mzn"
    data = directory / "data.dzn"
    model.write_bytes(b"solve minimize 0;")
    data.write_bytes(b"horizon = 3;")
    stdout.write_bytes(b'{"status":"optimal"}')
    stderr.write_bytes(b"")
    return PlannerExecutionEvidence(
        directory,
        (model, data),
        stdout,
        stderr,
    )


def test_static_rejection_gets_exact_feedback_before_verified_plan(
    tmp_path: Path,
) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingGenerator([_problem(b"invalid"), _problem()])
    planner = FakeMiniZincPlanner(
        checks=[False, True],
        executions=[
            PlannerExecutionResult(
                PlanningOutcome.SOLVED,
                (
                    TemporalAssignment(
                        "survey",
                        0,
                        2,
                        (ManeuverParameter("x", 120), ManeuverParameter("y", -45)),
                    ),
                ),
                _evidence(tmp_path),
            )
        ],
    )

    result = MiniZincTranslation(
        planner,
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
        assert set(attempt.asset_references) == {"model.mzn", "data.dzn"}
        assert all(Path(reference).is_file() for reference in attempt.asset_references.values())
    assert result.normalized_plan is not None
    assert result.normalized_plan.outcome is PlanningOutcome.SOLVED
    assert result.normalized_plan.mission_snapshot_id == "mission-1:snapshot:2"
    assert len(generator.requests) == 2
    assert generator.requests[0].correction_feedback is None
    feedback = generator.requests[1].correction_feedback
    assert feedback is not None
    assert feedback.stage is PlannerCorrectionStage.STATIC
    assert feedback.message == "MiniZinc syntax error: invalid model"
    assert set(feedback.diagnostic_references) == {"stdout", "stderr"}
    assert Path(feedback.diagnostic_references["stdout"]).read_text(
        encoding="utf-8"
    ) == ""
    assert Path(feedback.diagnostic_references["stderr"]).read_text(
        encoding="utf-8"
    ) == "MiniZinc syntax error: invalid model"
    assert len(planner.executed_assets) == 1

    assert result.normalized_plan.maneuvers[0].intent.to_dict()["parameters"] == {
        "x": 120,
        "y": -45,
    }


def test_solution_checker_rejection_receives_sanitized_feedback_before_retry(
    tmp_path: Path,
) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingGenerator([_problem(), _problem()])
    planner = FakeMiniZincPlanner(
        checks=[True, True],
        executions=[
            PlannerExecutionResult(
                PlanningOutcome.SOLVED,
                (TemporalAssignment("survey", 0, 1),),
                _evidence(tmp_path),
            ),
            PlannerExecutionResult(
                PlanningOutcome.SOLVED,
                (TemporalAssignment("survey", 0, 2),),
                _evidence(tmp_path),
            ),
        ],
    )

    result = MiniZincTranslation(
        planner,
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
    assert feedback.message == (
        "Planner assignment duration for 'survey' does not match the generated "
        "maneuver."
    )
    assert len(planner.executed_assets) == 2


def test_execution_rejection_receives_exact_planner_diagnostic_before_retry(
    tmp_path: Path,
) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingGenerator([_problem(), _problem()])
    execution_stdout = (
        '{"type":"error","location":{"filename":"/host/run/data.dzn"},'
        '"message":"instance mismatch"}\n'
    )
    planner = FakeMiniZincPlanner(
        checks=[True, True],
        executions=[
            PlannerExecutionResult(
                PlanningOutcome.ERROR,
                evidence=_evidence(tmp_path),
                return_code=7,
                stdout=execution_stdout,
            ),
            PlannerExecutionResult(
                PlanningOutcome.SOLVED,
                (TemporalAssignment("survey", 0, 2),),
                _evidence(tmp_path),
                return_code=0,
            ),
        ],
    )

    result = MiniZincTranslation(
        planner,
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
    feedback = generator.requests[1].correction_feedback
    assert feedback is not None
    assert feedback.stage is PlannerCorrectionStage.EXECUTION
    assert feedback.message == execution_stdout.strip()
    assert feedback.execution_result is not None
    assert feedback.execution_result.return_code == 7


def test_static_correction_stops_at_configured_bound(tmp_path: Path) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingGenerator([_problem(), _problem(), _problem()])
    planner = FakeMiniZincPlanner(
        checks=[False, False, True],
        executions=[],
    )

    result = MiniZincTranslation(
        planner,
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
        PlannerCorrectionStage.STATIC,
        PlannerCorrectionStage.STATIC,
    ]
    assert planner.executed_assets == []


def test_unsolvable_minizinc_result_never_becomes_a_normalized_plan(
    tmp_path: Path,
) -> None:
    mission_input, choice, snapshot, scene = _planning_context()
    generator = RecordingGenerator([_problem()])
    planner = FakeMiniZincPlanner(
        checks=[True],
        executions=[PlannerExecutionResult(PlanningOutcome.UNSOLVABLE)],
    )

    result = MiniZincTranslation(
        planner,
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
    assert result.correction_feedback == ()
