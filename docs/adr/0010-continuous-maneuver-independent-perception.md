# Continuous maneuver-independent perception

Perception runs continuously on each environment tick and is independent of physical maneuver lifecycles, so maneuvers no longer open sensing windows or own observation outcomes. The environment publishes uncertain Event Observations without updating Bayesian belief directly; ingesting pending perceptions remains a separate Maneuver Control decision so sensing evidence and belief authority stay independently auditable.
