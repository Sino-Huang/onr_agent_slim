from __future__ import annotations

import hashlib
from pathlib import Path

from onr.adapters.minizinc import MiniZincExecutor
from onr.application.minizinc_translation import MiniZincProblem, MiniZincTranslation
from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planner_translation import (
    PlannerGenerationContext,
    PlanningTranslationOutcome,
)
from onr.contracts.planning import (
    ManeuverIntent,
    PlannerChoice,
    ScheduledManeuver,
    TemporalManeuver,
)
from onr.contracts.planning_evidence import PlannerChoiceRecord
from onr.contracts.transport import TransportEvent

_MODEL = b"""
int: duration;
var 0..3: start;
solve minimize start;
output [
  "{\\\"assignments\\\":[{\\\"maneuver_id\\\":\\\"survey\\\","
  ++ "\\\"start\\\":" ++ show(start)
  ++ ",\\\"duration\\\":" ++ show(duration) ++ "}]}"
];
"""


def test_real_generated_minizinc_assets_require_optimal_checked_solution(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    minizinc = (
        repository_root
        / "modules"
        / "MiniZincIDE-2.9.7-bundle-linux-x86_64"
        / "bin"
        / "minizinc"
    )
    mission_input = MissionInput(
        "mission-real-generated",
        "Survey the current operational area.",
        "mission-control",
    )
    choice = PlannerChoiceRecord(
        decision_id="choice-real-generated",
        mission_id=mission_input.mission_id,
        mission_input_sha256=hashlib.sha256(
            mission_input.to_canonical_json().encode("utf-8")
        ).hexdigest(),
        planning_intent_sha256="c" * 64,
        planner_choice=PlannerChoice("temporal", "minizinc"),
        rationale="The Mission requires temporal optimization.",
    )
    scene = TransportEvent(
        schema_version=1,
        event_id="scene-real-generated",
        mission_id=mission_input.mission_id,
        sequence=0,
        event_kind="operational_scene_graph",
        payload={"graph": {"entities": [{"entity_id": "drone-1"}]}},
    )
    snapshot = MissionSnapshot(
        mission_id=mission_input.mission_id,
        version=1,
        created_at="time-1",
        operational_scene_graph=scene.event_id,
        source_revisions={"operational_scene_graph": 1},
        source_health={"operational_scene_graph": "healthy"},
        source_freshness={"operational_scene_graph": True},
    )
    requests: list[PlannerGenerationContext] = []

    def generate(request: PlannerGenerationContext) -> MiniZincProblem:
        requests.append(request)
        assert request.mission_input == mission_input
        assert request.mission_snapshot == snapshot
        assert request.scene_graph == scene
        return MiniZincProblem(
            assets={"model.mzn": _MODEL, "data.dzn": b"duration = 2;"},
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

    result = MiniZincTranslation(
        MiniZincExecutor(minizinc, tmp_path / "artifacts", timeout_seconds=10)
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
    assert len(requests) == 1
    assert result.normalized_plan is not None
    assert result.normalized_plan.maneuvers[0].maneuver_id == "survey"
    assert isinstance(result.normalized_plan.maneuvers[0], ScheduledManeuver)
    assert result.normalized_plan.maneuvers[0].start == 0
    assert result.evidence is not None
