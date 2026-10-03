# Runtime Host launches a per-run Environment Stack

Status: accepted

Supersedes the environment-lifecycle part of ADR 0001: the Runtime Host now launches and stops the local environment processes a Mission Run needs, instead of assuming a separately started environment.

Each Mission Run gets its own Environment Stack, resolved from a Stack Preset plus operator toggles (AirSim engine, perception mode, Environment Update Ownership, simulation limit). A Stack Supervisor inside the Run Worker starts the services in order (AirSim/Harbor engine, perception, physical runtime with its world-model viewer, Mission 4 worker), waits for each readiness signal, runs the closed loop against a run-local Runtime Configuration, and tears the services down in reverse order. Services normally inherit the Run Worker's process group. The engine deliberately starts Harbor in a separate session; cancellation and Host-Interrupted Failure recovery also reap detached processes carrying the exact inherited run-ownership token. The detached restoration guardian is exempt so it can restore Harbor configuration after forced teardown.

The Run Worker is its own session leader and exports a per-run ownership token that every service inherits. The Host persists the worker's PID, process group, session, process start time and token. The worker can outlive an unexpectedly killed Host, so a restarted Host must prove ownership without a child handle: the leader PID must still have the recorded start time, or a live member of the recorded group and session must carry the exact token. A detached session is owned only if it carries the token. An unproven or reused PID is never signalled. Teardown sends SIGTERM to the worker so its supervisor stops services in reverse order, then SIGTERM and finally SIGKILL to the owned group and owned detached groups. Zombie (`Z`) or dead (`X`) processes do not count as live because a reconstructed Host cannot reap a former child's zombies, and they hold no service. If procfs cannot be read, the group is treated as live. The restoration guardian (identified by `--guard-parent`) is excluded from both reaping and the exit wait, so it can restore Harbor's `environment.json` after forced teardown.

The stack is per run, not per Host session, because the physical runtime is scoped to one Mission id, the recorded AirSim scene clock is single-use, and the engine RPC (41451) and perception (8766) ports are fixed. The Host already allows one active Mission Run, so at most one stack exists at a time. The Runtime Host is the only authority for Mission ids: the Stack Builder passes the Host's `mission-<uuid>` to every service and writes run-local copies of fixtures that embed a demo Mission id.

The kernel-allocated world-model viewer port excludes the catalog's engine and perception ports. An explicitly requested viewer port that collides with an enabled service is rejected by preflight before any process starts.

All files for one Mission Run live under one run root, `var/runtime-host/runs/<mission_run_id>/`, with the same layout the herdr live-demo launcher uses, so the live-demo audit works on Host runs unchanged. The herdr launcher is a thin wrapper over the same Stack Builder, so command composition has one source of truth.

The Host retains cached evidence, projections and world frames for the current run, pending terminal narrative attempts, and at most one requested historical run. Projection state includes the per-run debug-artifact catalog, planner inventory and folded environment evidence. Older terminal views reload from the durable Run Root; they never fetch a frame from a viewer port reused by a later run.

## Consequences

- The Host owns only the lifecycle of processes it launched. Authority over environment state, Maneuver Feedback and world-model truth stays with the environment; cancellation still never retracts a submitted Maneuver Command.
- Mission Run Cancellation now stops the run's Environment Stack. An environment started outside the Host is not touched.
- Preflight Checks report missing executables, busy fixed ports, an active Mission Run and unreachable vLLM before activation; a stack readiness failure ends the run as `failed` with terminal classification `stack_failed`.
- Service logs under the run root are run-scoped Artifacts with classification `service_log`, visible through the loopback Operator Debug View only.
- On AirSim runs the Run Worker also owns a read-only camera capture. It is not a stack service and has no control authority: it only reads images, and it stops before the final world frame and stack teardown.
- The Operator Console remains a pure HTTP client of the Runtime Host; it never starts environment processes itself.
