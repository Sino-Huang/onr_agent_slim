---
name: detect-and-replan
description: Apply when observed evidence affects plan feasibility or a Mission 1 utility gate identifies a better route, requiring a replan decision.
version: '1.2.2'
---

# Detect And Replan

## Procedure

1. Correlate the evidence to the active Mission and plan, then inspect authoritative source health, revisions, and freshness.
2. Compare the evidence with Mission Input and PlanningIntent provenance, the current plan reference, scene/belief, lifecycle, and FSM execution artifacts.
3. Identify a concrete feasibility problem or evidence-driven utility gain. A changed belief can justify replanning while the current route remains executable; compare the gate's remaining-route scores and current constraints.
4. Preserve Mission Input authority and the current plan while replanning is evaluated.
5. Accept a replacement only after a validated durable replan result is published through the authoritative path; that result then supersedes the prior plan according to its revision contract.
6. Communicate and record the outcome as a new plan reference, no-change result, or decline with the relevant request and observed artifact references.

## Mission 1 reliability replans

Treat a Mission 1 gate trigger as advisory evidence for the heartbeat decision.
For a fresh `score_improvement` trigger (at least 10% combined utility), evaluate
the proposed gain and any concrete ongoing-observation cost. Request `replan`
when that gain remains justified; the next workflow verifies the replacement.
The absence of an already-validated replacement is expected at this decision
stage. An executable current route alone is not a reason to reject the gain.
Use `no_change` when specific evidence defeats it, recording that tradeoff.

For example, altered checks can raise one vessel's posterior corruption risk
while future reports remain dense. A gate-supported utility gain can justify
replanning toward pursuit even though the existing fixed-view route is still
feasible. The belief manager infers risk; MiniZinc selects the replacement mode
and target. The ship posterior is corruption risk, distinct from the shared
omission probability.

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

An active pursuit's last unchecked report is continuation work, not a new
two-report pursuit candidate. Evaluate its remaining observation window and
the gate's score comparison before choosing a replacement. A periodic GPS fix
describes the ship at its sample time; compare it with the public schedule at
that time, not with a later rendezvous coordinate as though the ship were static.

An accepted replacement starts its new Statechart and may preempt the current
assignment through Maneuver Control. Describe that tradeoff explicitly when
requesting a replan. Claim that a pursuit is preserved only if the accepted
replacement actually retains its target and remaining window; choose
`no_change` when the intended decision is to continue the current plan.

## Authority Boundaries

- A replan request is advisory and is not a new plan or authority revision.
- Never perform a hidden local rewrite of Mission Input, PlanningIntent, the plan, scene/belief, lifecycle, or FSM execution state.
- Replanning does not automatically cancel physical execution; cancellation must occur through the established control and lifecycle path.

## Gotchas

- Reject or defer stale, uncorrelated, or wrong-Mission evidence rather than treating it as a blocker.
- A source revision change alone is insufficient unless its content affects execution.
- Planner failure is evidence to evaluate, not permission to alter the objective or authoritative revisions.
- Record no-change and decline outcomes so a request is not mistaken for an accepted replacement plan.
