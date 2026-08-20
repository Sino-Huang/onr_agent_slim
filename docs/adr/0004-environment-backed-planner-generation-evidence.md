# ADR 0004: Publish environment-backed planner generation evidence

- Status: Accepted
- Date: 2026-08-20

Issue #38 activates the provenance boundary that ADR 0003 deliberately deferred for issue #37: a planner-native Mission Run publishes an immutable Planner Choice Record and accepted or rejected Planner Generation Attempts. Each attempt binds raw Mission Input and accepted PlanningIntent provenance, public rationale, translator identity, generated asset references and hashes, and the Mission Snapshot used for generation; private model reasoning is excluded.

The environment publishes flexible environment data and its content digest before generation, Context Coordination places its reference in the Mission Snapshot, and Hyper accepts an attempt only through that environment-backed planning heartbeat.
