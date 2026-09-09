#!/usr/bin/env bash
set -euo pipefail

# Archived examples below are not the shared-GPU configuration.
# -e HF_HUB_OFFLINE=1
# -e TRANSFORMERS_OFFLINE=1
# -e HF_DATASETS_OFFLINE=1
# docker run --gpus '"device=0,1,2,3"' \
#   --ipc=host -p 11411:8000 \
#   -v ~/.cache/huggingface:/root/.cache/huggingface \
#   vllm/vllm-openai:gemma4-unified google/gemma-4-12B-it \
#   --tensor-parallel-size 4 \
#   --gpu-memory-utilization 0.95 \
#   --enable-auto-tool-choice \
#   --tool-call-parser gemma4 \
#   --default-chat-template-kwargs '{"enable_thinking": true}' \
#   --chat-template examples/tool_chat_template_gemma4.jinja \
#   --reasoning-parser gemma4




# Starting layout: AirSim=0, vLLM=1,2, learned perception=3.
# This scopes vLLM only; see README.md for other services.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
export CUDA_VISIBLE_DEVICES="${VLLM_CUDA_VISIBLE_DEVICES:-${CUDA_VISIBLE_DEVICES:-0,1,2,3}}"
IFS=',' read -r -a VLLM_DEVICES <<< "$CUDA_VISIBLE_DEVICES"
VLLM_TENSOR_PARALLEL_SIZE="${VLLM_TENSOR_PARALLEL_SIZE:-${#VLLM_DEVICES[@]}}"
VLLM_TMPDIR="${VLLM_TMPDIR:-$REPO_ROOT/var/vllm/tmp}"

command=(vllm serve "${VLLM_MODEL:-Qwen/Qwen3.8-27B-FP8}"
  --host "${VLLM_HOST:-0.0.0.0}"
  --port "${VLLM_PORT:-11411}"
  --tensor-parallel-size "$VLLM_TENSOR_PARALLEL_SIZE"
  --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.95}"
  --max-model-len "${VLLM_MAX_MODEL_LEN:-65536}"
  --max-num-seqs "${VLLM_MAX_NUM_SEQS:-4}"
  --max-num-batched-tokens "${VLLM_MAX_NUM_BATCHED_TOKENS:-4096}"
  --enable-auto-tool-choice
  --tool-call-parser qwen3_coder
  --reasoning-parser qwen3
  --mm-encoder-tp-mode data)

dry_run=false
if [[ "${1:-}" == --dry-run ]]; then
  dry_run=true
  shift
fi
command+=("$@")
printf 'vLLM CUDA_VISIBLE_DEVICES=%s; tensor parallelism=%s\n' "$CUDA_VISIBLE_DEVICES" "$VLLM_TENSOR_PARALLEL_SIZE"
printf 'vLLM temporary directory: %s\n' "$VLLM_TMPDIR"
printf 'Command:'
printf ' %q' "${command[@]}"
printf '\n'
if "$dry_run"; then
  exit 0
fi
mkdir -p "$VLLM_TMPDIR"
export TMPDIR="$VLLM_TMPDIR" TMP="$VLLM_TMPDIR" TEMP="$VLLM_TMPDIR"
exec "${command[@]}"

# docker run --gpus '"device=0,1,2,3"' \
#   --privileged --ipc=host -p 11411:8000 \
#   -v ~/.cache/huggingface:/root/.cache/huggingface \
#   vllm/vllm-openai:qwen38 Qwen/Qwen3.8-27B-FP8 \
#   --tensor-parallel-size 4 \
#   --gpu-memory-utilization 0.85 \
#   --enable-auto-tool-choice \
#   --tool-call-parser qwen3_coder \
#   --reasoning-parser qwen3 \
#   --mm-encoder-tp-mode data
