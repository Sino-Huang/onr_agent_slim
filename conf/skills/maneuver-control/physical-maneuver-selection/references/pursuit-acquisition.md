# Pursuit acquisition and recovery

`pursue` uses partial observations. With no sighting or usable search prior,
it explores; it does not navigate to the planner's public rendezvous on its own.
Keep the numeric target ID and `pursue_ship` assignment throughout acquisition,
tracking, and recovery. Maneuver Control selects the existing physical tools.

## Acquire, then track

1. Check `world_model_info.visible_ship_ids` and current `visible_ships` for
   the exact target. A GPS fix or scheduled report position is a search hint,
   not proof of visibility. Preserve a suitable active pursuit when tracking.
2. If the target is unseen and the drone has not reached the rendezvous, issue
   `navigate` to `desired_outcome.acquisition_rendezvous.location`, with its
   `arrival_deadline.seconds` and the drone's current flight altitude. Begin
   travel as soon as this assignment is active, not at observation start.
   Preserve this navigation while it progresses; a current sighting permits
   switching to pursuit before arrival.
3. On navigation completion, issue `pursue` in that heartbeat, even if the
   target is still unseen: exploration now begins near its possible location.
   Arrival completes acquisition travel, not the assignment's report-evidence
   gate. Do not hold at the rendezvous for the entire observation window.
4. Retain pursuit during brief occlusion while tracking remains suitable.
   Anchor acquisition's search budget to the attempt's start, not the current
   heartbeat: its bound is the first required observation time after that
   start, or the next GPS broadcast after that start, whichever comes first
   (use the window end if neither is supplied). A passed bound stays passed.
   In particular, `search` begun before observation start, an elapsed
   observation start, and a missing first required report check mean failed
   acquisition now, even when the rest of the observation window is future.
   Reasoning wall time consumes none of this Mission-time budget.

## Recover with public evidence

When search has not acquired the target by that bound, or tracking is lost and
new position evidence materially changes the search area, inspect the newest
timestamped public GPS/position fix for that same entity in the supplied
environment. If it supports a reachable new acquisition location, navigate
there, then resume pursuit on arrival or sighting. State the source and sample
time in the navigation reflection/action identity so subsequent heartbeats can
distinguish a newer fix from one already tried. Keep a progressing recovery
navigation; replacing it every heartbeat defeats acquisition.
An unseen target's GPS fix is precisely a reason to navigate toward it; absence
from the current FoV is not a reason to preserve an already unsuccessful search.

In the simulated world-model feed, read `world_model_info.public_position_fixes`
by `entity_id`. Each fix carries `position: {x, y, z}`, `sampled_at_s`, and
`source: "gps"`. Use `gps_interval_seconds` and `next_gps_update_time_s` from
the same info object. Navigate at the drone's flight altitude, not the ship's
GPS altitude. Repeated payloads with the same sample time are the same fix.
For recovery after unsuccessful acquisition, prefer a fix sampled after that
acquisition/search attempt began. An older cached GPS fix is not a new update
merely because the latest environment payload is fresh; with no newer evidence,
escalate the missed deadline instead of restarting acquisition on that fix.

Use the actual supplied update interval; a 30-second GPS cadence is not a
universal constant. A profile without a public position provider may omit GPS.
With no usable newer fix, choose an escalation-only heartbeat: ask Hyper to
evaluate replanning and issue no physical command. Keep the Transition Intent
pending. Exhausting search requires recovery evaluation, not an unconditional
replacement action when no supported destination exists. The existing command
remains unchanged while awaiting new evidence or a revised plan; this response
does not cancel it or declare acquisition successful. Reassess when a fresh fix,
current sighting, or revised plan arrives. An active pursuit or
elapsed window is not evidence that the missing reports were checked. Current
visible sightings outrank GPS; GPS outranks an older location hint, but future
scheduled positions remain predictions for their stated time.

## Few-shot sequence (illustrative values)

- At t40, assignment `pursue_ship`, target 23, rendezvous (180, -20) by t52,
  drone (40, -20, -25), target unseen: `navigate(x=180, y=-20, z=-25,
  deadline_time=52)`. Preserve the same assignment and observation window.
- At t46, that navigation is completed at the rendezvous: `pursue(entity_id=23)`.
  If the target became visible at t44, switching to pursuit then is also valid.
- At t61, pursuit begun at t46 is still searching and the required t52 report
  was missed, although the window ends at t82. A newly supplied target-23 GPS
  fix sampled at t60 puts it at (150, 60): use
  that fix for feasible recovery navigation, and notify Hyper about the missed
  report. On arrival resume pursuit. Do not claim the GPS fix checks a report.
- With the same unsuccessful search but no newer position fix, request Hyper
  evaluation with no physical command and leave the evidence gate pending.
