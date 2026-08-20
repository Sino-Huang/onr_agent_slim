from __future__ import annotations

import hashlib
from pathlib import Path

from onr.adapters.fast_downward import FastDownwardExecutor
from onr.adapters.val import VALPlanValidator
from onr.application.pddl_translation import PDDLProblem, PDDLTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planner_translation import (
    PlannerGenerationContext,
    PlanningTranslationOutcome,
    environment_data_sha256,
)
from onr.contracts.planning import (
    ManeuverIntent,
    PlannerChoice,
    SymbolicManeuver,
)
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.transport import TransportEvent


def test_real_generated_pddl_plan_requires_independent_val_acceptance(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    fast_downward = repository_root / "modules" / "downward" / "fast-downward.py"
    validator = (
        repository_root
        / "modules"
        / "VAL"
        / "build"
        / "linux64"
        / "Release"
        / "bin"
        / "Validate"
    )
    example = (
        repository_root
        / "conf"
        / "skills"
        / "hyper"
        / "creating-pddl-problem-files"
        / "examples"
        / "survey-return"
    )
    assert validator.is_file(), f"VAL validator unavailable: {validator}"
    mission_input = MissionInput(
        "mission-real-pddl",
        "Survey the current area, then return.",
        "mission-control",
    )
    choice = PlannerChoiceRecord(
        decision_id="choice-real-pddl",
        mission_id=mission_input.mission_id,
        mission_input_sha256=hashlib.sha256(
            mission_input.to_canonical_json().encode("utf-8")
        ).hexdigest(),
        planning_intent_sha256="e" * 64,
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        rationale="The Mission is symbolic reachability.",
    )
    scene = TransportEvent(
        schema_version=1,
        event_id="scene-real-pddl",
        mission_id=mission_input.mission_id,
        sequence=0,
        event_kind="environment_data",
        payload={"graph": {"entities": [{"entity_id": "drone-1"}]}},
    )
    snapshot = MissionSnapshot(
        mission_id=mission_input.mission_id,
        version=1,
        created_at="time-1",
        environment_data=scene.event_id,
        source_revisions={"environment_data": 1},
        source_hashes={
            "environment_data": environment_data_sha256(scene)
        },
        source_health={"environment_data": "healthy"},
        source_freshness={"environment_data": True},
    )
    maneuvers = (
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
    )
    requests: list[PlannerGenerationContext] = []

    def generate(request: PlannerGenerationContext) -> PDDLProblem:
        requests.append(request)
        assert request.mission_input == mission_input
        assert request.mission_snapshot == snapshot
        assert request.environment_event == scene
        return PDDLProblem(
            assets={
                "domain.pddl": (example / "domain.pddl").read_bytes(),
                "problem.pddl": (example / "problem.pddl").read_bytes(),
            },
            maneuvers=maneuvers,
            domain_revision=1,
            translator_id="hyper-pddl",
            translator_version="1.0.0",
        )

    result = PDDLTranslation(
        FastDownwardExecutor(
            fast_downward,
            tmp_path / "planner-artifacts",
            timeout_seconds=10,
        ),
        VALPlanValidator(validator, timeout_seconds=10),
        tmp_path / "generation-attempts",
    ).plan(
        mission_input,
        choice,
        snapshot,
        scene,
        generate,
        plan_revision=1,
    )

    assert result.outcome is PlanningTranslationOutcome.VERIFIED
    assert result.attempt_count == 1
    assert len(result.generation_attempts) == 1
    attempt = result.generation_attempts[0]
    assert str(attempt.outcome) == "accepted"
    for name, reference in attempt.asset_references.items():
        assert (
            hashlib.sha256(Path(reference).read_bytes()).hexdigest()
            == (attempt.asset_sha256[name])
        )
    assert len(requests) == 1
    assert result.normalized_plan is not None
    assert result.normalized_plan.provenance is not None
    provenance = result.normalized_plan.provenance
    assert provenance.mission_intent.reference == (
        f"mission-input:{mission_input.mission_id}"
    )
    assert provenance.mission_intent.sha256 == choice.mission_input_sha256
    assert provenance.environment_data.sha256 == (
        environment_data_sha256(scene)
    )
    for name, reference in provenance.generated_assets.items():
        assert Path(reference.reference).name == name
        assert (
            hashlib.sha256(Path(reference.reference).read_bytes()).hexdigest()
            == reference.sha256
        )
    assert set(provenance.solver_evidence) == {
        "accepted-plan",
        "planner-result",
        "validator-result",
    }
    for reference in provenance.solver_evidence.values():
        assert (
            hashlib.sha256(Path(reference.reference).read_bytes()).hexdigest()
            == reference.sha256
        )
    assert "mission_spec" not in result.normalized_plan.to_dict()
    assert [step.maneuver_id for step in result.normalized_plan.symbolic_steps] == [
        "survey",
        "return-to-base",
    ]
    assert result.evidence is not None
    assert (result.evidence.artifact_directory / "validator.stdout").is_file()
