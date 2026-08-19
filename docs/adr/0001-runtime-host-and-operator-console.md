# Runtime Host and Operator Console Boundary

Status: accepted

The system will use a Rust Ratatui Operator Console as a peer client of a separate loopback Python Runtime Host, rather than embedding runtime authority in the console or extending the read-only viewer server. The Runtime Host owns durable Mission Run Records, one active Mission Run, idempotent activation and cancellation, and isolated Run Worker process trees; the console owns only its authenticated Console Session and may recover it after an ungraceful loss. This preserves a redacted shared observation surface, gives future artifact producers and Human Decision Requests a stable HTTP+JSON boundary, and permits hard cancellation of local agent work without claiming authority over the external environment.

## Consequences

- The existing viewer remains read-only and shares TraceProjection output with the Runtime Host.
- Run Activities are deterministic projections of redacted observations; LLM-produced Run Narratives are optional and non-authoritative.
- The console uses owner-scoped Mission Intent readback, while generic observations never expose raw authority input or private reasoning.
- Future HITL uses durable checkpoints and schema-driven decisions over the Runtime Host; the initial console reserves this state without implementing decision submission.
