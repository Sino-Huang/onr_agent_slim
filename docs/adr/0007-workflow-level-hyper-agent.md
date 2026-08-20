# Workflow-level Hyper Agent owns planning progress

Status: accepted

ADR 0003 modeled Hyper's model invocation as a one-shot `PlanningIntent` response. Hyper now uses one checkpointed Deep Agent thread for a Mission Run: `PlanningIntent` is an intermediate tool-validated artifact, Hyper alone owns the thread's live todos and workflow ordering, and code-owned planner, artifact, verification, and handoff capabilities are exposed as tools that return evidence without mutating those todos. This preserves one observable correction history across planning stages while keeping Mission authority and executable verification outside model assertions; a finite per-invocation recursion limit bounds debugging and runaway tool loops.
