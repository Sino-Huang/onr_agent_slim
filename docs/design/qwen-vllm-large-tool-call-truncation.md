# Qwen3.8 and vLLM large tool-call truncation

Date: 2026-08-21

## Conclusion

The failed `write_file` call is best explained by two interacting problems, not
by a missing `content` field chosen deliberately by the agent:

1. Qwen3.8 generated a tool call whose `content` parameter did not fit in the
   16,384-token completion budget.
2. vLLM parsed the already-complete `file_path` parameter from that truncated
   XML call, discarded the unclosed `content` parameter, and reported the
   result as `finish_reason="tool_calls"`. The downstream Pydantic error is a
   consequence of that partial parse.

The preceding repetitive reasoning is a separate Qwen generation-quality
problem that consumed time and tokens, but it is not sufficient to explain the
failed `data.dzn` response: that response used only 125 characters of visible
reasoning and still reached exactly 16,384 completion tokens. The strongest
fix is therefore to stop sending the whole 253-record file as one tool
argument. Qwen sampling/reasoning changes are worthwhile complementary tests,
not substitutes for bounding tool-call size.

## Local evidence

The response ending at `2026-08-21T06:25:40.396954+00:00` used
`Qwen/Qwen3.8-27B-FP8`, `temperature=0`, `stream=false`,
`max_completion_tokens=16384`, and `thinking_token_budget=4096`.

- The `model.mzn` turn returned 5,968 completion tokens. Its reasoning was
  25,785 characters and repeatedly said variants of “Continuing through the
  event log,” followed by a valid `write_file` call.
- The following `data.dzn` turn returned exactly 16,384 completion tokens,
  `finish_reason="tool_calls"`, and only 125 characters of visible reasoning.
  The parsed call was equivalent to
  `{"file_path":"/var/planner-artifacts/workspace/001/data.dzn"}`; its required
  `content` field was absent.
- A retry reproduced the same cap and missing parameter.
- The prompt was about 39,000 tokens and the server's native model length is
  262,144, so this was the per-request output cap, not exhaustion of the model's
  context window.

The exact equality between usage and the configured cap, the missing final
large parameter, and the reproduction make truncation the high-confidence root
cause. The raw, pre-parser XML was not persisted, so the exact byte at which it
ended is inferred rather than directly observed.

## Matching first-party reports and source behavior

### vLLM can mislabel a length-truncated call as a tool call

An open vLLM issue reproduces a Qwen tool call cut off by `max_tokens`. Its
streaming response contains a partial argument object and says
`finish_reason="tool_calls"`, whereas its non-streaming response says
`finish_reason="length"`. This is the closest published match to the observed
combination of a hard token cap, partial arguments, and a misleading tool-call
finish reason. The report uses Qwen3-0.6B and the Hermes parser, so it is close,
not identical. [vLLM issue #47903](https://github.com/vllm-project/vllm/issues/47903)

A second open vLLM report demonstrates the same cutoff class in the newer
parser engine used by `qwen3_coder` and `qwen3_xml`: terminating inside a Qwen
`<tool_call>` produces different streaming and non-streaming results. Its
reproduction uses `Qwen/Qwen3.6-27B`, `--tool-call-parser qwen3_coder`,
`--reasoning-parser qwen3`, and `temperature=0`, which is very close to this
deployment. [vLLM issue #47137](https://github.com/vllm-project/vllm/issues/47137)

Current vLLM source explains why `finish_reason="tool_calls"` is not reliable
evidence that generation completed. In the non-streaming auto-tool path, vLLM
sets `auto_tools_called` when its parser returns any tool call, then returns
`"tool_calls"` instead of the engine's `output.finish_reason`. Thus one parsed
parameter can mask an underlying `length` finish. [vLLM Chat Completions serving source](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/openai/chat_completion/serving.py#L924-L985)

The Qwen parser source also matches the missing-field shape. Qwen3.8's tool
protocol represents each argument as
`<parameter=name>value</parameter>`. vLLM's full parser collects matches that
have a closing parameter tag (or a following parameter opener); handling an
unclosed last parameter is conditional on partial parsing. A completed
`file_path` followed by a truncated, unclosed `content` therefore naturally
becomes an argument object containing only `file_path`.
[vLLM Qwen3 parser source](https://github.com/vllm-project/vllm/blob/main/vllm/parser/qwen3.py#L28-L64)

This is not evidence that the model intentionally omitted `content`, nor that
Pydantic or `write_file` lost a supplied field. It is a generation cutoff made
confusing by parser/serving behavior.

### Qwen thinking can enter repetitive loops

There is also a first-party Qwen issue reporting thousands of repetitive
thinking tokens. The loop disappeared when thinking was disabled. It uses a
different Qwen3 size and llama.cpp, so it supports a model-family failure mode
rather than proving the exact Qwen3.8 cause.
[Qwen3 issue #1887](https://github.com/QwenLM/Qwen3/issues/1887)

Another Qwen report describes thinking-mode failures when transitioning from
reasoning to actual tool emission under vLLM; disabling thinking made tool
execution reliable in that reproduction. Its symptom is skipped tool calls,
not truncated arguments, but it supports treating reasoning and tool emission
as separate reliability concerns.
[Qwen3 issue #1817](https://github.com/QwenLM/Qwen3/issues/1817)

The current request also differs materially from Qwen's official operating
recommendations. Qwen3.8 defaults to `reasoning_effort="xhigh"`; the model card
recommends `temperature=1.0`, `top_p=0.95`, and `top_k=20` in thinking mode,
while this run used deterministic `temperature=0`. The card says a
`presence_penalty` between 0 and 2 can reduce endless repetition, with possible
language mixing and some quality loss. It also supports `medium` and `low`
reasoning effort and an official non-thinking mode.
[Qwen3.8-27B-FP8 model card](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#best-practices)

The model card recommends much larger output allowances for agentic work and
states a native 262,144-token context. This establishes that 16,384 is
conservative for the model, although it does not mean that blindly increasing
the cap is operationally safe or that one giant tool argument is a good
interface.
[Qwen3.8 output-length guidance](https://huggingface.co/Qwen/Qwen3.8-27B-FP8#best-practices)

## Ranked fix candidates

### 1. Bound each filesystem tool call

Restore `edit_file` during planner authoring and explicitly prescribe this
sequence for large `data.dzn` files:

1. `write_file` creates a small, valid scaffold with explicit insertion
   markers.
2. Several bounded `edit_file` calls replace markers with record batches.
3. A final edit removes the last marker.
4. Submission remains gated until both files are non-empty and no marker
   remains.

An even stronger variant is to serialize the 253 already-structured evidence
records into DZN deterministically in code, leaving the model to choose the
mapping and planner template rather than transcribe every literal through a
tool-call argument.

Tradeoffs: chunked edits take more turns and require marker/integrity checks;
deterministic serialization narrows model flexibility and needs a defined DZN
schema. Both directly remove the observed size hazard and retain static
MiniZinc verification as the correctness authority.

### 2. Fail closed on capped or incomplete tool calls

Before invoking a tool, validate its required schema and inspect usage. If
`completion_tokens == max_completion_tokens` and arguments are missing or
invalid, classify the response as a truncated generation rather than exposing
only the Pydantic “field required” error. Do not trust
`finish_reason="tool_calls"` as proof of completeness with vLLM auto-tool
parsing.

This guard does not make the file fit, but it prevents identical retries and
makes the actual failure diagnosable. Where possible, record the engine finish
reason or raw pre-parser output; current vLLM's public finish reason can mask
`length` as shown above.

### 3. Use Qwen's supported generation settings

Run an isolated comparison using Qwen's thinking defaults:
`temperature=1.0`, `top_p=0.95`, `top_k=20`, with either
`reasoning_effort="medium"` or `"low"`. For the mechanical file-emission turn,
test `chat_template_kwargs={"enable_thinking": false}` after the planning
preflight has already occurred. Non-thinking mode both avoids the observed
reasoning attractor and leaves more of the completion budget for arguments.

Tradeoffs: sampling makes runs nondeterministic; lower or disabled reasoning can
reduce planning quality and may cause more retries. Qwen explicitly warns that
lower effort does not always reduce end-to-end agent time. These changes should
be A/B tested and do not guarantee that a very large call fits.

`preserve_thinking=false` is a lower-priority experiment. It reduces historical
reasoning carried into later turns, but the 39,000-token prompt was far below
the context limit and Qwen says preserved thinking can improve agent continuity.

### 4. Increase the completion budget, with a hard ceiling

Test 32,768 tokens after fixing sampling or disabling thinking for file
emission. The prompt plus that output still fits comfortably within the native
262,144-token context, and Qwen's own agent evaluations and recommendations use
larger outputs.

Tradeoffs: more latency and KV-cache use; a repetitive model may simply consume
the larger budget. This is a reasonable confirmation experiment, but a weaker
production fix than bounded calls. Keep the cap finite and retain capped-output
detection.

### 5. Constrain the tool schema

When the phase requires exactly `write_file`, a named function choice,
`tool_choice="required"`, or strict auto-tool schema can make vLLM constrain
arguments to the declared JSON schema. vLLM documents valid-schema guarantees
for named and required tool calling and strict-schema support for auto mode.
[vLLM tool-calling documentation](https://docs.vllm.ai/en/latest/features/tool_calling/)

Tradeoffs: constrained decoding cannot put more tokens inside the completion
budget, so it cannot solve an oversized `content` value by itself. Named choice
also removes the model's ability to choose another stage-appropriate tool and
can add first-use grammar-compilation latency. Treat it as an argument-shape
guard, not the primary remedy.

### 6. Upgrade or patch vLLM's truncation semantics

Track the open truncation issues and upgrade when fixes land. A robust server
fix should preserve `finish_reason="length"` whenever generation reaches its
limit, even if a prefix was parsed as a tool call, and should not present an
incomplete call as executable. Recovering the raw partial `content` can improve
diagnostics, but must not cause the truncated file to be written.

Tradeoffs: the current deployment uses a development vLLM build, so upgrading
requires regression tests for the official Qwen3.8 combination of
`qwen3_coder` and `qwen3`. A parser patch improves reporting but still does not
make a monolithic 253-record argument fit.

## Recommended verification order

1. Add capped/incomplete-response detection so failures are classified
   correctly.
2. Re-run the exact prompt once with Qwen's recommended thinking sampling and
   capture raw server output if available.
3. Test stage-specific non-thinking emission and a 32,768-token cap to quantify
   whether the original complete call can finish.
4. Adopt bounded scaffold-plus-edit generation (or deterministic DZN
   serialization) regardless if worst-case payloads remain close to the cap.
5. Verify the final trace contains complete writes/edits, static acceptance,
   execution, a verified `NormalizedPlan`, and the generated Statechart.

