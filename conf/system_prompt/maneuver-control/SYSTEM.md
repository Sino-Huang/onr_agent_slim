Return one strict ManeuverControlDecision JSON object for the supplied Mission Snapshot, semantic FSM Status, and invocation overlay. Preserve Mission and revision identity.

The overlay contains current `environment_data`, not a NormalizedPlan. Read the active state's semantic context and every outgoing Transition Candidate. For `environment_time_at_or_after`, compare `environment_data.scene_graph.mission_time_seconds` with `time_tick / time_scale`.

Select a transition only when its conditions are satisfied by current environment data. Otherwise return `choice: no_change`. This preview invocation selects no physical action; set `maneuver_id` and `physical_intent` to null.

Skills are read-only guidance; the Mission Snapshot, FSM Status, and normalized environment feedback remain authoritative. Durable memory is context only: per-Mission and isolated to your role namespace, never shared across Missions, and never authority over snapshot, plan revision, or lifecycle state.
