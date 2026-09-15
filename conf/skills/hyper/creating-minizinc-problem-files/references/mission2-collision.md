# Mission 2 ship observation planning

Use the current authorized environment file's `world_model_info` when
`mission_mode` is `mission2` or `joint`. `perception_predictions` v1 contains
active ship pairs, per-ship forecasts, readiness, timestamps and a latched alert
ledger. Source `simulated` has unavailable probability (`null`); source `sukai`
retains producer beliefs and calibration status. Both drive the same observation
planning procedure. Warning predictions are distinct from confirmed events.

1. Run `python -m onr.application.mission2_planning <environment-file>
   --output <shell-workspace>/mission2-candidates.json --model
   <shell-workspace>/model.mzn --data <shell-workspace>/data.dzn` using authorized execute
   paths. In joint mode choose `--joint-priority balanced|mission1|mission2`
   from Mission Intent; balanced is the default when no preference is specified.
   Inspect its compact manifest and candidate file. The candidate builder uses
   public forecasts, aircraft speed and estimated standoff travel time; terrain,
   line of sight and heading still require physical execution evidence.
2. Model one drone choosing feasible observation assignments within the current
   forecast and mission horizon. Prioritize impending risks and stale vessel
   measurements using each candidate's contact time, sample time, arrival window
   and travel duration. Probability `null` means unavailable, not zero risk or
   certainty. Keep source/run/sequence and pair/target identities in planner
   output. Keep desired observation windows separate from travel.
3. Submit the code-owned model and data from step 1 through the generic MiniZinc
   submission and external verification route; keep their returned paths and
   contents unchanged. Mission 2-only planning needs no
   reporting-reliability belief file or Mission 1 event materialization. When
   forecasts are unready/stale or no feasible risk assignment exists, plan
   continued monitoring until the next GPS/forecast update rather than claiming
   that the mission has finished or teleporting to an unreachable pair. Use
   The code-owned model supports an empty candidate array and emits a monitoring
   result through the next evidence time.
4. In joint mode, retain Mission 1 report candidates and Mission 2 risk
   candidates with separate utilities and scores. Honor the configured priority.
   Balanced scheduling alternates feasible mission opportunities instead of
   repeatedly starving one; `joint_observation_priority` in the same module
   provides that advisory rule. Actual priorities remain Mission Intent and
   planner decisions, not amendments to public observations.
5. Submit and execute with the existing external planner tools. Preserve the
   solver-native artifact for Statechart authoring. The accepted Statechart
   keeps monitoring/replanning alive until Mission Intent's completion condition
   or `mission_end_time_s`; completing one short observation is not completion
   of a whole collision-monitoring mission. `monitor_until_s` is the next
   evidence/replan cue. The monitoring state's terminal transition remains
   gated by `mission_end_time_s`, never by `monitor_until_s`.

The candidate helper produces planning inputs only. Hyper owns planner choice
and plan revisions; Maneuver Control owns physical action selection. Replanning
uses new/changed risks, new warning IDs and stale/cleared forecasts. Every
repeated copy of an alert has the same identity and is not another incident.
