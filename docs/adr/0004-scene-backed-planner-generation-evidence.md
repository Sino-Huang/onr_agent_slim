# ADR 0004: Publish scene-backed planner generation evidence

- Status: Accepted
- Date: 2026-08-20

Issue #38 activates the provenance boundary that ADR 0003 deliberately deferred for issue #37: a planner-native Mission Run publishes an immutable Planner Choice Record and accepted or rejected Planner Generation Attempts. Each attempt binds raw Mission Input and accepted PlanningIntent provenance, public rationale, translator identity, generated asset references and hashes, and the Mission Snapshot used for generation; private model reasoning is excluded.

The environment publishes an Operational Scene Graph and content digest before generation, Context Coordination places both in the Mission Snapshot, and Hyper accepts an attempt only through that scene-backed planning heartbeat.
