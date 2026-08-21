# Applying Qwen3.8 best practices in DeepAgents

Date: 2026-08-21

## Decision

The first corrective baseline for the live Hyper workflow is:

- use Qwen3.8's published **thinking-mode sampling**;
- use `reasoning_effort="medium"`;
- do not send either a generation cap (`max_tokens` or
  `max_completion_tokens`) or vLLM's separate `thinking_token_budget`;
- keep thinking enabled while the baseline is measured.

This baseline is already represented in the current worktree. Stage-specific
non-thinking generation, historical-reasoning transport, and bounded file
emission remain separate candidate improvements; they are not part of the
applied baseline.

The most important correction to the earlier diagnosis is that the observed
16,384-token boundary explains **where** the incomplete tool call was cut off,
but it does not explain **why** Qwen entered a repetitive reasoning trajectory.
The clearest configuration mismatch was greedy `temperature=0`: Qwen ships
`do_sample=true` and recommends `temperature=1.0` for thinking mode.
[Qwen generation configuration](https://huggingface.co/Qwen/Qwen3.8-27B-FP8/blob/main/generation_config.json),
[Qwen3.8 best practices](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#best-practices)

## What Qwen actually recommends

Qwen publishes two complete parameter sets. They should be treated as sets,
not as independent knobs selected piecemeal.

| Mode | `temperature` | `top_p` | `top_k` | `min_p` | `presence_penalty` | `repetition_penalty` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Thinking | 1.0 | 0.95 | 20 | 0.0 | 0.0 | 1.0 |
| Non-thinking | 0.7 | 0.80 | 20 | 0.0 | 1.5 | 1.0 |

Qwen says `presence_penalty` may be adjusted from 0 to 2 to reduce endless
repetition, but also warns that larger values can cause language mixing and a
small quality reduction. Therefore `presence_penalty=1.5` is appropriate for
the published non-thinking profile, not an automatic repetition fix for the
thinking profile. [Qwen3.8 best practices](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#best-practices)

The model's reasoning-effort vocabulary is exactly:

- `xhigh` (the default) for complex, thorough analysis;
- `medium` for an accuracy/speed balance;
- `low` for lower cost and latency.

The official chat template rejects unsupported values and recognizes only
`xhigh`, `medium`, and `low`; a generic provider value such as `high` must not
be sent to this model. Qwen also warns that lower per-turn reasoning effort can
increase total retries, latency, and token use in multi-turn agent work.
[Qwen chat template](https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/chat_template.jinja),
[Qwen3.8 API guidance](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#api-usage)

Thinking is enabled by default. For an OpenAI-compatible self-hosted endpoint,
the explicit request control is:

```json
{
  "chat_template_kwargs": {
    "enable_thinking": false
  }
}
```

`preserve_thinking` is also true by default. It controls whether reasoning
blocks from earlier assistant messages remain in the rendered conversation.
Qwen says preservation benefits decision consistency, avoids redundant
reasoning, and improves KV-cache use in multi-turn agent scenarios. Disabling
new reasoning with `enable_thinking=false` and retaining historical reasoning
with `preserve_thinking=true` are distinct choices.
[Qwen non-thinking example](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#instruct-or-non-thinking-mode),
[Qwen preserved-thinking guidance](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#disable-preserved-thinking)

## Why the output-length advice is easy to misread

Qwen's best-practices section conditionally recommends, **for frameworks that
support separate limits** and within a 1M-token context, capacity for up to
262,144 reasoning tokens and up to 131,072 final-response tokens. These are
separate-capacity recommendations for very long agentic work. They are not a
recommendation to set Chat Completions `max_tokens=262144`, and they do not say
that every request should consume those amounts.
[Qwen adequate-output guidance](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#best-practices)

vLLM's Chat Completions request has `max_tokens` and
`max_completion_tokens`, but resolves them to one `max_output_tokens` value
and then one sampling `max_tokens` value. That one ceiling covers the entire
generated sequence: reasoning plus final text or serialized tool calls. It is
not two independent Qwen limits. Omitting the field delegates the available
generation length to the server and its configured context constraints; it
does not recreate Qwen's suggested separate reasoning/final allowances.
[vLLM Chat Completions protocol](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/openai/chat_completion/protocol.py)

vLLM additionally exposes `thinking_token_budget`. This is a vLLM generation
control that forcibly limits the thinking portion when reasoning parsing is
configured. It is different from Qwen's qualitative `reasoning_effort`, and
the Qwen3.8 model card does not recommend a 4,096-token thinking budget. It
should therefore be absent from the diagnostic baseline rather than treated
as Qwen's prescribed low- or medium-effort setting.
[vLLM reasoning outputs](https://docs.vllm.ai/en/latest/features/reasoning_outputs/),
[vLLM sampling parameters](https://github.com/vllm-project/vllm/blob/main/vllm/sampling_params.py)

## vLLM support in this deployment

The installed environment currently contains vLLM 0.27.1. Its Chat
Completions schema supports all six sampling controls above:
`temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, and
`repetition_penalty`. It also accepts `reasoning_effort`, forwards it into chat
template rendering, and turns thinking on unless an explicit
`chat_template_kwargs.enable_thinking` override says otherwise.
[vLLM Chat Completions protocol](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/openai/chat_completion/protocol.py)

The official vLLM recipe for Qwen3.8 uses `--reasoning-parser qwen3` and, for
tool workloads, `--enable-auto-tool-choice --tool-call-parser qwen3_coder`.
Those server launch controls remain necessary: client-side sampling settings
do not replace reasoning or tool-call parsing.
[Official vLLM Qwen3.8 recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B)

## How the settings map through LangChain and DeepAgents

DeepAgents accepts a preconfigured LangChain chat-model instance. This repo
already passes a `ChatOpenAI` instance to `create_deep_agent`, which is the
documented route when full parameter control is needed.
[DeepAgents model configuration](https://docs.langchain.com/oss/python/deepagents/models)

With `ChatOpenAI`, standard Chat Completions fields belong directly on the
model:

```python
ChatOpenAI(
    temperature=1.0,
    top_p=0.95,
    presence_penalty=0.0,
    reasoning_effort="medium",
    extra_body={
        "top_k": 20,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
    },
)
```

`top_k`, `min_p`, `repetition_penalty`, and
`chat_template_kwargs` are vLLM extensions rather than standard OpenAI
parameters, so `extra_body` is the correct LangChain escape hatch. The
`ChatOpenAI` API explicitly recommends `extra_body`—not `model_kwargs`—for
provider-specific request fields used by OpenAI-compatible servers such as
vLLM.
[ChatOpenAI API reference](https://reference.langchain.com/python/langchain-openai/chat_models/base/ChatOpenAI)

DeepAgents' `middleware=` is ordinary LangChain agent middleware. A
`wrap_model_call` hook runs around every model invocation, and a changed
request must be passed to the handler using `request.override(...)`.
DeepAgents documents runtime model replacement through this mechanism, while
LangChain documents both dynamic model and tool selection.
[DeepAgents runtime model selection](https://docs.langchain.com/oss/python/deepagents/models#select-a-model-at-runtime),
[LangChain custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom#dynamic-model-selection)

There are two sound ways to implement stage-specific Qwen profiles:

1. Construct two complete `ChatOpenAI` instances and select one with
   `request.override(model=selected_model)`. This is the clearest option
   because each model instance owns one internally consistent Qwen parameter
   set.
2. Preserve `request.model_settings`, merge a complete per-stage setting set,
   and pass it with `request.override(model_settings=merged_settings)`. The
   installed LangChain agent factory binds `model_settings` after its normal
   tool and response-format binding, so per-call values take precedence.

For this repo, the existing Hyper `wrap_model_call` phase gate is the natural
integration seam. It already reads `HyperWorkflowContext` and calls
`request.override(...)` to choose the phase-appropriate tools. Model selection
can be done in the same immutable request override; it does not require a
second agent or a DeepAgents fork.

## Applied baseline versus remaining candidates

### Already applied in the current worktree

The runtime model construction now uses:

```text
temperature=1.0
top_p=0.95
top_k=20
min_p=0.0
presence_penalty=0.0
repetition_penalty=1.0
reasoning_effort=medium
```

It no longer supplies `max_tokens=16384` or
`thinking_token_budget=4096`. The YAML temperature is also 1.0. This is a
coherent Qwen thinking-mode baseline and implements the selected decision; it
does not yet prove that the original live workflow succeeds.

### Remaining candidate: non-thinking mechanical emission

If the corrected thinking baseline still loops or truncates while serializing
`data.dzn`, use Qwen's complete non-thinking profile only for turns whose
remaining work is mechanical emission:

```python
non_thinking_settings = {
    "temperature": 0.7,
    "top_p": 0.80,
    "presence_penalty": 1.5,
    "extra_body": {
        "top_k": 20,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "chat_template_kwargs": {
            "enable_thinking": False,
            "preserve_thinking": True,
        },
    },
}
```

Planning-intent extraction, planner/template selection, evidence-field
mapping, MiniZinc model design, and verifier-directed repair should remain in
thinking mode at `medium` effort. Initial bulk record serialization and
bounded `edit_file` population are candidates for non-thinking mode after the
mapping and output shape are fixed.

Do not route all of stage 4 to non-thinking merely because `write_file` or
`edit_file` is visible: early stage-4 model authoring and later verifier repair
still require judgment. Add an explicit workflow-context subphase (for
example, `planner_authoring` versus `data_emission` versus `repair`) and route
from that deterministic state. This avoids guessing intent from the tool list
or last message.

### Remaining candidate: preserve reasoning end to end

`preserve_thinking=true` only works when prior assistant messages sent back to
vLLM still contain their reasoning field. Qwen's own multi-turn example
explicitly appends both `reasoning_content` and `reasoning` to assistant
history. vLLM accepts those fields and the Qwen template can render them.
[Qwen multi-turn example](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#text-only-input),
[vLLM chat message handling](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/chat_utils.py)

This is a real compatibility gap in the current client stack: LangChain warns
that `ChatOpenAI` does not extract or preserve non-standard response fields
such as `reasoning_content` and `reasoning` from third-party
OpenAI-compatible providers, including vLLM. Setting
`chat_template_kwargs.preserve_thinking=true` cannot recover reasoning that
was discarded before the next DeepAgents turn.
[LangChain ChatOpenAI compatibility warning](https://docs.langchain.com/oss/python/integrations/chat/openai#api-scope)

Before depending on preserved thinking, inspect the `AIMessage` stored by the
live agent. If the field is missing, the candidates are a vLLM-specific
LangChain chat-model adapter or a narrowly scoped response conversion layer
that retains reasoning in assistant history. The raw debug recorder is useful
for diagnosis but does not by itself put reasoning back into DeepAgents state.

### Remaining candidate: bound oversized tool arguments

Correct sampling addresses the model-trajectory mismatch, but it does not
guarantee that an arbitrarily large JSON tool argument is a reliable transport
format. Keep `edit_file` available and, if the no-cap baseline still fails to
emit `data.dzn`, use scaffold-plus-bounded-edit generation or deterministic DZN
serialization. This is an interface-size fix, independent of reasoning depth
or token-cap policy.

## Verification order

1. Run the live Hyper CLI once with the already-applied thinking baseline and
   no client generation/thinking cap.
2. Confirm the actual request contains the six published sampling values and
   `reasoning_effort="medium"`.
3. Inspect whether repetition recurs, whether `data.dzn` is complete, and
   whether `submit_planner_attempt` reaches static acceptance.
4. Inspect the DeepAgents `AIMessage` history for preserved reasoning fields.
5. Only if mechanical emission still fails, A/B test the complete non-thinking
   profile for an explicit `data_emission` subphase.
6. If payload size remains a problem, adopt bounded edits or deterministic
   serialization rather than reintroducing an arbitrary thinking budget.

The acceptance criterion remains end-to-end: a statically accepted planner
attempt, successful planner execution, a verified `NormalizedPlan`, and a
validated generated Statechart. A response that merely avoids the original
Pydantic error is not sufficient.
