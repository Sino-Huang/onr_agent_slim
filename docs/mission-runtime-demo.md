# Mission runtime demo

This command runs one configured mission through the real ONR runtime and keeps
the operator console as a separate read-only process. The external maneuver
environment is the installed `onr.demo.fake_environment.FakeEnvironment`, a
deterministic demo fixture. It is not production environment authority and must
be acknowledged with `--demo-environment`. The legacy
`harness.fake_environment` path is only a compatibility re-export.

## Prerequisites

- Activate the repository's `onr` conda environment.
- Ensure the configured vLLM-compatible endpoint and model are running.
- Ensure the temporal and symbolic planner executables named by
  `conf/onr_agent_params.yaml` exist and are executable.
- Use `transport.backend: file`; the demo environment consumes the file-backed
  maneuver command stream.

The mission file is strict JSON with exactly three non-empty string fields:

```json
{
  "mission_id": "mission:demo",
  "mission_text": "Survey the designated demo area and complete one bounded maneuver.",
  "source_authority": "demo-operator"
}
```

Unknown fields, missing fields, non-string values, blank values, malformed JSON,
and non-finite JSON values are rejected.

## Run

Start the read-only viewer in terminal 1:

```bash
conda activate onr
python -m onr.viewer.server --host 127.0.0.1 --port 14398 --repo-root . --config-path conf/onr_agent_params.yaml
```

Open `http://127.0.0.1:14398`, then run the mission in terminal 2:

```bash
conda activate onr
python -m onr.runtime.cli --mission-file examples/mission.json --repo-root . --config-path conf/onr_agent_params.yaml --planner-artifacts var/planner-artifacts --demo-environment
```

The CLI always verifies the configured LLM endpoint, composes the configured
real planners, creates model-backed Hyper Agent and Maneuver Control services,
and runs Context Coordination and FSM Runner. During the active mission session,
the runtime owns the lease and writes normal transport, operational-log, FSM,
planner, and summary artifacts. It prints only mission/result identifiers and
final public status; it does not print mission text or configuration secrets.

The viewer owns no runtime lifecycle. It polls `/api/runtime` and the selected
mission's `/api/trace` endpoint, cannot issue commands, and cannot invoke
summarization or an LLM. A short demo may only appear active for its execution
window; after the runtime stops its lease, the viewer correctly returns to idle.
