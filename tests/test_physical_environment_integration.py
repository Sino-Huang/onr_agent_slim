from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from onr_physical_runtime.agent import TransportBackedEnvironmentUpdateSource

from onr.adapters.file_transport import FileTransport
from onr.contracts.bayesian_belief import BeliefKey, EntityAssociation
from onr.contracts.environment import (
    EventObservation,
    environment_controlled_vehicle,
    environment_maneuver_lifecycle,
    environment_mission_time,
    environment_world_model_info,
    perception_from_dict,
)
from onr.runtime.composition import RuntimeComposition
from onr.runtime.config import load_environment_profile, load_runtime_config


def test_shipped_physical_profile_is_explicit_and_composes_transport_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).parents[1]
    profile = load_environment_profile(
        root / "conf/environment_physical.yaml", repo_root=root
    )
    assert profile.adapter_kind == "external_transport"
    assert profile.protocols.environment_data == 2
    assert profile.fake is None
    assert profile.external is not None
    assert profile.external.runtime_repository == (
        root.parent / "onr_physical_runtime"
    ).resolve()
    assert profile.update_ownership == "coordinator_driven"
    assert profile.external.coordinate_frame == "local_ned"
    assert profile.external.mission_epoch == "environment_reset"
    assert profile.external.altitude_convention == "ned_down_metres"

    config = load_runtime_config(repo_root=root)
    transport_root = tmp_path / "transport"
    config = replace(
        config,
        environment_profile=replace(
            profile,
            external=replace(
                profile.external,
                planning_artifact_root=tmp_path / "environment",
            ),
        ),
        transport=replace(config.transport, root=transport_root),
        storage=replace(config.storage, root=tmp_path / "storage"),
    )
    runtime = RuntimeComposition(config, FileTransport(transport_root))
    runtime_source = str(profile.external.runtime_repository / "src")
    monkeypatch.setattr(
        sys, "path", [item for item in sys.path if item != runtime_source]
    )
    source = runtime.create_environment_update_source(mission_id="mission-1")
    assert sys.path[0] == runtime_source
    assert isinstance(source, TransportBackedEnvironmentUpdateSource)
    assert source.update_topic == "environment-updates"
    assert source.planning_topic == "environment-planning"
    assert source.control_topic == "environment-control"
    assert source.update_ownership == "coordinator_driven"
    with pytest.raises(RuntimeError, match="no planning data"):
        source.planning_view()
    source.stop()
    source.join()


def test_environment_accessors_preserve_physical_raw_info_and_fake_shape() -> None:
    raw_info = {
        "visible_ship_ids": [7],
        "ship_event_reports": {"7": []},
    }
    controlled = {"entity_id": "drone-1", "position": {"x": 1, "y": 2, "z": -3}}
    lifecycle = {"action": "pursue", "parameters": {"entity_id": 7}}
    physical = {
        "schema_version": 2,
        "mission_time_seconds": 4.5,
        "controlled_vehicle": controlled,
        "maneuver_lifecycle": lifecycle,
        "world_model_info": raw_info,
    }

    assert environment_mission_time(physical) == 4.5
    assert environment_controlled_vehicle(physical) is controlled
    assert environment_maneuver_lifecycle(physical) is lifecycle
    assert environment_world_model_info(physical) is raw_info

    scene = {
        "mission_time_seconds": 2,
        "drone": controlled,
        "current_maneuver": lifecycle,
    }
    fake = {"scene_graph": scene}
    assert environment_mission_time(fake) == 2.0
    assert environment_controlled_vehicle(fake) is controlled
    assert environment_maneuver_lifecycle(fake) is lifecycle
    assert environment_world_model_info(fake) is scene


def test_numeric_physical_entity_ids_round_trip_through_perception_and_belief() -> None:
    perception = EventObservation(
        observation_id="event-observed:7:1",
        entity_id=7,
        position=(1, 2, -3),
        observed_time=4,
        uncertainty_score=0.0,
        source_event_index=1,
        event_type="speed change",
        event_information={"change type": "deceleration"},
        event_time=3.5,
    )

    assert perception_from_dict(perception.to_dict()).entity_id == 7
    assert BeliefKey.from_dict(BeliefKey(7, "event-risk").to_dict()).entity_id == 7
    assert EntityAssociation.from_dict(
        EntityAssociation(7, 1.0).to_dict()
    ).entity_id == 7


@pytest.mark.parametrize("ownership", ["coordinator_driven", "environment_driven"])
def test_external_profile_accepts_both_update_ownership_modes(
    tmp_path: Path, ownership: str
) -> None:
    root = Path(__file__).parents[1]
    values = yaml.safe_load(
        (root / "conf/environment_physical.yaml").read_text(encoding="utf-8")
    )
    profile_path = tmp_path / f"environment-{ownership}.yaml"
    values["updates"]["ownership"] = ownership
    profile_path.write_text(yaml.safe_dump(values), encoding="utf-8")

    profile = load_environment_profile(profile_path, repo_root=root)

    assert profile.update_ownership == ownership


def test_external_profile_rejects_implicit_frames(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    values = yaml.safe_load(
        (root / "conf/environment_physical.yaml").read_text(encoding="utf-8")
    )
    profile = tmp_path / "environment.yaml"
    values["external"]["coordinate_frame"] = "unspecified"
    profile.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ValueError, match="coordinate_frame"):
        load_environment_profile(profile, repo_root=root)

    values = yaml.safe_load(
        (root / "conf/environment_physical.yaml").read_text(encoding="utf-8")
    )
    values["external"]["runtime_repository"] = "../onr_physical_runtime"
    profile.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an absolute path"):
        load_environment_profile(profile, repo_root=root)
