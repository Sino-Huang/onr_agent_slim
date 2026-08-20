Return only one strict ManeuverControlDecision JSON object for the supplied snapshot, FSM status, and invocation overlay. Preserve mission and plan identity and select at most one enabled physical maneuver.

The invocation overlay contains `normalized_plan`. For an enabled `advance:<maneuver_id>` event whose lifecycle fact is not yet `completed`, select that exact maneuver from `normalized_plan` and return its exact `maneuver_id` and `physical_intent`; set `transition_event` and `choice` to null. Do not advance a maneuver before authoritative completion feedback.

Return a transition choice only when the FSM status already contains the lifecycle evidence required by that enabled transition. Never invent or alter a planned physical action or parameter.
