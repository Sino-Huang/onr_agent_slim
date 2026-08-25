# Post-#50 live-run regression inspection

- Inspection time: 2026-08-25 10:29:29 AEST (+10:00)
- Captured run: `var`
- Baseline: `docs/inspection_agent_implementation/2026-08-24_20-43-13_AEST_full-run-inspection.md`
- Implementation: GitHub issue #50, now committed as `b0235d6`
- Mission: `mission:demo`
- Runtime session: 2026-08-25 09:48:59–09:53:26 AEST
- Persisted simulated interval: 0.0–5.0 seconds in 0.5-second ticks
- Inspection method: read-only joins across transport, FSM, environment, planner, operational, DeepAgents callback, and raw LLM records, followed by source tracing of the completion-validation path.

## Executive verdict

This is not a completed replacement for the prior 300-second full run. The runtime stopped at simulated time 5.0 seconds in nonterminal state `assignment-1-in-progress`. Only the first two Maneuver heartbeats ran, and only the first produced a durable heartbeat completion. No outcome transition, Hyper execution heartbeat, communication, replan, or terminal completion was reached. Findings that require those events cannot be declared fixed from this capture.

The partial trace does verify several parts of issue #50. Maneuver received the focused FSM context, selected and durably consumed the first Transition Intent, transitioned without a model-facing event name, received the new active-state operational context only after transition, submitted one aligned navigation, selected the next intent at 5.0 seconds, correctly withheld the 57.0-second-gated transition, and did not resubmit the still-suitable active navigation. The accepted Statechart contains outcome facts and no `navigation_adapter_parameters`. Todo middleware and `write_todos` are present and used.

A new live blocker prevented further evaluation. At 5.0 seconds Maneuver persisted the next Transition Intent and then returned `no_change`. Completion validation correctly treated the successful `set_transition_target` call as an effect and rejected `no_change`. Its single retry replaced the original invocation with a correction-only message. Without the live mission context, the retry claimed no FSM context had been supplied, emitted mission ID `demo` instead of `mission:demo`, and again returned `no_change`. The provider then exhausted its retry budget. The second heartbeat has no operational completion record, and the runtime lease stopped 0.015 seconds after the last invalid LLM response.

The console exception was not retained, so the final propagation from provider failure to process exit is an inference. The persisted correction chain, missing heartbeat record, nonterminal stopped lease, source behavior, and timestamps all support that inference.

## Finding comparison

| Prior ID | Status in this capture | Evidence-based assessment |
|---|---|---|
| F-01 | Mitigated in the exercised slice; not closed | The only applied transition met its 0.0-second gate. At 5.0 seconds the agent selected the 57.0-second-gated target but did not transition. Semantic readiness is still deliberately agent-owned rather than code-evaluated, so a five-second trace cannot prove that early transitions are eliminated. |
| F-02 | Not exercised | No evidence-interval outcome state or terminal state was reached. The run cannot test completion with missing sensed evidence. |
| F-03 | Partially verified fixed | The sole navigation matched the newly active assignment. Target operational context was unavailable before transition and became available immediately after it. At 5.0 seconds the suitable active navigation was not resubmitted. Later assignments and overrides were not reached. |
| F-04 | Not exercised | No Hyper execution heartbeat or replacement plan occurred, so same-time replan reconciliation was not observed live. |
| F-05 | Not exercised | The run stopped before live event perceptions and Hyper execution summaries. Planning workflow activity does not test this finding. |
| F-06 | Not exercised | The declared once-per-state-entry Hyper evaluation remained in a future outcome state and was never sent. Durable deduplication was not exercised. |
| F-07 | Healthy at the stop point; original hazard not exercised | Belief topic sequences 0–19, cursor 19, 20 processed identities, and committed belief revision 20 agree. No live Maneuver perception ingestion occurred, which was the path that created the prior divergence. |
| F-08 | Still present | Mission Snapshots contain 20 distinct belief references; only 2 resolve in the current `var` tree. Eighteen already-pruned references reproduce the prior evidence-closure failure. |
| F-09 | Still present | All 39 Mission Snapshots again use `2026-08-23T00:00:00+10:00` as `created_at`. |
| F-10 | Still a risk | The short run does not stress consumer growth, but request size remains large: Maneuver reached 100,728 bytes and the Hyper planning workflow reached 142,395 bytes. The consumer indexes retain the same lifetime-growth structure. |

The prior report also noted relocatability and debug-retention risks outside the numbered summary. They remain: all snapshot plan references use one absolute repository path, and the incomplete run already produced about 7 MiB of full debug records.

## New finding N-01: completion correction aborts an intent-only heartbeat

Severity: **High**. Impact: the live closed loop stopped at the second heartbeat, before the first navigation completed or any earlier semantic finding could be retested.

### Observed sequence

| Stage | Simulated time | Durable evidence |
|---|---:|---|
| Select initial target | 0.0 | Transition Intent sequence 0 selects `assignment-1-in-progress` with the exact Statechart condition. |
| Apply initial transition | 0.0 | Intent sequence 1 consumes the selection; the FSM internally records event `assignment-1-may-begin`. |
| Submit physical action | 0.0 | One accepted `navigate` command targets `patrol-action-185` at `(306, -17)` with deadline 16.5. |
| Select outcome target | 5.0 | Intent sequence 2 selects `assignment-1-outcome-achieved`, copying the condition with `not_before: 57.0` and expected observation count 4. |
| Preserve continuity | 5.0 | The agent explicitly reports that the gate is unsatisfied and the active navigation is suitable; no second physical command exists. |
| Invalid completion | 5.0 | Raw Maneuver LLM sequence 11 returns `outcome: no_change` after one successful `set_transition_target`. |
| Correction retry | 5.0 | Raw sequence 12 receives only `completion_correction`, request ID, and tool counts; the original invocation is absent. |
| Invalid retry completion | 5.0 | Raw sequence 15 returns mission ID `demo`, says no live FSM context was supplied, and again returns `no_change`. |
| Runtime stops | 5.0 | Only request 0 has an operational heartbeat record. The final LLM response ends at `23:53:26.480613Z`; the stopped lease is updated at `23:53:26.495564Z`. |

The tight trace assertion joined the stopped lease, nonterminal FSM, 5.0-second environment state, correction text, wrong retry identity, and missing request-1 heartbeat record. It failed deterministically with:

```text
AssertionError: FAIL: correction retry lost live context/identity and the run stopped nonterminal at t=5.0
```

### Cause

`DeepAgentsHeartbeatProvider._validate_completion` in `src/onr/agents/maneuver_control.py:245` rejects `no_change` whenever the heartbeat execution record contains any operational tool execution. That is consistent with the issue #50 design: Transition Intent is durable mission state, while only todo and skill tools are workflow aids. A heartbeat that newly selects an intent should complete as `completed`, even when it sends no physical command.

The recovery path is the decisive defect. `DeepAgentsHeartbeatProvider.heartbeat` at `src/onr/agents/maneuver_control.py:223` replaces `messages` with one correction-only `HumanMessage`. The retry retains the opaque tool context in code but loses the model-visible `ManeuverInvocation`. The model therefore cannot inspect the FSM, environment, current action, authoritative mission ID, or the work it just performed. It creates a second todo list, rereads the skill, and invents a context-free completion. With the default retry budget of one, the identity validation failure is raised.

`ManeuverControl._tool_heartbeat` emits the operational heartbeat record only after the provider returns successfully. This explains why all individual LLM/tool callbacks are marked complete and the operational log contains no `failed` outcome even though the closed loop stopped.

### Required regression

Use the exact second-heartbeat shape from this capture:

1. Current state is `assignment-1-in-progress` at 5.0 seconds.
2. The only target condition has `not_before: 57.0`.
3. The active `patrol-action-185` navigation is nonterminal and suitable.
4. Maneuver calls `set_transition_target` once, does not call `transition_fsm`, and submits no physical command.
5. The final outcome is `completed`, because Transition Intent changed.
6. No completion correction is needed and the loop advances to the next environment tick.

Also cover the generic retry seam: when correction is required, retain or re-supply the original serialized invocation and exact expected mission/request identities. Workflow-aid calls made during correction must not obscure the already-recorded mission effect.

## Run reconstruction and integrity

### Runtime endpoint

- Lease status: `stopped`
- Lease start: `2026-08-24T23:48:59.814285+00:00`
- Lease last seen: `2026-08-24T23:53:26.495564+00:00`
- Last environment Mission time: 5.0 seconds
- Last environment lifecycle: active navigation
- Final FSM state: `assignment-1-in-progress`
- FSM transition history: only `assignment-1-may-begin`
- Terminal: no

The runtime did not persist a normal closed-loop result or terminal classification under `var`. The lease's clean `stopped` label describes process lease cleanup, not mission success.

### Record counts

| Stream/artifact | Count | Integrity result |
|---|---:|---|
| Environment data | 12 | Sequences 0–11, contiguous; Mission time monotonic from 0.0 to 5.0 |
| Environment perceptions | 0 | The run stopped before the first selected evidence event |
| FSM status | 8 | Sequences 0–7, contiguous |
| Transition Intent events | 3 | `selected`, `consumed`, `selected`; exact condition copies |
| Mission Snapshots | 39 | Sequences 0–38, contiguous |
| Planning evidence | 39 | Sequences 0–38, contiguous |
| Operational records | 56 | Sequences 1–56, contiguous; no recorded failed/error outcome |
| Completed Maneuver heartbeats | 1 | Request 0 completed; request 1 is absent |
| Physical commands | 1 | Accepted by transport and active in the environment |
| Hyper execution heartbeats | 0 | No `hyper-heartbeat-outcomes` topic was created |
| Maneuver raw LLM records | 15 | All HTTP 200 and paired with 15 LLM callbacks |
| Hyper workflow raw LLM records | 23 | All HTTP 200 and paired with 23 LLM callbacks |
| Maneuver callback records | 34 | Contiguous, all complete; includes LLM and tool callbacks |
| Hyper workflow callback records | 59 | Contiguous, all complete; includes LLM and tool callbacks |

Context Coordination fully consumed what was published before termination: planning-evidence cursor 38, 39 processed identities, and 39 attempt-1 records. The absence of an operational error record is an observability gap at the provider boundary, not evidence that the run completed normally.

## Transition Intent and focused-context checks

### Intent lifecycle

The three append-only `transition-intents` events are internally consistent:

1. Selection revision 0 selects `patrol-awaiting-first-assignment -> assignment-1-in-progress` at 0.0 seconds.
2. Sequence 1 consumes that same intent after the transition.
3. Selection revision 2 selects `assignment-1-in-progress -> assignment-1-outcome-achieved` at 5.0 seconds and remains selected.

Every persisted condition exactly equals the matching accepted-Statechart transition context. The agent did not rewrite a readiness predicate. Source state, target state, plan revision, Statechart revision, and state-entry revision all match the live authority.

### Model-facing versus internal identity

The initial serialized `fsm_context` contains exactly:

- `current_state`
- `current_state_context`
- `state_entry_revision`
- `transition_candidates`
- `transition_intent`

Each candidate contains only `target_state` and `condition`. No Statechart event or future target-state context is exposed. The `transition_fsm` tool call uses `current_state`, `next_state`, `assessment`, `evidence`, and `uncertainty`; it has no event-name argument. After application, the FSM execution record retains internal event `assignment-1-may-begin` for audit.

The first transition's uncertainty text explicitly notes that the selected stop coordinates were not exposed before transition. Its returned focused context then supplies assignment 1's current-state outcome facts, from which the agent selects navigation. This is direct live evidence that future operational context was withheld until the state became active.

### Physical continuity

The sole physical command is aligned with the active state:

- State: `assignment-1-in-progress`
- Planner identity: `patrol-action-185`
- Action selected at runtime: `navigate`
- Target: `(306, -17)`
- Deadline: 16.5 seconds
- Transport outcome: accepted
- Environment lifecycle at 5.0 seconds: active

At the 5.0-second heartbeat the agent judged that action suitable and submitted no replacement. There is therefore no future-state command and no redundant active-action override in the exercised slice. The prior `patrol-action-417` scenario was not reached.

## Middleware and generated-Statechart checks

- The Maneuver debug profile lists `write_todos`, `set_transition_target`, `transition_fsm`, all physical tools, `communicate`, and the completion schema.
- The two original live heartbeat episodes each created and completed one seven-part heartbeat todo list.
- The correction retry created an additional todo list because it began a context-free agent episode; this is a consequence of N-01 and violates the intended one-list-per-heartbeat behavior across retries.
- The accepted Statechart contains no `navigation_adapter_parameters` key.
- The current assignment context carries planner-derived desired location and arrival deadline; the physical `navigate` parameters were chosen later by Maneuver.
- The first outcome state declares stable evaluation ID `first-evidence-interval-replan` with delivery policy `once_per_state_entry`, but the run never activated that state.

## Persistence, auditability, and growth findings

### F-07: belief cursor

The belief service is consistent at this early stop:

- Topic sequences: 0–19
- Consumer cursor: 19
- Processed identities: 20
- Attempts: 20, all initial deliveries
- Committed belief revision: 20

This only covers initial belief construction. Because no live perception reached Maneuver, it does not exercise the direct-ingestion path that left the prior cursor behind committed state.

### F-08: broken belief references and absolute plan references

The historical-reference failure recurs even in this short capture. Mission Snapshots cite 20 distinct hash-addressed belief artifacts, of which only 2 still exist under `var/storage`. Eighteen snapshot references cannot be resolved after generation pruning.

All snapshots with an active plan use the same absolute plan reference under `/mnt/array/sukaih/Project/onr_agent_slim/var/planner-artifacts/...`. The artifact exists locally now but remains non-relocatable.

### F-09: temporal metadata

All 39 Mission Snapshots have the identical creation time `2026-08-23T00:00:00+10:00`, despite the actual run occurring on 2026-08-25. Source revisions preserve ordering, but snapshot age, creation latency, and cross-source temporal coherence remain unavailable.

### F-10: transport and model growth

The short run's consumer files are small:

| Consumer | Processed bytes | Attempts bytes | Entries |
|---|---:|---:|---:|
| Context Coordination | 2,250 | 2,328 | 39 |
| Bayesian belief manager | 732 | 772 | 20 |

Their lifetime-index design is unchanged, so this capture neither removes nor meaningfully stress-tests the prior scaling risk.

Serialized request sizes were already substantial:

| Role/scope | Minimum | Median | Maximum | Maximum messages |
|---|---:|---:|---:|---:|
| Maneuver | 36,402 B | 54,063 B | 100,728 B | 18 |
| Hyper planning workflow | 31,624 B | 100,335 B | 142,395 B | 60 |

The Maneuver maximum exceeds the prior full run's 87,138 bytes even though Mission time reached only 5.0 seconds. The correction episode contributed extra model turns but did not carry the large live invocation; the principal payload remains the static operational context.

## Recommended next actions

1. Fix N-01 before another live comparison: explicitly require `completed` after a newly published/superseded Transition Intent and retain the authoritative invocation across completion correction.
2. Add the exact 5.0-second regression described above, including unchanged physical continuity and successful loop advance.
3. Persist a top-level run failure or provider-error record before lease cleanup so future captures contain the terminal exception without relying on console output.
4. Rerun the full 300-second live demo. Only that run can evaluate F-01/F-02 over evidence intervals, F-04 same-time replanning, F-05 Hyper evidence interpretation, F-06 once-per-state-entry delivery, and the later F-03 assignment scenarios.
5. Keep F-08 and F-09 open independently of the rerun; both are directly reproduced in this capture.

## Final assessment

Issue #50's core Transition Intent and focused-context mechanics are visible and correct in the portion that ran. The first timing judgment was correct, the next early transition was withheld, future operational context stayed hidden until activation, and the active physical action was not redundantly replaced. Those are meaningful improvements over the prior trace.

The run is nevertheless not acceptance evidence for the complete implementation. A new completion-correction defect stopped the closed loop at 5.0 seconds, leaving most prior critical/high findings unexercised. F-08 and F-09 remain directly reproducible. After N-01 is fixed, a complete live trace is required before the prior report's mission-success concerns can be considered resolved.
