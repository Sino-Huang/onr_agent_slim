#!/usr/bin/env bash

# Start the physical runtime and Agent Slim in a new herdr workspace inside an
# existing herdr session.
#
# Command composition lives in the Python Stack Builder
# (`python -m onr.runtime_host.stack plan --demo-env`, presets in
# conf/stack_presets.yaml), shared with the Runtime Host. This script keeps the
# ONR_DEMO_* interface and owns only the herdr session, workspace and panes.
#
# ONR_DEMO_PRESET selects a stack preset (the mode wrappers set it); otherwise
# ONR_DEMO_MISSION_MODE (default mission1) selects the bare mode defaults.
# Optional ONR_DEMO_SCENARIO_CONFIG and ONR_DEMO_MISSION1_INSTANCE select a
# caller-supplied task without changing the default harbor demo or CLI arguments.
# Mission 1 prepares public native-camera views unless the caller supplies an
# explicit ONR_DEMO_MISSION1_PLANNING_INPUT.
# ONR_DEMO_DIAGNOSTIC_PRIOR optionally installs an explicit oracle/flattening
# control bundle through the existing initial-belief store and outbox.
# ONR_DEMO_PERCEPTION=yolo|ideal replaces simulated ship evidence with the real
# Harbor engine (ONR_DEMO_ENGINE_SCENARIO, run under the freeze shim) and the
# onr_solution Sukai producer; two extra panes own the engine and producer.

set -euo pipefail

readonly AGENT_ROOT="/data/ccu/sukaih/ONR/onr_agent_slim"
readonly PHYSICAL_ROOT="/data/ccu/sukaih/ONR/onr_physical_runtime"
readonly DRY_RUN="${ONR_DEMO_DRY_RUN:-0}"

# The launcher's own shell parses herdr JSON with jq. Use the repository's
# vendored jq (the same copy the hyper agent backend puts on PATH) so jq is
# guaranteed regardless of the caller's environment.
export PATH="$AGENT_ROOT/modules/jq:$PATH"

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "Usage: $0 <herdr-session-name>" >&2
    exit 2
fi

sessname="$1"

# Fail fast with actionable guidance before any side effect when the caller's
# environment is incomplete. jq comes from modules/jq above; python and herdr
# must come from the caller, e.g. the activated onr conda environment.
required_tools=(python)
if [ "$DRY_RUN" != "1" ]; then required_tools=(herdr jq python); fi
for required_tool in "${required_tools[@]}"; do
    if ! command -v "$required_tool" >/dev/null 2>&1; then
        echo "The live-demo launcher requires '$required_tool' on PATH." >&2
        echo "Activate the onr conda environment first (it provides python; jq is also vendored under modules/jq)." >&2
        exit 1
    fi
done

stack_plan() {
    PYTHONPATH="$AGENT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python -m onr.runtime_host.stack plan \
        --demo-env --repo-root "$AGENT_ROOT" --physical-root "$PHYSICAL_ROOT" "$@"
}

# Validate every ONR_DEMO_* input without side effects; sets mission_mode,
# workspace_label and probe_ports.
plan_check="$(stack_plan --check)"
eval "$plan_check"

if [ "$DRY_RUN" != "1" ]; then
session_list="$(herdr session list)"
status="$(printf '%s\n' "$session_list" | awk -v session="$sessname" '$1 == session {print $2}')"
if [ -z "$status" ]; then
    echo "No herdr session named '$sessname' exists." >&2
    echo "Create and start it first with: herdr --session $sessname" >&2
    exit 1
fi
if [ "$status" != "running" ]; then
    echo "Herdr session '$sessname' is not running (status: $status)." >&2
    echo "Start it first with: herdr --session $sessname" >&2
    exit 1
fi

# A live demo owns one workspace label. Closing any prior matching workspace
# also terminates its pane processes while preserving its run data under var.
existing_workspace_ids="$(
    HERDR_SESSION="$sessname" herdr workspace list |
        jq -r --arg label "$workspace_label" \
            '.result.workspaces[] | select(.label == $label) | .workspace_id'
)"
for existing_workspace_id in $existing_workspace_ids; do
    HERDR_SESSION="$sessname" herdr workspace close "$existing_workspace_id"
done

read -r -a ports_to_probe <<< "$probe_ports"
for probe_port in "${ports_to_probe[@]}"; do
if ! python - "$probe_port" <<'PY'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    # A just-stopped engine leaves TIME_WAIT sockets; only a listener blocks.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        raise SystemExit(1) from None
PY
then
    echo "Address 127.0.0.1:$probe_port is already in use." >&2
    echo "Close the owning workspace or process (a running engine or producer), or set ONR_DEMO_VIEWER_PORT to a free viewer port." >&2
    exit 1
fi
done
fi

# Keep each live demo isolated while retaining all generated state under the
# repository's conventional var directory (Missions 2-4 use mode-specific
# parents). Sets run_root, the pane commands, audit_command, prepare_command
# and summary.
plan_output="$(stack_plan)"
eval "$plan_output"

if [ -n "$prepare_command" ]; then
    eval "$prepare_command"
fi

if [ "$DRY_RUN" = "1" ]; then
    printf 'DRY RUN: mode=%s; no services started\nRun configuration: %s\nPhysical command: %s\nAgent command: %s\nTerminal audit: %s\n' \
        "$mission_mode" "$run_root" "$physical_command" "$agent_command" "$audit_command"
    if [ -n "$worker_command" ]; then printf 'Worker command: %s\n' "$worker_command"; fi
    if [ -n "$engine_command" ]; then printf 'Engine command: %s\nPerception command: %s\n' "$engine_command" "$perception_command"; fi
    exit 0
fi

create_out="$(HERDR_SESSION="$sessname" herdr workspace create --cwd "$AGENT_ROOT" --label "$workspace_label" --no-focus)"
physical_pane="$(printf '%s' "$create_out" | jq -r '.result.root_pane.pane_id')"
workspace_id="$(printf '%s' "$create_out" | jq -r '.result.workspace.workspace_id')"

HERDR_SESSION="$sessname" herdr pane rename "$physical_pane" "physical-runtime"
HERDR_SESSION="$sessname" herdr pane run "$physical_pane" "$physical_command"

agent_pane="$(HERDR_SESSION="$sessname" herdr pane split "$physical_pane" --direction right --no-focus | jq -r '.result.pane.pane_id')"
HERDR_SESSION="$sessname" herdr pane rename "$agent_pane" "agent-slim"
HERDR_SESSION="$sessname" herdr pane run "$agent_pane" "$agent_command"

if [ -n "$worker_command" ]; then
    worker_pane="$(HERDR_SESSION="$sessname" herdr pane split "$physical_pane" --direction down --no-focus | jq -r '.result.pane.pane_id')"
    HERDR_SESSION="$sessname" herdr pane rename "$worker_pane" "mission4-worker"
    HERDR_SESSION="$sessname" herdr pane run "$worker_pane" "$worker_command"
fi

if [ -n "$engine_command" ]; then
    engine_pane="$(HERDR_SESSION="$sessname" herdr pane split "$physical_pane" --direction down --no-focus | jq -r '.result.pane.pane_id')"
    HERDR_SESSION="$sessname" herdr pane rename "$engine_pane" "airsim-engine"
    HERDR_SESSION="$sessname" herdr pane run "$engine_pane" "$engine_command"
    perception_pane="$(HERDR_SESSION="$sessname" herdr pane split "$agent_pane" --direction down --no-focus | jq -r '.result.pane.pane_id')"
    HERDR_SESSION="$sessname" herdr pane rename "$perception_pane" "perception"
    HERDR_SESSION="$sessname" herdr pane run "$perception_pane" "$perception_command"
fi

echo "Created workspace '$workspace_label' ($workspace_id) in herdr session '$sessname'."
printf '%s\n' "$summary"
echo "Attach with: herdr --session $sessname"
