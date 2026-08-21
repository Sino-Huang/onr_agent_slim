---
name: hyper-coordination
description: Use when Maneuver needs a replan evaluation, a planning-authority answer, or a correlated report to Hyper while preserving the active Mission objectives and plan authority.
version: '1.1.0'
---

# Hyper Coordination

## Communication Contract

- Send every replan request, Hyper query, and report through the correlated `CommunicationPort` envelope.
- Preserve the exact Mission ID, active plan revision, source revisions, correlation ID, sender, recipient, and message kind required by the envelope. Do not correlate by prose.
- Keep messages factual and bounded to Mission Snapshot, FSM Status, normalized lifecycle feedback, and the current execution constraint.
- Send environment lifecycle facts through their normalized Context Coordination path. Report their planning implications to Hyper only with the same Mission and action correlation.

## Replan Requests

Request a replan evaluation when current authoritative evidence makes the active plan blocked, infeasible, unsafe under its constraints, or materially stale.

1. State the concrete reason and relevant evidence.
2. Copy the observed plan revision and authoritative source revisions exactly.
3. Identify the affected maneuver or constraint without proposing new Mission objectives.
4. Continue to respect the active plan until Hyper returns a new plan reference and the runtime makes that revision current.

A Replan Request is advisory. Maneuver must not rewrite, broaden, or replace objectives, emit a plan revision, or force Hyper to replan. Replanning never automatically cancels a submitted physical action.

## Queries and Reports

- Ask Hyper for planning-authority information with `communicate(recipient="hyper-agent", kind="query", message=...)`.
- Use a report for execution facts or constraints Hyper should evaluate; do not present an opinion or prediction as environment feedback.
- Hyper may answer queries, issue replans, and send reports.
- Only Hyper may emit the reserved `human-question` message. Maneuver must never construct, relay as its own, or answer one from hidden knowledge.
- V1 has no live human-in-the-loop interaction. Do not wait for or initiate a direct human conversation.

## Handling Hyper Responses

For a replan request, accept only these response classes:

- a new plan reference;
- `no_change`; or
- `decline`.

The communication tool correlates the response to the original envelope. A new plan reference is not permission to edit the plan locally; use it only after the runtime supplies the corresponding authoritative plan/Statechart revision. On `no_change` or `decline`, retain current authority and choose the next bounded Maneuver response from live FSM and environment state.

## Gotchas

- A Hyper answer can resolve a query without changing the plan.
- Silence, timeout, or an uncorrelated message is not `no_change` or `decline`.
- Do not bypass Hyper by turning a query or report into a local objective change.
