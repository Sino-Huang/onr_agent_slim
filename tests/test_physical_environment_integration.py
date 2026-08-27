from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from onr_physical_runtime.agent import TransportBackedEnvironmentUpdateSource

from onr.adapters.file_transport import FileTransport
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
    assert profile.fake is None
    assert profile.external is not None
    assert profile.external.runtime_repository == Path(__file__).parents[3]
    assert profile.update_ownership == "environment_driven"
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
    with pytest.raises(RuntimeError, match="no planning data"):
        source.planning_view()
    source.stop()
    source.join()


def test_external_profile_rejects_coordinator_ownership_and_implicit_frames(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    values = yaml.safe_load(
        (root / "conf/environment_physical.yaml").read_text(encoding="utf-8")
    )
    profile = tmp_path / "environment.yaml"
    values["updates"]["ownership"] = "coordinator_driven"
    profile.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ValueError, match="environment-driven"):
        load_environment_profile(profile, repo_root=root)

    values["updates"]["ownership"] = "environment_driven"
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
