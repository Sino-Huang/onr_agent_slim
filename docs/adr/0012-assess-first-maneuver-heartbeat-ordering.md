# Assess-first Maneuver heartbeat ordering

Status: accepted; extends ADR 0011. Implementation tracked by GitHub issue #51.

Each Maneuver Heartbeat assesses its injected Transition Intent before retargeting and may apply at most one FSM transition over its authoritative evidence snapshot. A target selected after transition or unsuitable-intent replacement is persisted for the next heartbeat rather than assessed against stale evidence; only a heartbeat that begins without a valid intent may bootstrap and assess one immediately. Python owns completion identity and records every returned heartbeat as completed, while durable tool records remain the authority for effects and provider or ordering failures are recorded as failed.
