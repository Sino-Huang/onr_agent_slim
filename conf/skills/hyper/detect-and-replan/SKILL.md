---
name: detect-and-replan
description: Apply when observed execution facts may invalidate the current plan and an evidence-based replan decision is required.
version: '1.2.0'
---

# Detect And Replan

## Procedure

1. Correlate the evidence to the active Mission and plan, then inspect authoritative source health, revisions, and freshness.
2. Compare the evidence with Mission Input and PlanningIntent provenance, the current plan reference, scene/belief, lifecycle, and FSM execution artifacts.
3. Identify a concrete blocker or inconsistency. Replan only when a meaningful mission, plan, environment, or source-constraint change affects feasibility or execution.
4. Preserve Mission Input authority and the current plan while replanning is evaluated.
5. Accept a replacement only after a validated durable replan result is published through the authoritative path; that result then supersedes the prior plan according to its revision contract.
6. Communicate and record the outcome as a new plan reference, no-change result, or decline with the relevant request and observed artifact references.

## Mission 1 reliability replans

Treat a Mission 1 gate trigger as advisory evidence for the heartbeat decision.
When the disposition is `replan`, the replacement Hyper Workflow runs all
planning stages against the latest Mission Snapshot, physical planning view,
and reporting-reliability snapshot. A snapshot that already names an active
positive plan revision requires fresh planner files and a fresh Statechart in
the next revision workspace; the prior revision remains authoritative until
that replacement passes MiniZinc and Statechart validation.

Use the current Mission time, vehicle pose, cumulative `event_report_checks`,
future public schedule, and posterior revision. The replacement DZN therefore
removes expired and checked opportunities and recomputes reachability and
utility. The flow formulation may remain unchanged, but `model.mzn`,
`generate_data.py`, `belief.json`, and `data.dzn` are newly written artifacts
for the replacement revision.

## Authority Boundaries

- A replan request is advisory and is not a new plan or authority revision.
- Never perform a hidden local rewrite of Mission Input, PlanningIntent, the plan, scene/belief, lifecycle, or FSM execution state.
- Replanning does not automatically cancel physical execution; cancellation must occur through the established control and lifecycle path.

## Gotchas

- Reject or defer stale, uncorrelated, or wrong-Mission evidence rather than treating it as a blocker.
- A source revision change alone is insufficient unless its content affects execution.
- Planner failure is evidence to evaluate, not permission to alter the objective or authoritative revisions.
- Record no-change and decline outcomes so a request is not mistaken for an accepted replacement plan.
