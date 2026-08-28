# Mission runtime demo

This command runs one configured mission through the real ONR runtime and keeps
the operator console as a separate read-only process. The environment adapter
comes from the selected environment profile. A fake profile uses the installed
`onr.demo.fake_environment.FakeEnvironment`, a deterministic fixture that must
be acknowledged with `--demo-environment`; an external physical profile does
not require that flag. The legacy `harness.fake_environment` path is only a
compatibility re-export.

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
python -m onr.runtime.cli --mission-file examples/mission.json --repo-root . --config-path conf/onr_agent_params.yaml
```

The runtime reads the planner artifact directory from
`storage.planner_artifacts` in the YAML configuration. Pass
`--planner-artifacts PATH` to override it for one run.

To exercise the real Maneuver Control agent while bypassing the currently
independent Hyper/planner workflow, run the post-Hyper demo instead:

```bash
conda activate onr
python -m onr.runtime.maneuver_cli --mission-file examples/mission.json --repo-root . --config-path conf/onr_agent_params.yaml --demo-environment
```

This command injects a code-owned accepted four-stop Normalized Plan and
ten-state Statechart, activates the real FSM Runner, and drives ten live
Maneuver heartbeats through controlled fake Mission times. It exercises
navigation, explicit environment completion ticks, communication to a Hyper
recipient stub, and an emergency landing override. It does not exercise the
closed-loop pending-perception belief path.
It does not invoke Hyper, write planner source files, or run MiniZinc.

The Maneuver-only command uses the same demo rollover behavior and persistent
debug locations as the full runtime. Its final JSON includes the exact log
directories, normally:

- `var/debug/agent/maneuver-control/mission%3Ademo/`
- `var/debug/llm/maneuver-control/mission%3Ademo/`

The CLI verifies the configured LLM endpoint, composes the real planners and
agents, seeds 20 `event-risk` beliefs through the durable Bayesian service, and
hands the accepted first revision to Context Coordination. Initial Hyper planning receives the
complete 253-event planning view and produces a planner-native `PlannerPlan`
plus an accepted Statechart. No Normalized Plan is introduced.

Context Coordination owns execution after initial planning: it resolves each
Mission Snapshot, builds every Maneuver and supervisory Hyper invocation,
coordinates replanning, and advances 0.5-second simulation ticks without sleeping. Maneuver is
invoked at time zero, every 5 simulated seconds, and immediately when the
environment publishes authoritative maneuver lifecycle feedback such as
navigation completion. MiniZinc timing remains continuous and is not rounded to
the agent heartbeat cadence. Hyper runs a fresh supervisory episode every 10
seconds and immediately after a queued Maneuver replan request; coincident
triggers are coalesced. Maneuver receives the pending raw perception batch and
never receives accumulated Bayesian belief. One successful `ingest_perceptions`
call commits each pending event separately and clears the process-local batch;
skipped or failed ingestion retains it. Hyper receives only the latest resolved
environment, belief, FSM, request, and Mission Snapshot context. File transport
retains the full audit history.

A `replan` decision launches a fresh checkpointed Hyper workflow under a
revision-specific artifact directory. Only a verified replacement Statechart
supersedes the FSM. Failed replacement planning leaves the prior revision and
any active physical action authoritative. The loop stops at a terminal FSM
state or `--simulation-limit-seconds`.

Each full Hyper planning episode uses a recursion limit of 240 by default. For bounded
debugging, add a smaller value such as `--recursion-limit 5`; reaching the limit
stops the episode with a nonzero CLI result instead of continuing an agent loop.

During the active mission session,
the runtime owns the lease and writes normal transport, operational-log, FSM,
planner, and summary artifacts. Final JSON includes simulated duration, tick and
heartbeat counts, physical actions, feedback and perception counts, belief
revisions without gaps, Hyper outcomes, plan revisions, and the final FSM state. It does not
print mission text or configuration secrets.
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
