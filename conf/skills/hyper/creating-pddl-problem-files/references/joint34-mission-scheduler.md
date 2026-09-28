# Joint34 (Mission 3 + Mission 4) mission scheduling

Use the current authorized environment file's `world_model_info` when
`mission_mode` is `joint34`. One drone serves Mission 3 gate/vessel inspection
and Mission 4 worker-requested search in the same run; the PDDL top level
decides only which mission block executes next. The Mission 3 and Mission 4
middle tiers keep their own within-mission authority.

1. The Planner Choice for `joint34` is `symbolic` / `fast-downward`. The
   mission scheduler is a small STRIPS + action-costs problem, not a temporal
   model.
2. The workflow pre-materializes `domain.pddl`, `problem.pddl`, and
   `joint34-schedule.json` into the shell workspace from the two middle tiers'
   current outputs (Mission 3 next-inspection decision travel/service evidence
   from its grounding CLI, Mission 4 pending-request deadline and
   next-decision travel evidence). Submit the pre-materialized `domain.pddl`
   and `problem.pddl` exactly as written through the generic Fast Downward
   submission and external verification route; keep their returned paths and
   contents unchanged. Never hand-invent or adjust the numeric constants:
   urgency enters only through the code-owned defer-cost class, never through
   edited travel or service values.
3. The checked-in scheduler domain lives at
   `examples/joint34-scheduler/domain.pddl` in this skill. Serving a mission
   moves the drone to that mission's block site and charges travel plus
   service time; deferring a mission skips its low-urgency block at the
   revision's defer price, which is what makes the ordering urgency-sensitive.
4. After `planner_executor` returns the VAL-validated plan, the workflow's
   code-owned Joint34 emitter reads that exact native plan and the schedule
   metadata captured during materialization, then writes the Statechart to the
   returned `statechart_file_location`. Submit that path directly; do not author
   or edit a generator or Statechart. Submission regenerates the chart from the
   same trusted inputs before validation. A mission deferred in the validated
   plan has no block in this revision.
5. A Mission 3 block serves one maneuver; an unresolved selected ship stays
   pending after that block completes. A Mission 3 gate decision that advances
   a ship to a new required stage (screening to investigation) is new pending
   work, not next-view progress: when it arrives after that mission's block
   already executed, replan the mission order so the new stage is served.
   Gate trigger identities are opaque dedup keys, never directives. A gate
   decision whose action differs from the in-flight maneuver's action is not
   being served by that maneuver: a screening approach never becomes an
   investigation on arrival — the investigation is a separate tracker-re-aimed
   maneuver that must supersede the stale transit.
6. A completed block maneuver never finishes a mission by itself. While the
   Mission 4 ledger status is still active, a mission4-gate trigger carries
   the middle tier's current required work (its digest is that decision), not
   a completion signal: replan so the schedule serves it. Only a gate
   decision with action `report` or the terminal ledger establishes
   completion; never infer it from maneuver lifecycle or per-observation
   uncertainties. While a block is active and no maneuver is in flight,
   Maneuver serves that mission's current gate decision as the physical
   action; a rejected block transition or a terminal leg is not idle time. A
   healthy in-flight maneuver is preserved to its boundary: a same-mission
   gate decision waits for that boundary unless it advances a stage
   (Mission 3 screening to investigation).
7. The mission order in the accepted Statechart follows the validated plan.
   Keep Mission 3 inspection-resolution evidence and Mission 4 search-answer
   evidence separate; the run terminates at `mission_end_time_s` (the Mission
   3 time budget), not at the first completed block.

The scheduler decides ordering only. Hyper owns plan revisions; each mission's
gate and middle tier owns within-mission decisions; Maneuver Control owns
physical action selection and bounded preemption at maneuver boundaries.
