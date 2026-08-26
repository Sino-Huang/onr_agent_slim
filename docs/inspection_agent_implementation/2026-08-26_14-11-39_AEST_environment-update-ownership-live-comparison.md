# Environment-update ownership live comparison

- Inspection time: 2026-08-26 14:11:39 AEST (+10:00)
- Implementation baseline: `a00337b` (`Implement environment-owned asynchronous updates`), plus the replan snapshot-coherence fix described below
- Mission: `mission:demo`
- Runtime configuration: `conf/onr_agent_params.yaml`
- Environment profile: `conf/environment_params.yaml`
- Command: `python -m onr.runtime.cli --mission-file examples/mission.json --repo-root . --config-path conf/onr_agent_params.yaml --demo-environment`

## Executive verdict

The coordinator-driven full episode passed. It reached terminal FSM state `patrol-objective-complete` at Mission time 305.0 seconds after 610 deterministic 0.5-second ticks. Maneuver delivery remained asynchronous throughout: 13 unique `navigate` commands received 13 transport-owned enqueue receipts, the environment consumer processed every command once, and the environment published 26 authoritative lifecycle events. No environment `CommandOutcome`, synchronous accepted outcome, submission marker, or legacy submission cache evidence exists.

The first coordinator-driven attempt exposed a deterministic replan handoff defect. A planning heartbeat published fresh environment data while `active_maneuver` still referenced the preceding environment event, so Context Coordination rejected the mixed Mission Snapshot as incoherent before revision-2 planning. A focused regression reproduced the exact `ValueError`. The fix republishes runtime source facts after `planning_view()` and before resolving replan evidence, bringing both references onto the planning-view event. The fresh full episode then crossed that path twice, accepting revisions 2 and 3 while an active maneuver existed.

The environment-driven run also completed cleanly, stopping at its configured 600-second simulation bound rather than at a terminal FSM state. That nonterminal result is expected evidence for the intentionally deferred stale-decision fencing work: Mission time advances substantially while each model decision is in flight, so the model-authored schedule is stale by the time it is acted on. The harness itself behaved correctly: it retained updates, coalesced periodic boundaries, kept one agent lane, propagated no producer error, and stopped and joined the producer.

## Coordinator-driven run

### Configuration and endpoint

- `updates.ownership`: `coordinator_driven`
- `updates.cadence_seconds`: `0.5`
- Runtime lease: 2026-08-26 13:14:44–14:10:01 AEST, status `stopped`
- Terminal: yes
- Final FSM state: `patrol-objective-complete`
- Final active plan/statechart revision: 3
- Accepted plan revisions: `[1, 2, 3]`
- Simulated duration: 305.0 seconds
- Tick count: 610

### Closed-loop summary

| Measure | Result |
|---|---:|
| Maneuver heartbeats | 88 |
| Environment-triggered Maneuver heartbeats | 24 |
| Hyper heartbeats | 31 |
| Physical commands | 13 |
| Maneuver feedback events | 26 |
| Perception events | 42 |
| Belief revisions | 41 |
| Maximum environment update batch | 1 |
| Coalesced updates | 0 |

Every inference window reported identical evidence and completion Mission times. This is the expected coordinator-driven behavior: environment time does not advance during Maneuver, Hyper, or replan inference. The maximum update batch of one and zero coalesced updates are consistent with that serialized pacing.

### Asynchronous command-delivery evidence

| Evidence | Result |
|---|---:|
| Unique Maneuver Command IDs | 13 |
| Command actions | 13 `navigate` |
| Maneuver transport receipts | 13 `accepted` |
| Environment-consumer processed identities | 13 |
| Final consumer command cursor | 12 |
| `active` feedback | 13 |
| `completed` feedback | 11 |
| `cancelled` feedback | 2 |
| Environment `CommandOutcome` records | 0 |
| Legacy submission markers | 0 |

The lifecycle counts reconcile exactly. Every command became active. Eleven completed normally; two were cancelled when replacement-plan commands overrode revision-1 or revision-2 work. The transport receipt's `accepted` value records durable enqueue only. Environment application and lifecycle truth appear later as feedback, including a directly observed interval at Mission time 130.0 where command seven was queued while the prior lifecycle remained completed, then was consumed and completed at Mission time 139.0.

### Evidence preservation and serialization

- Environment-data events: 626
- Mission Snapshots: 934
- Perceptions: 42 unique files/identities
- Final committed belief: generation 82, belief revision 41
- Operational records: 1,444; failed outcomes: 0
- Raw LLM records: 604, all `complete`
  - Hyper workflow: 72
  - Hyper supervisor: 67
  - Maneuver Control: 465
- Overlapping raw LLM request intervals across roles: 0

The 604 request intervals contain no overlap, providing persisted evidence for one serialized agent decision lane. Perceptions and feedback identities remain individually durable rather than being reduced to a latest-only event. Context Coordination published the latest Mission Snapshot while retaining the underlying streams.

### Replan snapshot-coherence failure and fix

The failed attempt reached Mission time 80.0 with plan revision 1 and an active maneuver. `planning_view()` published environment event `environment-data:mission:demo:70f305...`, producing Mission Snapshot version 249. Its `environment_data` reference used that new identity, but `active_maneuver` still referenced the preceding environment event `environment-data:mission:demo:caa9ee...`. `ContextCoordination._resolve_environment()` raised:

```text
ValueError: Mission Snapshot environment and active maneuver are incoherent
```

The captured belief artifact and environment event validated successfully, and a direct revision-2 workflow replay returned `execution_ready`. A focused test then reproduced the same coordinator exception with an active command during replanning. Republishing runtime source facts immediately after the planning view made the snapshot coherent before evidence resolution. The fresh full run accepted revision 2 at Mission time 70.0 and revision 3 at Mission time 280.0, exercising the repaired handoff twice.

## Environment-driven run

### Configuration and endpoint

- `updates.ownership`: `environment_driven`
- `updates.cadence_seconds`: `0.5`
- Runtime lease: 2026-08-26 14:25:29–14:39:00 AEST, status `stopped`
- Terminal: no; stopped at the configured simulation bound
- Final FSM state: `patrol-stop-4-en-route`
- Active plan/statechart revision: 1
- Simulated duration: 600.0 seconds
- Tick count: 1,200

The successful coordinator run was rolled to `data/past_debug_rounds/20260826T041258.508709Z/var`. The first environment-driven attempt described below was rolled to `data/past_debug_rounds/20260826T042529.484427Z/var`; the successful environment-driven capture remains in `var` at inspection time.

### Closed-loop summary

| Measure | Result |
|---|---:|
| Maneuver heartbeats | 7 |
| Environment-triggered Maneuver heartbeats | 3 |
| Hyper heartbeats | 6 |
| Physical commands queued | 4 |
| Commands consumed/applied | 3 |
| Maneuver feedback events | 6 |
| Perception events | 4 |
| Belief revisions | 22 |
| Maximum environment update batch | 394 |
| Coalesced updates | 168 |

Eleven inference windows completed after Mission time had advanced beyond their evidence time. Examples include Maneuver 0.0→24.5, Maneuver 46.5→87.5, Hyper 208.5→239.0, and Maneuver 369.0→402.5 seconds. The largest recorded advance inside one completed invocation was 41.0 seconds. Raw request timestamps show zero overlapping LLM intervals, so this occurred with one serialized agent lane rather than concurrent agent decisions.

Periodic triggers were coalesced to one latest identity per catch-up decision. The six Hyper decisions carried `periodic:20`, `periodic:90`, `periodic:130`, `periodic:200`, `periodic:340`, and `periodic:600`; they did not replay every crossed ten-second boundary. The maximum drained batch of 394 and coalesced count of 168 demonstrate that queued updates were folded after long inference rather than dropped or expanded into one agent call per update.

### Asynchronous delivery and evidence preservation

| Evidence | Result |
|---|---:|
| Unique Maneuver Command IDs | 4 |
| Maneuver transport receipts | 4 `accepted` |
| Environment-consumer processed identities | 3 |
| Final consumer command cursor | 2 |
| `active` feedback | 3 |
| `completed` feedback | 3 |
| Environment `CommandOutcome` records | 0 |
| Legacy submission markers | 0 |

The fourth physical action was durably queued by the final catch-up Maneuver heartbeat after the producer had reached 600.0 seconds. It has a transport receipt and appears in the closed-loop physical-action list, but the stopped environment consumer did not apply it and no lifecycle feedback was invented. This is the clearest live distinction between transport acceptance and environment acceptance in either run.

All four published perception identities were retained and the closed-loop result reported `perception_count: 4`. The final belief artifact is generation 44, belief revision 22. The successful run contains 1,204 environment-data events, 1,259 Mission Snapshots, and 1,331 operational records with zero failed outcomes. Its 86 raw LLM records are all complete: 25 Hyper workflow, 13 Hyper supervisor, and 48 Maneuver Control.

The producer reached exactly 1,200 ticks, stopped at the 600-second bound, and the process exited normally with a stopped runtime lease. No producer exception escaped and no thread kept the process alive.

### Environment-driven snapshot-coherence failure and fix

The first environment-driven attempt reached Mission time 252.0 seconds before raising the same coherence `ValueError` through a different race. While Context Coordination was completing a serialized heartbeat, the producer advanced environment-data from revision 504 to 505. The latest snapshot therefore paired environment-data revision 505 with active-maneuver revision 504. A direct replay of persisted snapshot 553 reproduced:

```text
ValueError: Mission Snapshot environment and active maneuver are incoherent
```

Unlike the coordinator replan bug, republishing a source fact cannot close this race while the producer continues ticking. The fix is in snapshot assembly: when ownership is `environment_driven` and the environment has a current maneuver, the active-maneuver reference, revision, health, and freshness are atomically projected from the latest environment-data fact. Environment data already contains the environment-authoritative current maneuver; this avoids combining adjacent ticks without weakening coordinator-driven coherence checks.

A focused regression first failed with environment revision 2 and active revision 1, then passed after the change. The barrier-controlled update-folding test and coordinator-driven replan test also passed. Across all 1,259 Mission Snapshots in the fresh 600-second live run, the mismatch count among snapshots containing an active-maneuver reference is zero.

## Comparative assessment

| Property | `coordinator_driven` | `environment_driven` |
|---|---|---|
| Environment progress during inference | No; every evidence/completion time pair equal | Yes; 11 completed windows advanced Mission time |
| Terminal result | `patrol-objective-complete` at 305.0s | Nonterminal at configured 600.0s bound |
| Tick/update batching | 610 ticks, max batch 1 | 1,200 ticks, max batch 394 |
| Periodic coalescing | 0 | 168 |
| Agent lane | Serialized; 0 overlapping LLM intervals | Serialized; 0 overlapping LLM intervals |
| Commands / applied | 13 / 13 | 4 / 3 |
| Feedback preservation | 26/26 identities | 6/6 identities for applied commands |
| Perception preservation | 42/42 | 4/4 |
| Replanning | Revisions 1→2→3, terminal | No replan before bound |
| Producer lifecycle | No background producer | Producer stopped/joined at bound |

Both ownership modes keep Maneuver Command delivery asynchronous. The toggle changes who advances Mission time, not whether Maneuver Control waits for the environment. Coordinator mode provides deterministic serialized simulation and completed the full episode. Environment mode validates independent progress and catch-up mechanics, but also demonstrates why stale-decision fencing was intentionally deferred for a later design: model decisions can complete tens of Mission seconds after their evidence, and this live run followed stale schedule deadlines without reaching terminal state before the bound.

## Final assessment

The implementation satisfies the issue #54 delivery and ownership objectives after the two snapshot-coherence fixes. Coordinator-driven mode completes the existing serialized live mission. Environment-driven mode advances during Maneuver and Hyper inference, preserves feedback and perceptions, coalesces crossed periodic boundaries, remains single-flight, and shuts down cleanly at the simulation limit. Transport receipts remain enqueue evidence only, while the unapplied fourth environment-driven command proves that no synchronous environment acceptance is implied.

The shipped profile was restored to its required `coordinator_driven` default after this comparison. To enable independent updates, change `updates.ownership` in `conf/environment_params.yaml` to `environment_driven`; no other setting is required.
