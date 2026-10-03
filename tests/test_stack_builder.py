"""Environment Stack presets, builder and herdr launcher goldens (issue #75 Phase 1)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from onr.runtime_host.run_root import RunRoot
from onr.runtime_host.stack import (
    StackRequestError,
    build_stack_plan,
    load_stack_catalog,
    plan_mission_run,
    stack_request,
)
from tests.support import launcher_goldens

REPOSITORY = Path(__file__).parents[1]
CONTRACT = REPOSITORY / "docs/design/operator-console/contract/v1.3"
MISSION_ID = "mission-7c1f9a2e-3b4d-4c5e-8f6a-0b1c2d3e4f5a"

requires_checkouts = pytest.mark.skipif(
    not launcher_goldens.PHYSICAL_ROOT.is_dir(),
    reason="needs the sibling onr_physical_runtime checkout",
)


def test_presets_payload_matches_the_contract_example() -> None:
    example = json.loads((CONTRACT / "stack-presets.response.json").read_text())
    payload: dict[str, Any] = load_stack_catalog().payload(REPOSITORY)

    assert payload.keys() == example.keys()
    assert payload["default_preset_id"] == example["default_preset_id"]
    assert [preset["preset_id"] for preset in payload["presets"]] == [
        "mission1-harbor",
        "mission1-airsim",
        "mission2",
        "mission3",
        "mission4",
        "joint",
        "joint24",
        "joint34",
    ]
    ours = {preset["preset_id"]: preset for preset in payload["presets"]}
    for expected in example["presets"]:
        actual = ours[expected["preset_id"]]
        assert actual.keys() == expected.keys()
        assert actual["default_mission_text"]
        assert {k: v for k, v in actual.items() if k != "default_mission_text"} == {
            k: v for k, v in expected.items() if k != "default_mission_text"
        }
    for preset in payload["presets"]:
        assert preset["defaults"]["airsim"] in preset["supports"]["airsim"]
        assert preset["defaults"]["perception"] in preset["supports"]["perception"]


def test_toggles_fill_defaults_and_reject_unsupported_combinations() -> None:
    catalog = load_stack_catalog()
    airsim = catalog.preset("mission1-airsim")
    harbor = catalog.preset("mission1-harbor")

    toggles = catalog.toggles(airsim, {"perception": "ideal"})
    assert (toggles.airsim, toggles.perception, toggles.simulation_limit_seconds) == (
        True,
        "ideal",
        290.0,
    )
    # Both AirSim mechanisms: the follower (perception off) and the scene clock.
    assert catalog.toggles(airsim, {"perception": "off"}).airsim is True
    assert catalog.toggles(harbor, {"airsim": True}).perception == "off"
    with pytest.raises(StackRequestError, match="AirSim cannot be turned off"):
        catalog.toggles(airsim, {"airsim": False, "perception": "off"})
    with pytest.raises(StackRequestError, match="choose Mission 1 · AirSim live"):
        catalog.toggles(harbor, {"airsim": True, "perception": "yolo"})
    with pytest.raises(StackRequestError, match="perception requires airsim"):
        catalog.toggles(harbor, {"perception": "yolo"})
    with pytest.raises(StackRequestError, match="unknown stack fields"):
        catalog.toggles(airsim, {"camera": "front"})
    with pytest.raises(StackRequestError, match="unknown stack preset"):
        catalog.preset("mission9")


@requires_checkouts
@pytest.mark.parametrize("case", sorted(launcher_goldens.CASES))
def test_launcher_dry_run_matches_the_pre_builder_golden(case: str) -> None:
    golden = launcher_goldens.load_goldens()[case]
    actual = launcher_goldens.capture(case)

    # stack.json is the only new run-root entry the builder adds.
    assert set(actual["run_entries"]) - set(golden["run_entries"]) <= {"stack.json"}
    assert set(golden["run_entries"]) <= set(actual["run_entries"])
    for key in ("header", "run_parent", "commands", "audit", "configs"):
        assert actual[key] == golden[key], key


@requires_checkouts
def test_host_plan_scopes_services_and_config_to_the_run_root(tmp_path: Path) -> None:
    from onr.runtime.config import load_runtime_config

    root = RunRoot(tmp_path / "run")
    plan = plan_mission_run(
        preset_id="mission4",
        stack=None,
        mission_id=MISSION_ID,
        run_root=root,
        repo_root=REPOSITORY,
        viewer_port=5123,
    )

    assert [service.name for service in plan.services] == [
        "physical-runtime",
        "mission4-worker",
    ]
    physical = plan.service("physical-runtime")
    worker = plan.service("mission4-worker")
    assert physical is not None and worker is not None
    for argv in (physical.argv, worker.argv):
        assert argv[argv.index("--mission-id") + 1] == MISSION_ID
        assert argv[argv.index("--transport-root") + 1] == str(root.transport)
    assert physical.argv[physical.argv.index("--viewer-port") + 1] == "5123"
    assert [probe.target for probe in physical.readiness] == [
        str(
            root.transport
            / "identity"
            / f"event-environment-update%3A{MISSION_ID}%3Ainitial.json"
        ),
        "http://127.0.0.1:5123/health",
    ]
    assert worker.readiness[0].target == str(root.path / "mission4-worker-ready.json")

    config = load_runtime_config(plan.agent_config, repo_root=REPOSITORY)
    assert config.transport.root == root.transport
    assert config.storage.root == root.agent_storage
    assert config.storage.planner_artifacts == root.planner_artifacts
    assert config.heartbeats.maneuver_seconds == 300
    stack = json.loads(root.stack_plan.read_text())
    assert (stack["viewer_port"], stack["mission_id"]) == (5123, MISSION_ID)


@requires_checkouts
def test_host_plan_rewrites_demo_mission_fixtures_into_the_run_root(
    tmp_path: Path,
) -> None:
    root = RunRoot(tmp_path / "run")
    source = REPOSITORY / "examples/mission3_description.json"
    original = source.read_text()
    plan = plan_mission_run(
        preset_id="mission3",
        stack=None,
        mission_id=MISSION_ID,
        run_root=root,
        repo_root=REPOSITORY,
        viewer_port=5124,
    )

    selection = plan.inputs.mission3_selection
    assert selection is not None and selection.parent == root.path / "fixtures"
    assert json.loads(selection.read_text())["mission_id"] == MISSION_ID
    assert (
        json.loads(plan.closed_loop.mission_file.read_text())["mission_id"]
        == MISSION_ID
    )
    physical = plan.service("physical-runtime")
    assert physical is not None
    assert physical.argv[physical.argv.index("--mission3-selection") + 1] == str(
        selection
    )
    assert source.read_text() == original
    # Fixtures without the demo identity stay shared.
    fixture = plan.inputs.mission3_fixture
    assert fixture is not None and fixture.parent.name == "mission3_smoke"


@requires_checkouts
def test_update_ownership_toggle_reaches_the_environment_profile(
    tmp_path: Path,
) -> None:
    root = RunRoot(tmp_path / "run")
    plan_mission_run(
        preset_id="mission1-harbor",
        stack={"update_ownership": "environment_driven"},
        mission_id=MISSION_ID,
        run_root=root,
        repo_root=REPOSITORY,
        viewer_port=5125,
    )

    profile = yaml.safe_load(root.environment_profile.read_text())
    assert profile["updates"]["ownership"] == "environment_driven"
    assert profile["external"]["planning_artifact_root"] == str(
        root.environment_artifacts
    )
    assert profile["external"]["mission1_planning_input_path"] == str(
        root.path / "mission1-planning-input/environment.json"
    )


@requires_checkouts
def test_perception_plan_starts_engine_then_perception_with_fresh_runtime_state(
    tmp_path: Path,
) -> None:
    root = RunRoot(tmp_path / "run")
    plan = plan_mission_run(
        preset_id="mission1-airsim",
        stack={"perception": "yolo"},
        mission_id=MISSION_ID,
        run_root=root,
        repo_root=REPOSITORY,
        viewer_port=5126,
    )

    assert [service.name for service in plan.services] == [
        "airsim-engine",
        "perception",
        "physical-runtime",
    ]
    engine, perception, physical = plan.services
    assert engine.readiness[0].target == str(root.engine / "ready.json")
    assert engine.timeout_seconds == 600
    assert perception.readiness[0].target == "http://127.0.0.1:8766/api/v1/health"
    assert perception.timeout_seconds == 900
    assert physical.timeout_seconds == 1200
    assert "--perception-url" in physical.argv
    assert engine.stop_grace_seconds > physical.stop_grace_seconds
    assert [step.name for step in plan.post_ready] == [
        "mission1-public-input",
        "mission1-surveillance-views",
    ]
    # The scene clock needs a fresh state root and live_engine a fresh output.
    assert not root.physical_state.exists()
    assert not root.engine.exists()
    assert plan.closed_loop.simulation_limit_seconds == 290


@requires_checkouts
@pytest.mark.parametrize(
    ("preset_id", "vessels", "offset"),
    [
        ("mission1-harbor", "data/harbor_world/vessels", ["253.7", "45.5", "0"]),
        ("mission1-airsim", "offshore_dock_1/non_collision/0/ships", ["0", "0", "0"]),
    ],
)
def test_airsim_without_perception_follows_the_simulated_world_model(
    tmp_path: Path, preset_id: str, vessels: str, offset: list[str]
) -> None:
    root = RunRoot(tmp_path / "run")
    plan = plan_mission_run(
        preset_id=preset_id,
        stack={"airsim": True, "perception": "off"},
        mission_id=MISSION_ID,
        run_root=root,
        repo_root=REPOSITORY,
        viewer_port=5126,
    )

    (fixture,) = [step for step in plan.prepare if step.name == "airsim-fixture"]
    argv = list(fixture.argv)
    assert argv[argv.index("--vessels") + 1].endswith(vessels)
    assert argv[argv.index("--trajectory-ned-offset") + 1 :][:3] == offset
    assert argv[argv.index("--lead-in-s") + 1] == "30"
    assert [service.name for service in plan.services] == [
        "airsim-engine",
        "physical-runtime",
        "airsim-visualizer",
    ]
    engine, physical, follower = plan.services
    fixture_root = root.path / "airsim-fixture"
    assert engine.argv[engine.argv.index("--scenario") + 1] == str(
        fixture_root / "scenarios/follower"
    )
    assert engine.argv[engine.argv.index("--settings") + 1] == str(
        fixture_root / "engine/settings_airsim.json"
    )
    # Simulated information feeds the world model: no perception, no scene clock.
    assert not {"--perception-url", "--experimental-scene-clock-state"} & set(physical.argv)
    # The follower must start, but a later visualization failure never fails the run.
    assert follower.required is False
    assert follower.readiness[0].target == str(root.path / "airsim-follower-ready.json")
    assert follower.argv[follower.argv.index("--mission-id") + 1] == MISSION_ID
    assert plan.closed_loop.wait_for.target == physical.readiness[0].target


@requires_checkouts
def test_config_rewrite_fails_loudly_when_a_launcher_line_moved(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    (agent / "conf").mkdir(parents=True)
    (agent / "examples").mkdir()
    shutil.copyfile(
        REPOSITORY / "examples/mission.json", agent / "examples/mission.json"
    )
    shutil.copyfile(
        REPOSITORY / "conf/environment_physical.yaml",
        agent / "conf/environment_physical.yaml",
    )
    (agent / "conf/onr_agent_params.yaml").write_text(
        (REPOSITORY / "conf/onr_agent_params.yaml")
        .read_text()
        .replace("  root: var/transport\n", "  root: /shared/transport\n")
    )
    catalog = load_stack_catalog()
    preset = catalog.preset("mission1-harbor")
    request = stack_request(
        catalog,
        preset.preset_id,
        preset.defaults,
        mission_id=MISSION_ID,
        repo_root=agent,
        viewer_port=5127,
    )

    with pytest.raises(ValueError, match="root: var/transport"):
        build_stack_plan(request, tmp_path / "run")


@requires_checkouts
@pytest.mark.parametrize("entrypoint", ["host", "cli"])
def test_allocated_viewer_port_skips_configured_service_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, entrypoint: str
) -> None:
    from onr.runtime_host.stack import builder
    from onr.runtime_host.stack.__main__ import main

    ports = iter((41451, 8766, 5150))
    monkeypatch.setattr(builder, "allocate_port", lambda: next(ports))
    root = tmp_path / "run"
    resolved: dict[str, Any]
    if entrypoint == "host":
        plan = plan_mission_run(
            preset_id="mission1-airsim",
            stack={"perception": "ideal"},
            mission_id=MISSION_ID,
            run_root=root,
            repo_root=REPOSITORY,
        )
        resolved = plan.payload()
    else:
        assert main(
            [
                "plan",
                "--preset",
                "mission1-airsim",
                "--perception",
                "ideal",
                "--mission-id",
                MISSION_ID,
                "--run-root",
                str(root),
                "--no-write",
            ]
        ) == 0
        resolved = json.loads(capsys.readouterr().out)
    assert resolved["viewer_port"] == 5150
    assert set(resolved["ports"].values()) == {41451, 8766, 5150}
