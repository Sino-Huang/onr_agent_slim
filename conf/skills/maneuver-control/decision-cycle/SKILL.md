---
name: decision-cycle
description: Use for Maneuver intent bootstrap, transition, retargeting, Event ingestion, and Hyper communication.
version: '2.2.2'
---

# Decision cycle

Current focused FSM, environment, lifecycle, and Hyper facts override planning
provenance or memory. Candidates describe desired outcomes; expected evidence is
uncertain rather than ground truth.

1. Inspect the injected intent before another target. If none is valid, select one
   exact candidate and inspect its result. Assess this bootstrap intent now. An
   intent selected after a transition or as a replacement waits for the next
   heartbeat.
2. Check exact time readiness before evidence confidence. Retain any intent whose
   not-before or observation-window end is future. Once time-ready, missing or
   occluded evidence may support `satisfied_with_uncertainty` only when required
   physical coverage was achieved; name every unconfirmed report ID.
3. For a satisfied condition, call `transition_fsm` once with exact current/next
   IDs, assessment, evidence, and uncertainty. Inspect its returned context and
   select one target for the new state unless terminal/candidate-free. Never
   transition twice or assess that new target in the same heartbeat.
4. For an unsatisfied condition, retain the intent normally. If it is unsuitable,
   select one exact replacement and defer assessment.
5. Preserve a suitable active physical action. A new physical call overrides it.
   Inspect submission results before completion; submission is not lifecycle
   confirmation. A completed fixed-view navigation remains suitable while its
   gate is pending. For pursuit acquisition or continuity, apply the supplied
   physical-maneuver-selection guidance and acquisition reference.
6. If `pending_event_perception_count` is positive, call
   `ingest_perceptions` exactly once. Entity sightings need no ingestion. Send
   evidence-driven communications independently. For `hyper_evaluation`, copy
   its exact kind, reason, evaluation ID, and delivery policy.
   If `pre_ingested_event_perceptions` is present, ingestion already succeeded;
   use that result and do not call the tool again.
7. After all selected results arrive, call `ManeuverHeartbeatResponse` with a
   concise summary grounded in those results. State a submitted maneuver only
   when its tool returned submission evidence.

On a correction for missing post-transition target selection, use the latest
focused context and do not transition again. A queued communication is transport
evidence; its correlated Hyper decision arrives later in `hyper_outcomes`.

Reason briefly. Use exact derived facts directly, call the next required tool as
soon as the decision is clear, and complete after the selected results without
restating or re-analyzing settled evidence.
