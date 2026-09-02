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




# Vanilla vLLM (run from an environment with vLLM installed).
VLLM_TMPDIR="${VLLM_TMPDIR:-${PWD:?Activate the onr conda environment first}/.cache/tmp/vllm}"
mkdir -p "$VLLM_TMPDIR" || exit 1
export TMPDIR="$VLLM_TMPDIR"
export TMP="$VLLM_TMPDIR"
export TEMP="$VLLM_TMPDIR"

echo "vLLM temporary directory: $VLLM_TMPDIR"

CUDA_VISIBLE_DEVICES=0,1,2,3 vllm serve Qwen/Qwen3.8-27B-FP8 \
  --host 0.0.0.0 \
  --port 11411 \
  --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.85 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --mm-encoder-tp-mode data

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
