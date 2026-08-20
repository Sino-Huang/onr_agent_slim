# Mission runtime demo

This command runs one configured mission through the real ONR runtime and keeps
the operator console as a separate read-only process. The external maneuver
environment is the installed `onr.demo.fake_environment.FakeEnvironment`, a
deterministic demo fixture. It is not production environment authority and must
be acknowledged with `--demo-environment`. The legacy
`harness.fake_environment` path is only a compatibility re-export.

## Prerequisites

- Activate the repository's `onr` conda environment.
- Ensure the configured vLLM-compatible endpoint and model are running.
- Ensure the temporal and symbolic planner executables named by
  `conf/onr_agent_params.yaml` exist and are executable.
- Use `transport.backend: file`; the demo environment consumes the file-backed
  maneuver command stream.

The mission file is strict JSON with exactly three non-empty string fields:

```json
{
  "mission_id": "mission:demo",
  "mission_text": "Survey the designated demo area and complete one bounded maneuver.",
  "source_authority": "demo-operator"
}
```

Unknown fields, missing fields, non-string values, blank values, malformed JSON,
and non-finite JSON values are rejected.

## Run

Start the read-only viewer in terminal 1:

```bash
conda activate onr
python -m onr.viewer.server --host 127.0.0.1 --port 14398 --repo-root . --config-path conf/onr_agent_params.yaml
```

Open `http://127.0.0.1:14398`, then run the mission in terminal 2:

```bash
conda activate onr
python -m onr.runtime.cli --mission-file examples/mission.json --repo-root . --config-path conf/onr_agent_params.yaml --planner-artifacts var/planner-artifacts --demo-environment
```

The CLI always verifies the configured LLM endpoint, composes the configured
real planners, creates model-backed Hyper Agent and Maneuver Control services,
and runs Context Coordination and FSM Runner. It first publishes a demo
environment heartbeat, turns that scene evidence into a Mission Snapshot, and
invokes the Hyper workflow. Hyper owns the live todo list, creates the MiniZinc
files under `--planner-artifacts`, invokes the configured solver, and returns the
verified Normalized Plan consumed by mission execution. There is no operator
supplied plan file.

The Hyper episode uses a recursion limit of 100 by default. For bounded
debugging, add a smaller value such as `--recursion-limit 5`; reaching the limit
stops the episode with a nonzero CLI result instead of continuing an agent loop.

During the active mission session,
the runtime owns the lease and writes normal transport, operational-log, FSM,
planner, and summary artifacts. It prints only mission/result identifiers and
final public status; it does not print mission text or configuration secrets.
Mission-summary calls send the per-request
`chat_template_kwargs.enable_thinking: false` override, disabling Gemma thinking
for summaries even when the vLLM server default enables it. Other model calls
retain their existing behavior.

Hyper progress is recorded as immutable mission-scoped files under
`var/storage/operational-log/<mission-id>/events/`. The `hyper-agent` records
include workflow start and terminal outcome plus `planning-intent`,
`planner-choice`, `planning-context`, `planner-assets`, and `planner-execution`
events. Repeated asset and execution events expose correction attempts. These
records contain sanitized identifiers and outcomes, not raw Mission Intent or
private model reasoning.

Before checking the configured LLM endpoint, a new demo run moves any existing
`var/` directory wholesale to
`data/past_debug_rounds/<UTC timestamp>/var/`. This preserves prior transport,
storage, planner, environment, and debug output together. If another demo
runtime session has an active lease, the new run fails safely and leaves
`var/` in place; wait for the active demo to finish before trying again.

With top-level `debug: true`, provider chat-completion responses are separated by
their runtime role and recorded atomically under:

- `var/debug/llm/hyper-agent/mission%3Ademo/*.json`
- `var/debug/llm/maneuver-control/mission%3Ademo/*.json`
- `var/debug/llm/mission-summary/mission%3Ademo/*.json`

Completed callback traces use the corresponding role-first layout:

- `var/debug/agent/hyper-agent/mission%3Ademo/*.json`
- `var/debug/agent/maneuver-control/mission%3Ademo/*.json`
- `var/debug/agent/mission-summary/mission%3Ademo/*.json`

The raw LLM artifacts contain the entire outbound JSON request body, including
prompts/messages, tool definitions, response format, and model parameters, plus
the returned content, function calls, tool calls, and provider reasoning fields.
Request headers, authorization values, cookies, API keys, URL query values, and
other credentials are not stored. Callback input, output, and error data
preserve provider reasoning fields whenever LangChain exposes those fields. The
raw LLM artifacts remain authoritative when `ChatOpenAI` does not surface a
vLLM-specific field. Existing exclusions for request headers and credentials
apply to both debug folders. Debug artifacts remain outside the viewer and trace
APIs.

The viewer owns no runtime lifecycle. It polls `/api/runtime` and the selected
mission's `/api/trace` endpoint, cannot issue commands, and cannot invoke
summarization or an LLM. A short demo may only appear active for its execution
window; after the runtime stops its lease, the viewer correctly returns to idle.
