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




docker run --gpus '"device=0,1,2,3"' \
  --privileged --ipc=host -p 11411:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:qwen38 Qwen/Qwen3.8-27B-FP8 \
  --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.95 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --mm-encoder-tp-mode data