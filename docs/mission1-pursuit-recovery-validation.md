# Mission 1 pursuit and GPS recovery validation

This is the completion receipt for issue
[#60](https://github.com/Sino-Huang/onr_agent_slim/issues/60). It separates the
recorded missing tool call in the old equal-prior run from physical visibility,
target motion, and later planner decisions. It also validates the current
code-owned pursuit handoff and recovery path with isolated model replays, focused
regressions, and completed live runs.

## Recorded failure reproduction

Run `run.8dAH8Z` used Agent `42c297c`, before the guidance correction in
`f839b52` and the code-owned recovery path in `430f15b`. At Mission time 239.5 s,
recovery navigation to ship 4's public GPS fix sampled at 210 s completed while
the target remained unseen.

The captured Maneuver Control records reproduce the failure:

- sequence 56 called `read_file` twice;
- sequence 57 called `read_file` once;
- sequence 58 called only `ManeuverHeartbeatResponse`;
- none called `pursue`.

The durable command ledger has navigation to the 210 s fix followed by
navigation to the newer 240 s fix. It has no command between them. There is no
rejected pursuit receipt or failed pursuit feedback, so this was a missing tool
submission rather than a transport or Physical Runtime failure.

## Isolated model replays

`var/full-mission-reasoning/replay_pursuit_submission.py` replaces only the
captured decision-cycle skill result with current guidance, calls the configured
vLLM, and records public tool calls. It never executes a returned tool or connects
to Physical Runtime.

| Captured case | Seed | Selected tool | Entity ID | Tool count | Latency |
| --- | ---: | --- | ---: | ---: | ---: |
| assignment handoff at 19.5 s, sequence 13 | 0 | `pursue` | 4 | 1 | 61.46 s |
| assignment handoff at 19.5 s, sequence 13 | 1 | `pursue` | 4 | 1 | 16.23 s |
| recovery arrival at 239.5 s, sequence 58 | 0 | `pursue` | 4 | 1 | 79.30 s |
| recovery arrival at 239.5 s, sequence 58 | 1 | `pursue` | 4 | 1 | 72.92 s |
| recovery arrival at 239.5 s, sequence 58 | 2 | `pursue` | 4 | 1 | 35.57 s |

All five valid samples succeeded. No failed model sample is omitted. A proposed
sequence-30 replay was rejected before calling the model because its accumulated
request did not contain exactly one replaceable decision-cycle result; it is not
counted as a model sample or action result. Receipts are under
`var/mission1-harbor-single-suspect/in-map-control-ship4/issue60-replays/` and
`live-run8dAH8Z-final/submission-replay-seed0.json`.

These samples validate the updated model guidance across two captured contexts,
but they do not prove physical submission. That evidence comes from the live
runs below.

## Code-owned command boundary

Agent `430f15b` already moved routine acquisition and recovery onto the real
Maneuver tool boundary:

- a current sighting or completed acquisition navigation calls `pursue` and
  accepts only `queued` or `already_queued`;
- a missed acquisition with a newer same-entity public GPS fix calls `navigate`,
  reports the recovery to Hyper, and records the submitted fix;
- a repeated heartbeat preserves the already-submitted recovery instead of
  duplicating it;
- without a usable newer fix, it reports the miss once for Hyper evaluation,
  retains local search, and issues no replacement physical command.

The implementation uses the configured GPS interval from the environment. It
does not treat the appearance of an old fix in a fresh snapshot as a new sample.
Public positions remain search hints and never become event checks or visibility
proof.

Focused verification on the current tree passed:

```text
pytest -q tests/test_tool_driven_maneuver.py \
  tests/test_live_pursuit_acquisition.py tests/test_maneuver_wakeups.py \
  tests/test_role_context.py
106 passed, 7 deselected in 5.37s
```

These tests exercise the real command dispatcher, numeric entity preservation,
navigation-to-pursuit handoff, bounded recovery, wakeups, and guidance loading.
No new command or evidence contract was introduced for this ticket.

## Completed live evidence

The labelled truth-prior run `run.yddLBS` and intermediate run `run.sk18ab`
both used Agent `430f15b`. Their durable command ledgers begin with acquisition
navigation and numeric `pursue(entity_id=4)`, then contain the following repeated
recovery sequence without duplicate commands:

1. navigate to the ship-4 GPS fix sampled at 30 s, then pursue ship 4;
2. navigate to the ship-4 GPS fix sampled at 90 s, then pursue ship 4;
3. navigate to the ship-4 GPS fix sampled at 180 s, then pursue ship 4.

In `run.yddLBS`, pursuit became physically active at 11.0, 37.0, 115.5, and
195.5 s. Progressing navigation was preserved until arrival; a current or
arrived target then caused a real pursuit command. At the first missed
acquisition bound, no newer fix was usable, so Hyper evaluated the miss without
replacing the still-active local search. The next published same-entity fix
enabled recovery.

Both completed-run audits prove unique report checks, belief-count parity with
the sensor ledger, exact MiniZinc/oracle parity, no transition timing errors,
Mission-time freeze during inference, and no failed physical feedback. The
truth run finished at 10/11 recall; the 0.25 run finished at 5/11 after a later
planner-selected fixed-view replacement. Full receipts are referenced from
`docs/mission1-prior-live-control.md`.

This is actual command and lifecycle evidence. It does not infer submission from
agent prose and does not use a command-injection shortcut.

## Equal-prior live/native gap

The standalone native equal-prior control found 6/11 issues. Its two detections
missing from old live `run.8dAH8Z` were the altered ship-4 reports at 238 and
253 s.

At 238.0 s, the old live drone was still navigating to the stale 210 s public
fix, 22.95 m short of that fix. Ship 4 was absent from the current heightmap-
occluded camera FoV and no check was created. Evaluator-only coordinates put the
ship within the nominal 300 m radius, so distance alone does not explain this
miss; current evidence does not further separate camera heading from terrain
occlusion. The missing pursuit submission happened only after arrival at
239.5 s and therefore did not cause the earlier 238 s miss.

At 240.5 s the old run navigated toward the newer 240 s fix. At 249.5 and
250.0 s, planner revisions 3 and 4 replaced that recovery with fixed-view
navigation. At the 253 s altered event the ship was approximately 351 m from
the drone, outside the configured radius, while the drone was executing the
fixed-view route. At 299 s the selected view saw ship 13 rather than ship 4, so
the late ship-4 cluster was also missed. Those misses are planner/coverage and
sensor-state outcomes, not command transport failures.

With current recovery code, the truth-prior live run detected the 238 s and
253 s altered events and all later corrupted events, closing its live/native
gap at 10/11. The 0.25 live run reproduced the native 5/11 result because its
evidence-driven replan deliberately left pursuit at 268.5 s. Remaining recall
optimization therefore belongs to sensor-faithful route coverage rather than a
new pursuit command lifecycle.
