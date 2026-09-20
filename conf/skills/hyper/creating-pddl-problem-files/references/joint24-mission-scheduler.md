# Joint24 (Mission 2 + Mission 4) mission scheduling

Use the current authorized environment file's `world_model_info` when
`mission_mode` is `joint24`. One drone serves Mission 2 collision-risk
monitoring and Mission 4 worker-requested search in the same run; the PDDL top
level decides only which mission block executes next. The Mission 2 and
Mission 4 middle tiers keep their own within-mission authority.

1. The Planner Choice for `joint24` is `symbolic` / `fast-downward`. The
   mission scheduler is a small STRIPS + action-costs problem, not a temporal
   model.
2. The workflow pre-materializes `domain.pddl`, `problem.pddl`, and
   `joint24-schedule.json` into the shell workspace from the two middle tiers'
   current outputs (Mission 2 selected candidate travel/monitor evidence,
   Mission 4 pending-request deadline and next-decision travel evidence).
   Submit the pre-materialized `domain.pddl` and `problem.pddl` exactly as
   written through the generic Fast Downward submission and external
   verification route; keep their returned paths and contents unchanged. Never
   hand-invent or adjust the numeric constants: urgency enters only through the
   code-owned defer-cost class, never through edited travel or service values.
3. The checked-in scheduler domain lives at
   `examples/joint24-scheduler/domain.pddl` in this skill. Serving a mission
   moves the drone to that mission's block site and charges travel plus
   service time; deferring a mission skips its low-urgency block at the
   revision's defer price, which is what makes the ordering urgency-sensitive.
4. After `planner_executor` returns the VAL-validated plan, write the returned
   plan text to `<shell-workspace>/sas_plan` and run
   `python -m onr.application.joint24_planning --emit-statechart --plan
   <shell-workspace>/sas_plan --schedule <shell-workspace>/joint24-schedule.json
   --output <statechart_file_location shell path>` from the repository root.
   The emitted Statechart reuses the code-owned Mission 2 and Mission 4 block
   shapes; a mission deferred in the validated plan has no block this
   revision. Submit the emitted `statechart.json` unchanged.
5. The mission order in the accepted Statechart follows the validated plan.
   Keep Mission 2 collision-monitoring evidence and Mission 4 search-answer
   evidence separate; the run terminates at `mission_end_time_s`, not at the
   first completed block.

The scheduler decides ordering only. Hyper owns plan revisions; each mission's
gate and middle tier owns within-mission decisions; Maneuver Control owns
physical action selection and bounded preemption at maneuver boundaries.
