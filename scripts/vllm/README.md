# Shared GPU launcher

Activate conda `onr`, then run `bash scripts/vllm/start_vllm.sh`.
Use `--dry-run` to inspect the command without starting vLLM or creating files.

| Physical GPU | Intended service |
|---|---|
| 0 | AirSim / Unreal Vulkan rendering |
| 1, 2 | vLLM, tensor parallelism 2 |
| 3 | Learned perception (existing detector default cuda:3) |

This is a starting allocation, not a measured simultaneous-load guarantee.
The script restricts vLLM only; it neither reserves other GPUs nor launches or
configures other services. The current Sukai ideal-mask/current-pose interface
does not load the learned GPU detector.

For learned perception, leave all devices visible and use `--device cuda:3`,
or set its own `CUDA_VISIBLE_DEVICES=3` and use `--device cuda:0`. CUDA device
indices are renumbered within a restricted process.

AirSim uses Vulkan, so CUDA_VISIBLE_DEVICES alone does not select its graphics
adapter. The donor launcher supplies SDL_HINT_CUDA_DEVICE=0; do not assume that
hint enforces Vulkan selection. Prior local runs used GPU 0; verify the engine
PID's actual placement with nvidia-smi.

## Configuration

```bash
VLLM_CUDA_VISIBLE_DEVICES=1,2 VLLM_GPU_MEMORY_UTILIZATION=0.95 \
  bash scripts/vllm/start_vllm.sh --dry-run
```

Device priority: VLLM_CUDA_VISIBLE_DEVICES, inherited CUDA_VISIBLE_DEVICES, then
1,2. Use comma-separated devices without spaces. Tensor parallelism defaults
to the device count; VLLM_TENSOR_PARALLEL_SIZE overrides it.

Other environment overrides:

- VLLM_MODEL, VLLM_HOST, VLLM_PORT: same model/host/port defaults as before.
- VLLM_MAX_MODEL_LEN: 65536 total prompt + generated tokens.
- VLLM_MAX_NUM_SEQS: 4 concurrent sequences.
- VLLM_MAX_NUM_BATCHED_TOKENS: 4096.
- VLLM_TMPDIR: defaults to repository var/vllm/tmp.

Extra command-line arguments are forwarded. Qwen tool/reasoning parsers remain
unchanged; select compatible parsers when overriding the model.

The context/concurrency defaults reduce memory demand compared with unrestricted
model defaults. Increase them for workloads needing longer prompts, after testing
KV-cache capacity. The default 95% budget assumes vLLM has GPUs 1 and 2 to itself.
The cached FP8 checkpoint is about 28.75 GiB, but replicated vision
modules, runtime activations and caches add overhead. Two-GPU fit and performance
must be confirmed with representative text/image requests, then all services together.
Do not silently fall back to all GPUs if memory is insufficient.

Local launch verification: 85%/32K failed cache allocation; 92%/32K generated
responses but the live agent exceeded 32K context. A cold 92%/64K profile had
1.82 GiB KV memory versus 2.15 GiB required and failed. 95%/64K successfully
started and generated a response on GPUs 1,2. This proves startup and a short
generation, not four simultaneous 64K requests or the full multi-service workload.

Reference: https://docs.vllm.ai/en/latest/configuration/conserving_memory/
