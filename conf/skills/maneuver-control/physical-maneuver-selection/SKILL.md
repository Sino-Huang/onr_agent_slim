---
name: physical-maneuver-selection
description: Use when selecting or preserving a physical maneuver, including pursuit acquisition and recovery.
version: '1.4.0'
---

# Physical maneuver selection

Choose only the typed tools `navigate`, `takeoff`, `land`, `search_area`,
`pursue`, and `investigate`. Copy targets from current FSM/environment facts and
keep the active Mission/plan identity. Express intent through tool parameters;
environment feedback alone confirms accepted, active, completed, failed, or
cancelled lifecycle.

- Preserve a suitable nonterminal action. Every new physical call overrides it.
- A completed navigation that established a fixed viewpoint remains suitable
  while its observation/time gate is pending.
- For deadline navigation, normally omit `speed` so the environment uses its
  configured capability. Arrive early and wait. Use the drone's operating
  altitude when a target/report location supplies no valid flight altitude.
- `queued` and `already_queued` prove submission only. Missing feedback is an
  unknown lifecycle, not failure. Keep stable action identity for retries.
- In Mission 2, retain the authorized pair/target/source/run/forecast/window. A
  risk pair alone proves neither visibility nor collision. Ask Hyper to reconsider
  expired or unreachable assignments.

For `fixed_view`, navigate to the selected location. For `pursue_ship`, apply
the acquisition reference below while retaining the same FSM assignment and
numeric target ID:

[Pursuit acquisition and recovery](references/pursuit-acquisition.md)

An unseen target away from its rendezvous needs navigation first. Arrival or an
exact current target sighting requires pursuit in that heartbeat. An acquisition/search deadline can require GPS
recovery or Hyper escalation even while the assignment's final evidence gate is
future. Replacing an active recovery navigation every heartbeat defeats recovery.

For Mission 3, target only `world_model_info.mission3.selected_ship_ids` and use
its current public target estimate. Obtain a screening view with `navigate` or
`pursue`; use `investigate` when usable evidence is suspicious or inconclusive.
Preserve an active action until new evidence, meaningful target movement or
terminal feedback changes it. Either sufficient verdict permits immediate
replacement, which cancels an active orbit through the normal lifecycle. Orbit
completion, no detection, unavailable perception and absent confidence leave the
ship unresolved. After a failed or inconclusive attempt, screen other feasible
ships before a bounded revisit; report the reason when no recovery evidence arrives.
