from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from onr.adapters.role_skills import FilesystemRoleSkillCatalog
from onr.adapters.system_prompts import load_system_prompt
from onr.agents import DeepAgentsPlanningIntentInterpreter, create_planning_intent_agent
from onr.contracts import PlanningIntent
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import PlannerChoice
from onr.runtime import RuntimeComposition


pytestmark = pytest.mark.live

_DEFAULT_REPORT_PATH = Path(
    "/home/sukai/Project/onr_agent_slim/data/ships_report_and_trajectory_example/"
    "ships/events_report.json"
)


def _runtime_with_temporary_roots(tmp_path: Path) -> RuntimeComposition:
    repository_root = Path(__file__).resolve().parents[1]
    source = repository_root / "conf" / "onr_agent_params.yaml"
    values = yaml.safe_load(source.read_text(encoding="utf-8"))
    values["transport"]["root"] = str(tmp_path / "transport")
    values["storage"]["root"] = str(tmp_path / "storage")
    # This interpretation-only test never constructs or executes planners, but
    # runtime config validation requires executable entrypoints for both profiles.
    values["planners"]["temporal"]["entrypoint"] = sys.executable
    values["planners"]["symbolic"]["entrypoint"] = sys.executable
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return RuntimeComposition.create(repo_root=repository_root, config_path=config_path)


def _report_path() -> Path:
    override = os.environ.get("ONR_SHIPS_EVENTS_REPORT")
    return Path(override) if override is not None else _DEFAULT_REPORT_PATH


def _entity_sort_key(entity_id: str) -> tuple[int, int, str]:
    try:
        return (0, int(entity_id), entity_id)
    except ValueError:
        return (1, 0, entity_id)


def _report_summary_and_risk_scores(
    report_path: Path,
) -> tuple[dict[str, object], dict[str, int], tuple[str, ...], list[dict[str, object]]]:
    decoded = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(decoded, list) or not decoded:
        raise ValueError("ships events report must be a non-empty JSON array")

    event_type_counts: Counter[str] = Counter()
    speed_change_counts: Counter[str] = Counter()
    lane_change_counts: Counter[str] = Counter()
    entity_ids: set[str] = set()
    times: list[int | float] = []

    for index, event in enumerate(decoded):
        if not isinstance(event, Mapping):
            raise ValueError(f"ships events report entry {index} must be an object")
        event_type = event.get("event type")
        entity_id = event.get("entity_id")
        event_time = event.get("time")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError(f"ships events report entry {index} has no event type")
        if isinstance(entity_id, bool) or not isinstance(entity_id, (str, int)):
            raise ValueError(f"ships events report entry {index} has no usable entity ID")
        if isinstance(event_time, bool) or not isinstance(event_time, (int, float)):
            raise ValueError(f"ships events report entry {index} has no usable time")

        normalized_entity_id = str(entity_id)
        entity_ids.add(normalized_entity_id)
        times.append(event_time)
        event_type_counts[event_type] += 1
        if event_type == "speed change":
            speed_change_counts[normalized_entity_id] += 1
        elif event_type == "lane change":
            lane_change_counts[normalized_entity_id] += 1

    ordered_entity_ids = tuple(sorted(entity_ids, key=_entity_sort_key))
    # Code-owned risk model: score = speed-change count + 2 * lane-change count.
    # A ship is high-risk when its observable score is at least 3.
    risk_scores = {
        entity_id: speed_change_counts[entity_id] + 2 * lane_change_counts[entity_id]
        for entity_id in ordered_entity_ids
    }
    high_risk_ship_ids = tuple(
        entity_id for entity_id in ordered_entity_ids if risk_scores[entity_id] >= 3
    )
    if not high_risk_ship_ids:
        raise ValueError("ships events report contains no high-risk ships under the code-owned formula")

    # Keep the live prompt compact while preserving actual spatial-temporal
    # observations for the highest-risk candidates: at most two source records
    # for each of the four highest code-owned scores.
    sampled_entity_ids = tuple(
        sorted(
            high_risk_ship_ids,
            key=lambda entity_id: (-risk_scores[entity_id], _entity_sort_key(entity_id)),
        )[:4]
    )
    sample_counts: Counter[str] = Counter()
    high_risk_event_sample: list[dict[str, object]] = []
    for event in decoded:
        entity_id = str(event["entity_id"])
        if entity_id not in sampled_entity_ids or sample_counts[entity_id] >= 2:
            continue
        position = event.get("position")
        event_information = event.get("event information")
        if (
            not isinstance(position, list)
            or len(position) != 3
            or any(
                isinstance(component, bool) or not isinstance(component, (int, float))
                for component in position
            )
        ):
            raise ValueError("sampled high-risk event has no usable position")
        if not isinstance(event_information, Mapping):
            raise ValueError("sampled high-risk event has no event information")
        high_risk_event_sample.append(
            {
                "entity_id": entity_id,
                "time": event["time"],
                "position": list(position),
                "event_type": event["event type"],
                "event_information": dict(event_information),
            }
        )
        sample_counts[entity_id] += 1
    if set(sample_counts) != set(sampled_entity_ids):
        raise ValueError("ships events report has no samples for a high-risk ship")

    return (
        {
            "event_count": len(decoded),
            "time_range": [min(times), max(times)],
            "event_type_counts": dict(sorted(event_type_counts.items())),
            "entity_ids": list(ordered_entity_ids),
        },
        risk_scores,
        high_risk_ship_ids,
        high_risk_event_sample,
    )


def _json_scalar_values(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _json_scalar_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _json_scalar_values(item)
    elif value is not None:
        yield str(value)


def _contains_planner_asset_or_verification_evidence(value: object) -> bool:
    prohibited_fragments = (
        "planner_asset",
        "solver_input",
        "solver_output",
        "verification_evidence",
        "verification_result",
        "model.mzn",
        "data.dzn",
        "solver.stdout",
        "solver.stderr",
        ".pddl",
        "sas_plan",
    )
    return any(
        fragment in scalar.casefold()
        for scalar in _json_scalar_values(value)
        for fragment in prohibited_fragments
    )


def test_live_planning_intent_interprets_ships_report_for_temporal_fov_scheduling(
    tmp_path: Path,
) -> None:
    report_path = _report_path()
    if not report_path.is_file():
        pytest.skip(
            "live ships report is absent at "
            f"{report_path}; set ONR_SHIPS_EVENTS_REPORT to an events_report.json file"
        )
    (
        report_summary,
        risk_scores,
        high_risk_ship_ids,
        high_risk_event_sample,
    ) = _report_summary_and_risk_scores(report_path)
    repository_root = Path(__file__).resolve().parents[1]
    runtime = _runtime_with_temporary_roots(tmp_path)
    mission_id = f"live-planning-intent-{uuid4().hex}"
    report_facts = {
        "report_source": str(report_path),
        "report_summary": report_summary,
        "risk_model": {
            "formula": "speed_change_count + 2 * lane_change_count",
            "high_risk_threshold": 3,
            "risk_scores": risk_scores,
            "high_risk_ship_ids": list(high_risk_ship_ids),
        },
        "high_risk_event_sample": high_risk_event_sample,
    }
    mission_input = MissionInput(
        mission_id=mission_id,
        mission_text=(
            "find ships not obeying decisions in the submitted report and schedule drone "
            "movement to maximize FoV information gain weighted by risk.\n\n"
            "Use the submitted report facts below. Schedule drone movement and FoV "
            "observations over the reported event times and locations to maximize "
            "risk-weighted information gain. In PlanningIntent details preserve the report "
            "source, the time-and-location observation objective, and high-risk candidate "
            "ship IDs with their supplied code-owned scores. Do not include planner assets "
            "or verification evidence.\n"
            f"{json.dumps(report_facts, sort_keys=True, separators=(',', ':'))}"
        ),
        source_authority="live-ships-report",
    )

    runtime.verify_llm_reachability()
    model = runtime.create_chat_model(mission_id=mission_id, debug_scope="hyper-agent")
    hyper_prompt = load_system_prompt(repository_root / "conf" / "system_prompt", "hyper-agent")
    deep_agent = create_planning_intent_agent(
        model=model,
        system_prompt=f"You are agent {runtime.config.agent_name}. {hyper_prompt}",
        mission_id=mission_id,
        skill_catalog=FilesystemRoleSkillCatalog(repository_root / "conf" / "skills"),
        backend_root=repository_root,
    )
    intent = DeepAgentsPlanningIntentInterpreter(deep_agent, max_retries=4).interpret(
        mission_input
    )

    assert isinstance(intent, PlanningIntent)
    assert intent.mission_id == mission_input.mission_id
    assert intent.source_authority == mission_input.source_authority
    assert intent.planner_choice == PlannerChoice("temporal", "minizinc")
    assert intent.rationale.strip()
    assert len(intent.rationale.strip()) <= 500

    details = intent.to_dict()["details"]
    assert isinstance(details, Mapping)
    details_text = json.dumps(details, sort_keys=True, ensure_ascii=False).casefold()
    assert str(report_path).casefold() in details_text
    assert "fov" in details_text or "field of view" in details_text
    assert "risk" in details_text
    assert len(set(high_risk_ship_ids) & set(_json_scalar_values(details))) >= min(
        2, len(high_risk_ship_ids)
    )
    assert not _contains_planner_asset_or_verification_evidence(details)

    canonical_json = intent.to_canonical_json()
    assert json.loads(canonical_json) == intent.to_dict()
    assert PlanningIntent.from_json(canonical_json) == intent
