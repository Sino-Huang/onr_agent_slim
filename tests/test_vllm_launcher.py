from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/vllm/start_vllm.sh"


def preview(tmp_path, **overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VLLM_")}
    env.pop("CUDA_VISIBLE_DEVICES", None)
    env.update(overrides)
    env["VLLM_TMPDIR"] = str(tmp_path / "not-created")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    assert not (tmp_path / "not-created").exists()
    command = next(line for line in result.stdout.splitlines() if line.startswith("Command:"))
    return result.stdout, shlex.split(command.removeprefix("Command:"))


def test_shared_gpu_defaults_and_no_launch(tmp_path):
    output, args = preview(tmp_path)
    assert "CUDA_VISIBLE_DEVICES=1,2" in output
    assert args[args.index("--tensor-parallel-size") + 1] == "2"
    assert args[args.index("--max-model-len") + 1] == "65536"
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.95"
    assert args[args.index("--max-num-seqs") + 1] == "4"
    assert args[1:3] == ["serve", "Qwen/Qwen3.8-27B-FP8"]


def test_inherited_devices_determine_parallelism(tmp_path):
    output, args = preview(tmp_path, CUDA_VISIBLE_DEVICES="0,1,2,3")
    assert "CUDA_VISIBLE_DEVICES=0,1,2,3" in output
    assert args[args.index("--tensor-parallel-size") + 1] == "4"


def test_explicit_overrides_take_precedence(tmp_path):
    output, args = preview(tmp_path, CUDA_VISIBLE_DEVICES="0,1,2,3",
        VLLM_CUDA_VISIBLE_DEVICES="2,3", VLLM_GPU_MEMORY_UTILIZATION="0.75",
        VLLM_MAX_MODEL_LEN="16384", VLLM_PORT="11412")
    assert "CUDA_VISIBLE_DEVICES=2,3" in output
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.75"
    assert args[args.index("--max-model-len") + 1] == "16384"
    assert args[args.index("--port") + 1] == "11412"
