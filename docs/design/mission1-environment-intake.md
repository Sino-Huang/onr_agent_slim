# Mission 1 environment intake

The physical runtime publishes one immutable `environment-update` envelope for
every completed MultiGrid step. The envelope references an `environment_data`
event whose `world_model_info` is the JSON-safe `info[0]` captured at
`observation_time_seconds`. `TransportBackedEnvironmentUpdateSource` resolves
every referenced event and retains every envelope as an ordered
`EnvironmentTickResult`.

Context Coordination drains the complete buffered batch. Before it reduces the
context stream to the latest Mission Snapshot, it passes every tick in order to
the Mission 1 reporting-reliability service. The service reads only
`environment_data.world_model_info.event_report_checks`, processes every unseen
opaque `check_id` once, checkpoints, and publishes the resulting snapshot
through the existing `bayesian_belief_snapshot` source. Replayed envelopes,
cumulative ledgers, and restarts therefore do not repeat an update. All numeric
`entity_id` values stay numeric through this path.

The evidence views have separate authority:

- Live `world_model_info.ship_event_reports` is cumulative only through the
  observation time. `event_report_checks` and `detected_issues` are cumulative,
  visibility-gated ledgers.
- A planning view adds `static_info`, the complete future public report
  schedule, while retaining the latest live world-model fields. Public reports
  have opaque `report_id` values and no private `source_event_index`.
- Event Observations travel as separate perception events and describe actual
  actions. They are available to Maneuver Control but are not corruption
  outcomes and do not update reporting reliability.

Every `ManeuverInvocation` receives the latest live environment plus the entire
pending perception batch. Every accepted Mission 1 gate evaluation constructs
the `HyperHeartbeatInvocation` from the latest Mission Snapshot and reliability
artifact; a replan workflow then asks the environment for a fresh planning view
so Hyper also receives the complete future schedule.
