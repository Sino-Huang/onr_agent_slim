---
name: detect-and-replan
description: Apply when evidence affects plan feasibility, a Mission 1 utility gate identifies a better route, Mission 2 collision risks change, Mission 3 inspection evidence changes, or Mission 4 search objectives/evidence change.
version: '1.6.0'
---

# Detect And Replan

For Mission 2 or joint missions, assess the current
`world_model_info.perception_predictions` and any `mission2-gate:*` trigger.
New warnings, changed/cleared risks and expired forecasts can warrant a new
observation plan independently of Mission 1's score gate. Read the Mission 2
reference in `creating-minizinc-problem-files` for candidates and separate joint
mission priorities. Correlate alerts by run and event ID; a repeated ledger is
not a new incident. A short observation's completion returns to monitoring;
whole-mission completion follows Mission Intent and the recorded mission end.

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

A missed pursuit-acquisition bound is a valid escalation, but it is not by
itself evidence that replanning can improve the route. When the same target is
already under active pursuit/local search and no newer usable target position
or other route-changing evidence exists, choose `no_change` and preserve the
search. A new solve would reuse the same stale rendezvous while consuming the
remaining observation window. Choose `replan` only when fresh evidence can
change the target, route, mode, or feasibility. Record either disposition as the
correlated evaluation of the Maneuver request.

A newer target fix may support Maneuver's bounded recovery navigation without a
replacement plan. When that recovery is already active or submitted for the
same target and the current assignment, mode, and remaining window stay
feasible, choose `no_change`. Replan only when the new evidence requires
planning authority to alter the assignment or its feasibility.

An accepted replacement starts its new Statechart and may preempt the current
assignment through Maneuver Control. Describe that tradeoff explicitly when
requesting a replan. Claim that a pursuit is preserved only if the accepted
replacement actually retains its target and remaining window; choose
`no_change` when the intended decision is to continue the current plan.

## Authority Boundaries

- A replan request is advisory and is not a new plan or authority revision.
- Never perform a hidden local rewrite of Mission Input, PlanningIntent, the plan, scene/belief, lifecycle, or FSM execution state.
- Replanning does not automatically cancel physical execution; cancellation must occur through the established control and lifecycle path.

## Mission 3 inspection replans

Treat `mission3-gate:*` as a public advisory. Replan when a sufficient verdict
removes a target, usable inconclusive/suspicious evidence adds investigation
service, a public target estimate moves materially, or terminal maneuver evidence
changes feasibility. A repeated snapshot produces no new action. Prefer the
nearest feasible unresolved target, interleave investigation during the same
visit, and bound revisits after failed or full-orbit-inconclusive attempts. Keep
unresolved ships and their reasons in the final report at the recording/budget
limit. Use the source-supplied sufficiency flag identically for live and simulated
evidence; producer access and camera control stay outside Agent planning.
When `all_resolved` arrives before the recording boundary and the active
Statechart is nonterminal, choose `replan`: the replacement code-owned policy
emits the evidence-backed terminal report chart. Do not choose `no_change`
merely because the superseded chart could wait until the recording deadline.

## Mission 4 search replans

Treat `mission4-gate:*` as public advisory evidence. Replan when an accepted
worker revision changes the active objectives, a maneuver completes or fails,
coverage changes, or new observations change accumulated match uncertainty.
Preserve unaffected objectives, evidence identities, elapsed Mission time and
feasible plan segments. Seek another useful view while a required attribute or
location remains unresolved, and use `investigate` as the close-up
identification action when a track's position is precise but its attributes
remain unresolved. An `all_found` report requires supported locations inside
the requested areas and the configured strict belief threshold; for animal-type
questions the species posterior must also be resolved. Runtime receipts remain
the authority for request and physical lifecycle changes.
When an accepted request revision adds or changes an objective and the current
Statechart is monitoring with no still-useful active physical leg, choose
`replan` so the replacement chart schedules the code-owned adaptive decision.
Do not choose `no_change` merely because both objectives share an area.

## Gotchas

- Reject or defer stale, uncorrelated, or wrong-Mission evidence rather than treating it as a blocker.
- A source revision change alone is insufficient unless its content affects execution.
- Planner failure is evidence to evaluate, not permission to alter the objective or authoritative revisions.
- Record no-change and decline outcomes so a request is not mistaken for an accepted replacement plan.
