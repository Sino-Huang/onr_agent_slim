"""Preflight Checks with injected probes (issue #75 §3.1)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from onr.runtime_host.stack import PreflightProbes, load_stack_catalog, run_preflight
from tests.support import launcher_goldens

REPOSITORY = Path(__file__).parents[1]
CONTRACT = REPOSITORY / "docs/design/operator-console/contract/v1.2"
MODEL = "Qwen/Qwen3.8-27B-FP8"

pytestmark = pytest.mark.skipif(
    not launcher_goldens.PHYSICAL_ROOT.is_dir(),
    reason="needs the sibling onr_physical_runtime checkout",
)


def _probes(
    *,
    busy: frozenset[int] = frozenset(),
    models: tuple[str, ...] = (MODEL,),
    gpu: str | None = "0, 40000, 81559\n1, 40000, 81559\n",
    reachable: bool = True,
) -> PreflightProbes:
    def http_get(url: str, _timeout: float) -> bytes:
        assert url == "http://127.0.0.1:11411/v1/models"
        if not reachable:
            raise OSError("connection refused")
        return json.dumps({"data": [{"id": model} for model in models]}).encode()

    return PreflightProbes(
        http_get=http_get,
        port_free=lambda port: port not in busy,
        module_available=lambda _name: True,
        disk_free_bytes=lambda _path: 500 * 1024**3,
        gpu_query=lambda: gpu,
        allocate_port=lambda: 5140,
    )


def _checks(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {check["check_id"]: check for check in report["checks"]}  # type: ignore[index, union-attr]


def test_busy_airsim_port_blocks_launch_in_the_contract_shape() -> None:
    example = json.loads((CONTRACT / "stack-preflight.response.json").read_text())
    report = run_preflight(
        load_stack_catalog(),
        "mission1-airsim",
        {"perception": "yolo"},
        repo_root=REPOSITORY,
        probes=_probes(
            busy=frozenset({41451}), gpu="0, 40000, 81559\n1, 1536, 81559\n"
        ),
    )

    assert report.keys() == example.keys()
    assert report["toggles"] == example["toggles"]
    assert report["launchable"] is False
    checks = _checks(report)
    for check in report["checks"]:  # type: ignore[union-attr]
        assert check.keys() == example["checks"][0].keys()
    expected = {check["check_id"]: check for check in example["checks"]}
    assert checks["vllm"] == expected["vllm"]
    assert checks["port:41451"] == expected["port:41451"]
    assert checks["gpu"] == {**expected["gpu"], "detail": "GPU1 free 1.5 GiB"}
    assert {
        "engine",
        "perception",
        "yolo",
        "port:8766",
        "port:viewer",
        "disk",
    } <= checks.keys()
    assert all(
        checks[name]["status"] == "pass" for name in ("engine", "perception", "yolo")
    )


def test_simulated_harbor_preset_is_launchable_with_healthy_probes() -> None:
    report = run_preflight(
        load_stack_catalog(),
        None,
        None,
        repo_root=REPOSITORY,
        probes=_probes(),
        active_run=lambda: None,
    )

    assert report["preset_id"] == "mission1-harbor"
    assert report["launchable"] is True, report["checks"]
    checks = _checks(report)
    assert "port:41451" not in checks and "engine" not in checks
    assert checks["planners"]["status"] == "pass"
    assert checks["active-run"]["status"] == "pass"


@pytest.mark.parametrize(
    ("probes", "detail"),
    [
        (_probes(models=("other/model",)), f"{MODEL} not served"),
        (_probes(reachable=False), "connection refused"),
    ],
)
def test_vllm_without_the_configured_model_fails(
    probes: PreflightProbes, detail: str
) -> None:
    report = run_preflight(
        load_stack_catalog(), "mission2", None, repo_root=REPOSITORY, probes=probes
    )

    vllm = _checks(report)["vllm"]
    assert vllm["status"] == "fail" and detail in str(vllm["detail"])
    assert report["launchable"] is False


def test_another_active_mission_run_fails_preflight() -> None:
    report = run_preflight(
        load_stack_catalog(),
        "mission1-harbor",
        None,
        repo_root=REPOSITORY,
        probes=_probes(),
        active_run=lambda: "run-other",
    )

    assert _checks(report)["active-run"]["status"] == "fail"
    assert report["launchable"] is False


def test_unsupported_toggles_explain_the_reason() -> None:
    report = run_preflight(
        load_stack_catalog(),
        "mission1-harbor",
        {"airsim": True, "perception": "ideal"},
        repo_root=REPOSITORY,
        probes=_probes(),
    )

    assert report["launchable"] is False
    assert report["toggles"] == {
        "airsim": True,
        "perception": "ideal",
        "update_ownership": "coordinator_driven",
    }
    (check,) = report["checks"]  # type: ignore[misc]
    assert check["check_id"] == "toggles" and check["status"] == "fail"
    assert "choose Mission 1 · AirSim live" in check["hint"]


def test_joint_preset_needs_a_mission1_instance() -> None:
    report = run_preflight(
        load_stack_catalog(), "joint", None, repo_root=REPOSITORY, probes=_probes()
    )

    inputs = _checks(report)["mission-inputs"]
    assert inputs["status"] == "fail" and "ONR_DEMO_MISSION1_INSTANCE" in str(
        inputs["detail"]
    )
    assert report["launchable"] is False


def test_missing_gpu_tooling_only_warns() -> None:
    report = run_preflight(
        load_stack_catalog(),
        "mission1-harbor",
        None,
        repo_root=REPOSITORY,
        probes=_probes(gpu=None),
    )

    assert _checks(report)["gpu"]["status"] == "warn"
    assert report["launchable"] is True


def test_preflight_allocated_viewer_port_skips_both_fixed_ports() -> None:
    ports = iter((41451, 8766, 5150))
    report = run_preflight(
        load_stack_catalog(),
        "mission1-airsim",
        {"perception": "ideal"},
        repo_root=REPOSITORY,
        probes=replace(_probes(), allocate_port=lambda: next(ports)),
    )
    assert report["launchable"] is True, report["checks"]
    assert _checks(report)["port:viewer"]["detail"] == "5150 free"


@pytest.mark.parametrize("viewer_port", [41451, 8766])
def test_explicit_viewer_collision_fails_preflight_even_if_ports_are_free(
    viewer_port: int,
) -> None:
    report = run_preflight(
        load_stack_catalog(),
        "mission1-airsim",
        {"perception": "ideal"},
        repo_root=REPOSITORY,
        viewer_port=viewer_port,
        probes=_probes(),
    )
    assert report["launchable"] is False
    inputs = _checks(report)["mission-inputs"]
    assert inputs["status"] == "fail"
    assert "viewer port collides" in str(inputs["detail"])
