import json
from collections.abc import Mapping
from typing import TypedDict, cast

import pytest

from onr.contracts import PlanningIntent
from onr.contracts.planning import PlannerChoice


class _PlanningIntentArguments(TypedDict):
    mission_id: str
    source_authority: str
    objective: str
    rationale: str
    planner_choice: PlannerChoice
    mission_input_sha256: str
    details: Mapping[str, object]


class _PlanningIntentOverrides(TypedDict, total=False):
    mission_id: str
    source_authority: str
    objective: str
    rationale: str
    planner_choice: PlannerChoice
    mission_input_sha256: str
    details: Mapping[str, object]


def _intent(
    *, details: object | None = None, **overrides: object
) -> PlanningIntent:
    default_details: Mapping[str, object] = {
        "constraints": {
            "regions": ["alpha", {"priority": 1}],
            "maximum_altitude_m": 120,
        },
        "include_imagery": True,
        "operator_note": None,
    }
    values: _PlanningIntentArguments = {
        "mission_id": "mission-1",
        "source_authority": "mission-control",
        "objective": "Survey the operating area",
        "rationale": "The survey supports the requested assessment.",
        "planner_choice": PlannerChoice("temporal", "minizinc"),
        "mission_input_sha256": "a" * 64,
        "details": cast(
            Mapping[str, object], details if details is not None else default_details
        ),
    }
    values.update(cast(_PlanningIntentOverrides, overrides))
    return PlanningIntent(**values)


def test_planning_intent_is_a_canonical_immutable_public_contract() -> None:
    source_priority = {"priority": 1}
    source_details: dict[str, object] = {
        "constraints": {
            "regions": ["alpha", source_priority],
            "maximum_altitude_m": 120,
        },
        "include_imagery": True,
        "operator_note": None,
    }
    intent = _intent(details=source_details)

    assert intent.schema_version == 1
    assert intent.planner_choice == PlannerChoice("temporal", "minizinc")
    assert intent.mission_input_sha256 == "a" * 64
    assert intent.to_dict()["schema_version"] == 1

    source_priority["priority"] = 2
    serialized_details = cast(Mapping[str, object], intent.to_dict()["details"])
    serialized_constraints = cast(Mapping[str, object], serialized_details["constraints"])
    serialized_regions = cast(list[object], serialized_constraints["regions"])
    serialized_priority = cast(Mapping[str, object], serialized_regions[1])
    assert serialized_priority["priority"] == 1

    frozen_constraints = cast(Mapping[str, object], intent.details["constraints"])
    frozen_regions = cast(tuple[object, ...], frozen_constraints["regions"])
    frozen_priority = cast(dict[str, object], frozen_regions[1])
    with pytest.raises(TypeError):
        frozen_priority["priority"] = 3

    reordered = _intent(
        details={
            "operator_note": None,
            "include_imagery": True,
            "constraints": {
                "maximum_altitude_m": 120,
                "regions": ["alpha", {"priority": 1}],
            },
        }
    )
    canonical_json = intent.to_canonical_json()
    assert canonical_json == reordered.to_canonical_json()
    assert PlanningIntent.from_dict(intent.to_dict()) == intent
    assert PlanningIntent.from_json(canonical_json) == intent


@pytest.mark.parametrize(
    "field",
    ("mission_id", "source_authority", "objective", "rationale"),
)
def test_planning_intent_requires_nonblank_identity_and_explanation_fields(
    field: str,
) -> None:
    with pytest.raises(ValueError):
        _intent(**{field: "   "})


@pytest.mark.parametrize(
    "reserved_key",
    (
        "schema_version",
        "mission_id",
        "source_authority",
        "objective",
        "rationale",
        "planner_choice",
        "mission_input_sha256",
        "details",
    ),
)
def test_planning_intent_rejects_reserved_detail_keys(reserved_key: str) -> None:
    with pytest.raises(ValueError):
        _intent(details={reserved_key: "shadowed"})


@pytest.mark.parametrize(
    "prohibited_key",
    (
        "planner_assets",
        "generated_assets",
        "solver_input",
        "solver_output",
        "verification_evidence",
        "normalized_plan",
        "mission_spec",
    ),
)
def test_planning_intent_rejects_nested_planner_owned_provenance(
    prohibited_key: str,
) -> None:
    with pytest.raises(ValueError):
        _intent(details={"requested_context": {"nested": {prohibited_key: {}}}})


def test_planning_intent_requires_an_available_planner() -> None:
    assert _intent(
        planner_choice=PlannerChoice("symbolic", "fast-downward")
    ).planner_choice == PlannerChoice("symbolic", "fast-downward")
    with pytest.raises(ValueError):
        _intent(planner_choice=PlannerChoice.unsupported_symbolic())


@pytest.mark.parametrize("mission_input_sha256", ("a" * 63, "A" * 64))
def test_planning_intent_rejects_untrusted_hashes_versions_and_nonfinite_json(
    mission_input_sha256: str,
) -> None:
    with pytest.raises(ValueError):
        _intent(mission_input_sha256=mission_input_sha256)

    wrong_version = _intent().to_dict()
    wrong_version["schema_version"] = 2
    with pytest.raises(ValueError):
        PlanningIntent.from_dict(wrong_version)

    nonfinite = _intent().to_dict()
    nonfinite["details"] = {"score": float("nan")}
    with pytest.raises(ValueError):
        PlanningIntent.from_json(json.dumps(nonfinite))
