# Full-run agent implementation inspection

- Inspection time: 2026-08-24 20:43:13 AEST (+10:00)
- Captured run: `data/temp/var_for_inspection`
- Mission: `mission:demo`
- Runtime session: 2026-08-24 09:43:24–10:19:55 UTC
- Simulated interval: 0.0–300.0 seconds in 0.5-second ticks
- Inspection method: read-only joins across transport streams, FSM records, planner artifacts, environment history, operational records, DeepAgents callbacks, and raw LLM records; relevant runtime code was then traced to distinguish observed failures from logging artifacts.

## Executive verdict

The transport and context-delivery mechanics were internally consistent during this run. Context Coordination consumed its entire input stream without gaps or dead letters, every Maneuver and Hyper episode started with a fresh current invocation, raw LLM records matched callback records one-for-one, and the pending perception buffer neither lost nor duplicated any of the 38 live perceptions.

The mission-control semantics were not correct, however. The FSM reached `patrol-objective-complete` even though required temporal and sensed-evidence predicates had been violated. Seven of 26 FSM transitions occurred before their declared time gates; two navigation commands were issued for maneuvers that did not match the live FSM state; one completed maneuver was unnecessarily resubmitted; and revision 2 confirmed its first stop despite observing zero of the one event required by that stop. The final terminal state therefore cannot be treated as proof that the accepted Statechart was physically and evidentially completed.

The principal cause is architectural: Statechart readiness is descriptive model-facing JSON, not a code-enforced guard. `transition_fsm` checks only that the requested event is the one structurally enabled candidate and then applies it. A model can—and in this run did—advance the FSM while its own `reflection` says that the transition is not warranted.

## Finding summary

| ID | Severity | Finding | Observed impact |
|---|---|---|---|
| F-01 | Critical | FSM readiness predicates are not enforced by code | 7 early transitions, including calls whose reflections explicitly say the gate is not satisfied |
| F-02 | Critical | Terminal completion is possible without required sensed evidence | Revision 2 stop 1 expected source event 108 but observed none; the FSM still completed the stop and later the mission |
| F-03 | High | Physical execution can diverge from the live FSM | 2 navigations targeted the next assignment before that assignment was active; 1 completed maneuver was resubmitted |
| F-04 | High | Replan activation does not immediately reconcile the physical maneuver | The revision 1 action continued for 5 simulated seconds, making revision 2's first navigation infeasible and 1.5 seconds late |
| F-05 | High | Agent evidence summaries can contradict authoritative context | Hyper claimed intervals had closed and expected events had been captured when the time and observation streams disproved those claims |
| F-06 | Medium | `send_once` is descriptive but has no durable execution marker | The same revision 1 first-stop replan evaluation was sent three times |
| F-07 | Medium | Belief topic cursor and committed belief state diverge | Belief state reached revision 39 while its durable consumer cursor remained at topic sequence 19 |
| F-08 | Medium | Historical evidence references are not retained as long as snapshots that cite them | 37 of 39 distinct belief artifact references in Mission Snapshots no longer resolve inside the capture |
| F-09 | Medium | Snapshot time/freshness metadata is too weak for staleness diagnosis | All 895 snapshots have the same `created_at`; `healthy`/`fresh` does not detect cross-revision physical state |
| F-10 | Low–Medium | Filesystem consumer and model-context sizes have scaling risk | Two ever-growing consumer indexes reached about 165 KiB at 955 facts; one planner LLM request reached 145,589 serialized bytes |

## 1. Run reconstruction and log integrity

### 1.1 Authoritative record counts

| Stream/artifact | Count | Integrity result |
|---|---:|---|
| Environment data | 616 | Sequences 0–615, contiguous; mission time monotonic |
| Environment perceptions | 38 | 19 entity observations plus 19 event observations, sequences 0–37 |
| FSM status | 202 | Sequences 0–201, contiguous |
| Mission Snapshots | 895 | Versions 1–895; transport sequence always equals version minus one |
| Planning evidence | 955 | Sequences 0–954, contiguous, no duplicate event IDs |
| Operational records | 1,230 | Sequences 1–1,230, contiguous; no `error`/`failed` record |
| Hyper heartbeat outcomes | 31 | 30 `no_change`, 1 `replan` |
| Maneuver heartbeats | 74 | Request IDs 0–73, contiguous; 30 effectful completions and 44 no-change outcomes |
| Physical commands | 14 | Every command had a receipt, accepted outcome, and authoritative lifecycle feedback |
| Agent-to-agent commands | 5 | Every command had a completed outcome |
| DeepAgents callback records | 659 | All complete, no recorded errors, per-role sequences contiguous |
| Raw LLM records | 344 | All HTTP 200, complete, and paired to exactly one callback LLM invocation |

The capture is about 68 MiB: approximately 37 MiB of debug material, 24 MiB of transport state, 5.6 MiB of application storage, and 1.9 MiB of planner artifacts.

### 1.2 Context Coordination cursor

The Context Coordination subscription is complete:

- Topic: `planning-evidence`
- Topic range: 0–954
- `cursor.json`: sequence/event 954
- `processed.json`: 955 identities
- `attempts.json`: 955 identities, all with attempt count 1
- Dead letters: none

The 955 inputs comprise 616 environment facts, 150 FSM facts, 148 active-maneuver facts, 39 belief updates, and 2 planner revisions. They produced 895 Mission Snapshots; fewer snapshots than input facts is expected because repeated source facts that do not change `_facts` return no new snapshot.

No source revision regressed across the 895 snapshots. Every FSM and environment reference used by an actual agent invocation resolved to the captured transport event. Context Coordination deliberately removes the embedded `perceptions` field from resolved environment data and supplies perceptions separately to Maneuver; after accounting for that transformation, the serialized agent contexts matched their referenced source events.

### 1.3 Pending perception buffer

The Maneuver perception buffer behaved correctly in this run:

- 38 distinct observation IDs entered Maneuver invocations.
- Every ID appeared in exactly one invocation; none was repeated while waiting for ingestion.
- All 38 IDs occurred in one of 17 successful `ingest_perceptions` calls.
- The largest batch was six perceptions: three entity/event pairs at mission time 299.0.
- The tool intentionally filters the batch to `EventObservation`, so 19 event observations produced belief revisions 21–39 while the paired entity observations remained audit context.
- The final buffer was empty.

This agrees with `ContextCoordination.run`: ticks append ordered perceptions, and a successful `ingest_perceptions` execution deletes exactly the prefix supplied to that heartbeat. The loop is serialized, so there was no concurrent append during a model invocation in this run.

## 2. Critical FSM readiness failure

### 2.1 Reproduced early transitions

The following transitions were accepted before their own readiness time. `Early by` is `required_time - mission_time`.

| Plan | Heartbeat | Transition | Mission time | Required time | Early by |
|---:|---:|---|---:|---:|---:|
| 1 | 7 | `assignment-1-outcome-confirmed` | 30.0 | 57.0 | 27.0 s |
| 1 | 12 | `assignment-2-may-begin` | 55.0 | 57.0 | 2.0 s |
| 1 | 19 | `assignment-2-outcome-confirmed` | 85.0 | 98.5 | 13.5 s |
| 1 | 23 | `assignment-3-outcome-confirmed` | 102.5 | 103.0 | 0.5 s |
| 2 | 64 | `assignment-2-outcome-confirmed` | 265.5 | 270.0 | 4.5 s |
| 2 | 67 | `assignment-3-outcome-confirmed` | 277.0 | 277.5 | 0.5 s |
| 2 | 72 | `assignment-4-outcome-confirmed` | 299.0 | 299.5 | 0.5 s |

Two callback records make the failure unambiguous rather than inferential:

- Maneuver callback sequence 70 says, “Mission time 55.0s precedes readiness gate (57.0s) ... No transition warranted this heartbeat,” but its tool call is `transition_fsm(event="assignment-2-may-begin")`, and the returned state is `assignment-2-in-progress`.
- Maneuver callback sequence 105 says the interval is incomplete, the transition is “not appropriate at this time,” and still calls `transition_fsm(event="assignment-2-outcome-confirmed")`; the returned state is `assignment-2-outcome-achieved`.

The relevant evidence is under:

- `debug/agent/maneuver-control/mission%3Ademo/00000000000000000070.json`
- `debug/agent/maneuver-control/mission%3Ademo/00000000000000000105.json`
- `planner-artifacts/revision-001/statechart-attempts/001/accepted-statechart.json`
- `planner-artifacts/revision-002/statechart-attempts/001/accepted-statechart.json`

### 2.2 Code-level cause

`src/onr/agents/maneuver_tools.py:168` implements `transition_fsm`. Its executable checks are:

1. the live mission and plan revision match the invocation;
2. exactly one current transition candidate has the requested event name; and
3. `FSMRunner.apply` reaches the candidate target.

It never evaluates `not_before`, `mission_time_at_or_after`, `live_evidence`, or `sensed_evidence`. The free-text reflection is copied only into an audit decision payload. The existing test name `test_transition_tool_checks_exact_candidate_without_interpreting_context` confirms that this is the current intended seam, not a one-off logging defect.

The result is a safety boundary inversion: untrusted model choice determines whether readiness is true, while code verifies only graph adjacency.

### 2.3 Concrete missed events caused by early departure

Revision 1 planned four events at stop `patrol-action-179` during 16.5–57.0 seconds: source indices 14, 16, 23, and 24. The drone observed only 14, 16, and 23. It departed at 55.0 seconds because of the early `assignment-2-may-begin`; source event 24 occurred at 56.5 seconds after the drone had started moving away and was missed.

Revision 1 planned two events at stop `patrol-action-347` during 81.0–98.5 seconds: source indices 29 and 34. The drone observed only 29. At 85.0 seconds the agent prematurely confirmed the interval and dispatched the next navigation, so source event 34 at 98.0 seconds was missed.

This is a direct physical consequence of the FSM error, not merely an inaccurate state label.

## 3. Terminal state without final-plan evidence

Revision 2 is the final accepted plan, so it is the decisive completion test.

| Stop | Evidence interval | Required event indices/count | Actually observed | State transition result |
|---|---|---|---|---|
| `patrol-action-684` | 220.5–221.0 | source 108; count 1 | none | Outcome confirmed at 222.0 anyway |
| `patrol-action-711` | 265.5–270.0 | sources 122, 126; count 2 | 122 and 126 | Outcome confirmed at 265.5 after only 122 existed, 4.5 s early |
| `patrol-action-730` | 277.0–277.5 | source 129; count 1 | 129 | Outcome confirmed 0.5 s early |
| `patrol-action-776` | 299.0–299.5 | sources 183–185; count 3 | 183–185 | Outcome confirmed 0.5 s early |

The final plan expected seven event observations and produced six. Source event 108 never appears in `transport/topics/environment-perceptions`, yet Maneuver callback sequence 313 confirmed stop 1 using only physical arrival: “Navigate patrol-action-684 completed at mission time 222.0s ... satisfying the live-evidence condition.” It did not address the candidate's `sensed_evidence` requirement.

The persisted execution record nevertheless ends with:

```text
assignment-4-outcome-confirmed
patrol-objective-may-complete
active_state = patrol-objective-complete
```

This is recorded in `storage/fsm/mission:demo/execution-record.json`. Consequently, `terminal=true` would mean only that every structural event was applied, not that every declared readiness predicate was satisfied.

## 4. Physical action versus FSM alignment

### 4.1 Command audit

| Maneuver | Plan | Dispatch time | Live FSM state at dispatch, after same-heartbeat transitions | Deadline | Physical result | Assessment |
|---|---:|---:|---|---:|---|---|
| `patrol-action-179` | 1 | 0.0 | assignment 1 in progress | 16.5 | completed 16.5 | aligned |
| `patrol-action-347` | 1 | 55.0 | assignment 2 in progress | 81.0 | completed 81.0 | identity aligned, but the state transition was 2.0 s early |
| `patrol-action-417` | 1 | 85.0 | assignment 2 outcome achieved | 102.5 | completed 102.5 | **does not match live FSM**; assignment 3 became active at 100.0 |
| `patrol-action-435` | 1 | 105.0 | assignment 4 in progress | 118.5 | completed 118.5 | aligned |
| `patrol-action-480` | 1 | 120.0 | assignment 5 in progress | 125.5 | completed 125.5 | aligned |
| `patrol-action-532` | 1 | 130.0 | assignment 5 outcome achieved | 139.0 | completed 139.0 | **does not match live FSM**; assignment 6 became active at 135.0 |
| `patrol-action-532` | 1 | 139.0 | assignment 6 in progress | 142.0 | completed 139.5 | redundant zero-distance resubmission of an already completed maneuver |
| `patrol-action-593` | 1 | 145.0 | assignment 7 in progress | 164.5 | completed 164.5 | aligned |
| `patrol-action-600` | 1 | 165.0 | assignment 8 in progress | 176.5 | completed 176.5 | aligned |
| `patrol-action-658` | 1 | 180.0 | assignment 9 in progress | 204.5 | cancelled at replan handoff | infeasible at dispatch; earliest arrival 206.49 |
| `patrol-action-684` | 2 | 195.0 | assignment 1 in progress | 220.5 | completed 222.0 | infeasible at dispatch; 1.5 s late |
| `patrol-action-711` | 2 | 222.0 | assignment 2 in progress | 265.5 | completed 265.5 | aligned |
| `patrol-action-730` | 2 | 270.0 | assignment 3 in progress | 277.0 | completed 277.0 | aligned |
| `patrol-action-776` | 2 | 280.0 | assignment 4 in progress | 299.0 | completed 299.0 | aligned |

All 14 commands were accepted by the adapter and produced authoritative feedback; the issue is therefore upstream command selection and FSM semantics, not command delivery.

### 4.2 Replan handoff made the replacement action infeasible

Hyper correctly detected at 190.0 seconds that `patrol-action-658` could not reach its target by 204.5. Revision 2 was planned from the drone position at 190.0 and selected `patrol-action-684`, whose event interval began at 220.5.

The closed loop did not reconcile physical execution immediately when the new Statechart was activated:

```text
t=190.0  Hyper returns REPLAN; revision 2 is planned and activated
           |
           | revision 1 patrol-action-658 remains physically active
           v
t=190.0 .. 195.0  drone continues toward the superseded revision 1 target
           |
           v
t=195.0  revision 2 patrol-action-684 is finally dispatched and cancels action-658
           |
           | distance now 533.72; at max speed earliest arrival is 221.69
           v
t=220.5  source event 108 occurs while drone is outside the selected stop's FoV
t=222.0  drone arrives; FSM confirms the stop despite zero sensed evidence
```

Environment sequences 390–401 show the revision 1 maneuver continuing from 190.0 through 195.0. Environment sequence 402 activates revision 2. During that five-second gap the planner revision and physical maneuver revision disagree, but all Context Coordination sources remain marked healthy and fresh.

The loop ordering explains the gap: Maneuver runs before Hyper at a given mission time; after Hyper activates a replacement Statechart, no immediate Maneuver trigger is queued, so control waits until the next five-second periodic boundary.

## 5. DeepAgents context and reasoning audit

### 5.1 What was delivered correctly

The DeepAgents integration did not show transcript leakage or stale serialized input:

- Maneuver: 74 fresh episodes, each beginning with exactly the system message and one current `ManeuverInvocation`; request IDs 0–73 were contiguous.
- Hyper supervisor: 31 fresh episodes, each beginning with one current `HyperHeartbeatInvocation`.
- Planner Hyper agent: two fresh workflow episodes, one per planner revision.
- Tool results and messages accumulated only within their episode. A new heartbeat did not inherit the prior heartbeat's tool transcript.
- For Maneuver and supervisor invocations, the Mission Snapshot payload, resolved FSM Status, and resolved environment scene matched the referenced transport records. Maneuver correctly received ordered raw pending perceptions separately; Hyper correctly received the current belief snapshot instead.
- Revision 2 planning used Mission Snapshot 580, environment revision 391 at mission time 190.0 (including all 253 static report events), and belief revision 33. The planner workflow constructor checks that its environment file equals the authorized environment event before exposing it.
- All 344 raw LLM requests had matching callback invocation IDs, HTTP 200 status, complete state, and recorded reasoning. No callback or tool error was present.

Thus, the primary failures were not caused by the wrong snapshot being serialized to DeepAgents.

### 5.2 Context is insufficient for cumulative sensed-evidence predicates

Maneuver receives pending perceptions since the last successful ingestion but deliberately does not receive accumulated Bayesian belief content. A fresh later heartbeat therefore cannot determine how many earlier event observations belong to a multi-heartbeat planner interval. The FSM state context does not persist an observation ledger or a satisfied-predicate result.

Hyper receives belief marginals and the last input identity, but not a ledger of all observed source event indices. Both agents can see `expected_observation_count`, yet neither has a reliable structured count of which required report events have been observed over the interval. This creates pressure to infer completion from planner fields such as `captured_event_count`, which describe what the plan expects rather than what the environment actually observed.

That confusion is visible in the run:

- At 40.0 seconds Hyper stated that the 16.5–57.0 interval “has completed” with four captured events. Time was still 40.0 and only source events 14 and 16 had been observed.
- At 50.0 seconds Hyper again stated that the four expected observations were complete; only three had been observed by then.
- At 222.0 seconds Hyper described source event 108 and `captured_event_count: 1` as successful observed completion. Belief revision was still 33 and source event 108 had never been emitted as a perception.

The authoritative context was current, but the agent's interpretation was factually wrong and no deterministic gate corrected it.

### 5.3 Stateless episodes conflict with `send_once`

Revision 1's first outcome state contains `hyper_evaluation.send_once: true`. Maneuver sent the same evaluation at heartbeats 7, 9, and 11, producing distinct request identities at mission times 30, 40, and 50. Hyper safely returned `no_change` each time, but the repeated work consumed three supervisor episodes.

A model-facing `send_once` instruction cannot be implemented reliably by fresh stateless episodes unless the fact that it was sent is persisted in code-owned state and included in later context.

### 5.4 Context-size risk

Serialized raw request sizes were:

| Role | Minimum | Median | Maximum | Maximum message count |
|---|---:|---:|---:|---:|
| Maneuver | 34,933 B | 42,076 B | 87,138 B | 14 |
| Hyper supervisor | 25,726 B | 30,125 B | 98,877 B | 14 |
| Planner Hyper agent | 31,645 B | 101,761 B | 145,589 B | 62 |

The run stayed within the configured model's capacity, but the planner transcript grows as files, shell output, and repairs accumulate. A larger event report or failed-attempt loop could exhaust the model context without a code-owned compaction strategy.

## 6. Cursor and persistence risks

### 6.1 Belief consumer cursor is behind committed state

The belief topic contains sequences 0–38 and the committed Bayesian state is revision 39. Only the initial 20 observations went through the belief service's consumer, so its durable state is:

- cursor: 19
- processed identities: 20
- attempts: 20, all attempt 1

Live Maneuver ingestion publishes observations 20–38 and then calls `belief_service.handle` directly. That commits revisions 21–39 but never acknowledges those topic records through the registered consumer.

If that consumer is later opened against the same durable store, its next delivery is input revision 21 while the manager's last input revision is 39. `_require_next_input_revision` rejects any revision less than or equal to the current revision unless it is exactly the single last committed event identity. The old event would therefore be nacked and retried rather than harmlessly acknowledged as already committed.

The demo CLI normally rolls over the prior `var` before a new run, so this failure was not exercised here. It remains a restart/resume hazard in the underlying service design.

### 6.2 Filesystem consumer index growth

At 955 Context Coordination inputs:

- `attempts.json`: 83,631 bytes
- `processed.json`: 81,721 bytes

`FileConsumer.receive` reads both files, and acknowledgement atomically rewrites the cursor and the full processed set. Attempts are never removed. Correctness was good in this run, but time and write amplification grow with the full lifetime event count rather than the unread tail.

## 7. Auditability and artifact-retention risks

### 7.1 Broken historical belief references

Mission Snapshots contain 39 distinct hash-addressed belief references. Only 2 resolve inside the completed capture; 37 refer to artifacts pruned from `storage/bayesian-beliefs/.../generations/by-content`.

This follows the bounded store policy: `FileBayesianBeliefStore` defaults to a generation history limit of three and `_prune_generations` deletes both older generation directories and their by-content artifacts. The current and near-current state remain recoverable, but old Mission Snapshots are not self-contained evidence and cannot be replayed through `_resolve_belief` after pruning.

### 7.2 Non-relocatable planner references

Planning evidence stores absolute references such as `/mnt/array/sukaih/Project/onr_agent_slim/var/planner-artifacts/...`, while the inspected copy lives under `data/temp/var_for_inspection/planner-artifacts`. Equivalent artifacts are present in the capture, but the recorded references do not resolve relative to it. Moving the archive to another machine or removing the original `var` breaks direct reference resolution.

### 7.3 Weak temporal metadata

All 895 Mission Snapshots have exactly the same `created_at`: `2026-08-23T00:00:00+10:00`. Ordering remains recoverable from version and source revisions, but age and latency are not. Hyper repeatedly says that all sources are “healthy and fresh”; these booleans are copied source facts, not computed temporal freshness or cross-revision consistency.

### 7.4 Debug-data sensitivity

The 37 MiB debug tree contains full system prompts, mission context, tool arguments/results, model reasoning, and raw model responses. This was essential for diagnosing the contradictory transition calls, but it should be treated as sensitive operational data with explicit retention and access controls. The debug recorder is fail-open, so a missing record in another run would not prove that an invocation never happened.

## 8. Recommended remediation order

1. **Make readiness executable and code-owned.** Before `FSMRunner.apply`, evaluate typed time gates, physical/lifecycle predicates, and sensed-evidence predicates against authoritative inputs. Reject the tool call with structured unmet predicates. Do not use the reflection as evidence.
2. **Persist an interval evidence ledger.** Track observed source event IDs/counts per planner assignment and expose a code-computed readiness result. Belief marginals alone are not an observation ledger.
3. **Reconcile physical execution immediately after replan activation.** Queue an immediate Maneuver heartbeat or deterministic cancellation/replacement action before advancing simulation time. Recheck distance/deadline feasibility at dispatch.
4. **Gate physical tools against live FSM context.** Require the maneuver identity and parameters to match the current state's authorized physical context, unless a separately typed emergency/override path is used.
5. **Persist one-shot effects.** Record that a `send_once` evaluation has been sent, keyed by plan revision/state/evaluation identity, and include that fact in future invocations.
6. **Unify belief ingestion with cursor advancement.** Either ingest live observations through the registered consumer or atomically mark direct-handled topic events as processed. Make replay of any already committed earlier revision idempotent rather than fatal.
7. **Retain the evidence closure for accepted snapshots.** Keep belief artifacts referenced by Mission Snapshots used for plans/replans, or export a run archive manifest containing immutable copies and verified hashes.
8. **Use relocatable artifact references.** Store repository/run-relative planner and Statechart references, with the run root supplied separately.
9. **Improve observability semantics.** Record real snapshot creation time, mission time, and a computed coherence state; distinguish per-source liveness from plan-revision consistency.
10. **Bound transport/model growth.** Compact consumer bookkeeping behind an acknowledged watermark and define planner transcript summarization or token limits before scaling event counts.

## 9. Regression tests suggested by this trace

The captured run can support deterministic replay tests at the real seams:

1. Reject `assignment-2-may-begin` at 55.0 when its gate is 57.0, even if the model requests it.
2. Reject `assignment-2-outcome-confirmed` at 85.0 when `not_before` is 98.5.
3. Reject an outcome when fewer than `expected_observation_count` source event identities have been observed.
4. Assert that source events 24 and 34 remain observable when the prior interval cannot be exited early.
5. After the 190.0 replan, reconcile the old maneuver before the next tick and prove that revision 2 action 684 remains deadline-feasible and observes source event 108.
6. Reject navigation for action 417 while the live state is still assignment 2 outcome achieved, and reject navigation for action 532 while assignment 6 is not active.
7. Prevent a second submission of a completed action 532 without an explicit override reason.
8. Reopen the belief consumer from cursor 19 with committed revision 39 and prove that sequences 20–38 are acknowledged without rollback, error, or double update.
9. Export the capture and verify that every Mission Snapshot evidence reference resolves without access to the original `var` tree.

## 10. Final assessment

The cursor, transport, callback capture, and pending perception buffer provide a strong mechanical audit trail for this run. DeepAgents generally received the correct current facts. Those facts were not converted into enforceable control invariants, however. The system currently trusts an LLM to decide semantic readiness, even when the same LLM's tool call contradicts its stated reasoning. Because this produced missed planned observations and a false terminal state, the current implementation should not use FSM terminality as a mission-success or safety assertion until F-01 and F-02 are fixed.
