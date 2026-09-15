from __future__ import annotations

import copy
import json

import pytest

from onr.application.mission3_planning import (
    Mission3AdaptivePlanner,
    Mission3Description,
    Mission3ReplanGate,
    main,
)
from onr.contracts.transport import TransportEvent


def _ship(
    ship_id: int,
    *,
    screening: str = "unobserved",
    investigation: str = "unobserved",
    verdict: str | None = None,
    evidence: tuple[str, ...] = (),
    incomplete: str | None = None,
) -> dict[str, object]:
    resolved = verdict is not None
    return {
        "ship_id": ship_id,
        "screening": {
            "status": "resolved"
            if resolved and screening != "unobserved"
            else screening,
            "usable_observation_count": 0 if screening == "unobserved" else 1,
        },
        "investigation": {
            "status": "resolved"
            if resolved and investigation != "unobserved"
            else investigation,
            "usable_observation_count": 0 if investigation == "unobserved" else 1,
        },
        "evidence_ids": list(evidence),
        "usable_view_evidence_ids": list(evidence),
        "suspected_object_evidence_ids": [],
        "perception_status": "unobserved" if not evidence else "usable",
        "verdict": (
            None
            if verdict is None
            else {
                "value": verdict,
                "sufficient": True,
                "supporting_evidence_ids": list(evidence),
                "confidence": None,
            }
        ),
        "resolution": {
            "status": "resolved"
            if resolved
            else ("incomplete" if incomplete else "unresolved"),
            "incomplete_reason": incomplete,
            "marked_at_s": 5.0 if incomplete else None,
        },
    }


def _environment(
    ships: list[dict[str, object]],
    *,
    observations: list[dict[str, object]] | None = None,
    lifecycle: dict[str, object] | None = None,
    now: float = 1.0,
    source: str = "simulated_camera",
) -> dict[str, object]:
    selected = [ship["ship_id"] for ship in ships]
    observations = observations or [
        {
            "ship_id": ship_id,
            "sampled_at_s": now,
            "age_s": 0.0,
            "source": source,
            "estimated_position": {"x": 50.0 + index * 20, "y": 0.0, "z": -2.5},
            "estimated_speed_mps": float(index),
            "estimated_heading_degrees": 0.0,
        }
        for index, ship_id in enumerate(selected)
    ]
    return {
        "schema_version": 2,
        "mission_id": "mission-3",
        "mission_time_seconds": now,
        "state_version": int(now * 2),
        "controlled_vehicle": {
            "position": {"x": 0.0, "y": 0.0, "z": -25.0},
            "max_velocity": 20.0,
            "fov_radius": 100.0,
        },
        "maneuver_lifecycle": lifecycle,
        "world_model_info": {
            "mission_mode": "mission3",
            "mission_end_time_s": 100.0,
            "mission3": {
                "schema_version": 1,
                "selected_ship_ids": selected,
                "selection": {"empty": not selected},
                "ships": ships,
                "evidence": [],
                "target_observations": observations,
                "target_observation_max_age_s": 7.5,
            },
        },
    }


def _lifecycle(
    command_id: str,
    action: str,
    target: int,
    state: str,
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "action": action,
        "parameters": {"entity_id": target},
        "lifecycle": state,
        "phase": "orbit" if action == "investigate" else action,
        "progress": {},
    }


def test_description_filters_are_strict_and_preserve_whole_fleet_default() -> None:
    whole = Mission3Description.from_mapping(
        {
            "schema_version": 1,
            "mission_id": "mission-3",
            "mission_text": "Inspect the declared fleet.",
            "source_authority": "operator",
        }
    )
    assert whole.runtime_selection() == {}
    filtered = Mission3Description.from_mapping(
        {
            "schema_version": 1,
            "mission_id": "mission-3",
            "mission_text": "Inspect selected ships in the patrol area.",
            "source_authority": "operator",
            "target_ids": [3, 1, 3],
            "area": {
                "north_min_m": -10,
                "north_max_m": 10,
                "east_min_m": -20,
                "east_max_m": 20,
            },
            "mission_time_budget_s": 80,
        }
    )
    assert filtered.target_ids == (3, 1)
    assert filtered.runtime_selection() == {
        "target_ids": [3, 1],
        "area": {
            "north_min_m": -10.0,
            "north_max_m": 10.0,
            "east_min_m": -20.0,
            "east_max_m": 20.0,
        },
    }
    with pytest.raises(ValueError):
        Mission3Description.from_mapping(
            {
                "schema_version": 1,
                "mission_id": "mission-3",
                "mission_text": "Inspect.",
                "source_authority": "operator",
                "target_ids": [True],
            }
        )


def test_fixed_roster_and_nearest_public_position_prevent_scope_expansion() -> None:
    planner = Mission3AdaptivePlanner()
    environment = _environment(
        [_ship(2)],
        observations=[
            {
                "ship_id": 1,
                "sampled_at_s": 1.0,
                "age_s": 0.0,
                "source": "simulated_camera",
                "estimated_position": {"x": 1.0, "y": 0.0, "z": -2.5},
            },
            {
                "ship_id": 2,
                "sampled_at_s": 1.0,
                "age_s": 0.0,
                "source": "gps",
                "estimated_position": {"x": 80.0, "y": 0.0, "z": -2.5},
            },
        ],
    )
    decision = planner.decide(environment)
    assert decision is not None
    assert decision.entity_id == 2
    assert decision.action == "navigate"
    assert decision.parameters["x"] == pytest.approx(55.0)
    assert decision.reason.endswith("public_position_approach")


def test_live_and_simulated_sources_drive_the_same_screening_action() -> None:
    ship = _ship(1)
    simulated = Mission3AdaptivePlanner().decide(
        _environment([ship], source="simulated_camera")
    )
    live = Mission3AdaptivePlanner().decide(_environment([ship], source="live_camera"))
    assert simulated is not None and live is not None
    assert simulated.action == live.action == "pursue"
    assert simulated.entity_id == live.entity_id == 1
    assert simulated.parameters == live.parameters


def test_inconclusive_target_is_investigated_before_screening_next_ship() -> None:
    planner = Mission3AdaptivePlanner()
    environment = _environment(
        [
            _ship(1, screening="inconclusive", evidence=("view:1",)),
            _ship(2),
        ]
    )
    decision = planner.decide(environment)
    assert decision is not None
    assert decision.action == "investigate"
    assert decision.entity_id == 1
    assert decision.supporting_evidence_ids == ("view:1",)
    assert "investigate_public_evidence" in decision.reason


@pytest.mark.parametrize("verdict", ["normal", "abnormal"])
def test_both_early_verdicts_replace_active_investigation(
    verdict: str,
) -> None:
    planner = Mission3AdaptivePlanner()
    environment = _environment(
        [
            _ship(
                1,
                screening="inconclusive",
                investigation="resolved",
                verdict=verdict,
                evidence=(f"verdict:{verdict}",),
            ),
            _ship(2),
        ],
        lifecycle=_lifecycle("orbit-1", "investigate", 1, "active"),
    )
    decision = planner.decide(environment)
    assert decision is not None
    assert decision.entity_id == 2
    assert decision.action in {"navigate", "pursue"}
    assert decision.reason.startswith("early_verdict")

    all_resolved = _environment(
        [
            _ship(
                1,
                screening="resolved",
                verdict=verdict,
                evidence=(f"verdict:{verdict}",),
            )
        ],
        lifecycle=_lifecycle("orbit-only", "investigate", 1, "active"),
        now=2.0,
    )
    cancellation = Mission3AdaptivePlanner().decide(all_resolved)
    assert cancellation is not None
    assert cancellation.action == "navigate"
    assert cancellation.reason == "early_verdict_cancel"


def test_completed_inconclusive_orbit_interleaves_screening_then_bounded_revisit() -> (
    None
):
    planner = Mission3AdaptivePlanner(maximum_investigations_per_ship=2)
    initial = _environment(
        [
            _ship(1, screening="inconclusive", evidence=("view:1",)),
            _ship(2),
        ]
    )
    assert planner.decide(initial).entity_id == 1  # type: ignore[union-attr]

    after_orbit = copy.deepcopy(initial)
    after_orbit["mission_time_seconds"] = 2.0
    after_orbit["state_version"] = 4
    after_orbit["maneuver_lifecycle"] = _lifecycle(
        "orbit-1", "investigate", 1, "completed"
    )
    screening = planner.decide(after_orbit)
    assert screening is not None and screening.entity_id == 2

    after_screening = copy.deepcopy(after_orbit)
    after_screening["mission_time_seconds"] = 3.0
    after_screening["state_version"] = 6
    after_screening["maneuver_lifecycle"] = _lifecycle(
        "screen-2", screening.action, 2, "completed"
    )
    after_screening["world_model_info"]["mission3"]["ships"][1] = _ship(
        2, screening="resolved", verdict="normal", evidence=("normal:2",)
    )
    revisit = planner.decide(after_screening)
    assert revisit is not None
    assert revisit.action == "investigate"
    assert revisit.entity_id == 1
    assert revisit.reason.endswith("bounded_revisit")


def test_duplicate_active_snapshot_is_ignored_and_target_motion_replans() -> None:
    planner = Mission3AdaptivePlanner(movement_replan_distance_m=10.0)
    initial = _environment([_ship(1)], source="gps")
    first = planner.decide(initial)
    assert first is not None and first.action == "navigate"
    assert planner.decide(copy.deepcopy(initial)) is None

    moved = copy.deepcopy(initial)
    moved["mission_time_seconds"] = 2.0
    moved["state_version"] = 4
    moved["maneuver_lifecycle"] = _lifecycle("navigate-1", "navigate", 1, "active")
    observation = moved["world_model_info"]["mission3"]["target_observations"][0]
    observation["sampled_at_s"] = 2.0
    observation["estimated_position"]["x"] += 20.0
    decision = planner.decide(moved)
    assert decision is not None
    assert decision.entity_id == 1
    assert decision.reason == "target_moved:public_position_approach"


def test_failure_is_bounded_until_new_public_evidence_and_stale_positions_are_rejected() -> (
    None
):
    planner = Mission3AdaptivePlanner()
    initial = _environment([_ship(1)], source="gps")
    assert planner.decide(initial) is not None
    failed = copy.deepcopy(initial)
    failed["mission_time_seconds"] = 2.0
    failed["state_version"] = 4
    failed["maneuver_lifecycle"] = _lifecycle("failed-1", "navigate", 1, "failed")
    report = planner.decide(failed)
    assert report is not None and report.action == "report"
    assert report.reason == "no_feasible_targets"
    assert report.report["inspection_complete"] is False

    recovered = copy.deepcopy(failed)
    recovered["mission_time_seconds"] = 3.0
    recovered["state_version"] = 6
    recovered["maneuver_lifecycle"] = None
    observation = recovered["world_model_info"]["mission3"]["target_observations"][0]
    observation["sampled_at_s"] = 3.0
    observation["age_s"] = 0.0
    assert planner.decide(recovered).entity_id == 1  # type: ignore[union-attr]

    stale = _environment([_ship(1)], source="gps")
    stale["world_model_info"]["mission3"]["target_observations"][0]["age_s"] = 8.0
    stale_report = Mission3AdaptivePlanner().decide(stale)
    assert stale_report is not None and stale_report.action == "report"
    assert stale_report.reason == "no_feasible_targets"


def test_final_report_keeps_supporting_evidence_and_incomplete_reasons() -> None:
    inspection = _environment(
        [
            _ship(
                1, screening="resolved", verdict="abnormal", evidence=("abnormal:1",)
            ),
            _ship(
                2,
                screening="inconclusive",
                evidence=("view:2",),
                incomplete="target_observation_stale",
            ),
        ]
    )["world_model_info"]["mission3"]
    report = Mission3AdaptivePlanner.final_report(inspection, "mission_budget")
    assert report["inspection_complete"] is False
    assert report["resolved_ship_count"] == 1
    assert report["ships"] == [
        {
            "ship_id": 1,
            "verdict": "abnormal",
            "supporting_evidence_ids": ["abnormal:1"],
            "resolution": "resolved",
            "incomplete_reason": None,
        },
        {
            "ship_id": 2,
            "verdict": None,
            "supporting_evidence_ids": [],
            "resolution": "incomplete",
            "incomplete_reason": "target_observation_stale",
        },
    ]


def test_replan_gate_coalesces_snapshots_and_cli_dry_run_writes_nothing(
    tmp_path, capsys
) -> None:
    environment = _environment([_ship(1)])
    gate = Mission3ReplanGate()
    frozen_environment = TransportEvent(
        1, "environment:1", "mission-3", 1, "environment_data", environment
    ).payload
    first = gate.assess(frozen_environment)
    assert first is not None and first.startswith("mission3-gate:")
    assert gate.assess(frozen_environment) is None
    assert Mission3ReplanGate().assess({"world_model_info": {}}) is None

    source = tmp_path / "environment.json"
    source.write_text(json.dumps(environment), encoding="utf-8")
    output = tmp_path / "decision.json"
    assert main([str(source), "--output", str(output), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == "public_world_model"
    assert result["decision"]["entity_id"] == 1
    assert not output.exists()
