# Agent-owned Transition Intent and immediate replan reconciliation

Status: accepted; extends ADR 0009. Implementation tracked by GitHub issue #50.

Maneuver Control owns semantic condition assessment by first persisting a Transition Intent for one exact current Statechart target and later consuming that intent to authorize the FSM Runner's internal event; code validates identity, revision, and topology but does not reinterpret timing, observation counts, occlusion, or other condition semantics. Maneuver receives only current-state operational context and target conditions, while target-state operational context remains unavailable until transition. Context Coordination invalidates stale intent and invokes Maneuver at the same Mission time immediately after replacement Statechart activation, leaving physical action cancellation or replacement to Maneuver judgment.
