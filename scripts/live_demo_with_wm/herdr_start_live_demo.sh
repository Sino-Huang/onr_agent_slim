#!/usr/bin/env bash

# Start the Mission 1 physical runtime and Agent Slim in a new two-pane
# workspace inside an existing herdr session.

set -euo pipefail

readonly AGENT_ROOT="/data/ccu/sukaih/ONR/onr_agent_slim"
readonly PHYSICAL_ROOT="/data/ccu/sukaih/ONR/onr_physical_runtime"
readonly CONDA_INIT="/home/sukaih/miniconda3/etc/profile.d/conda.sh"
readonly MISSION_ID="mission:demo"
readonly VEHICLE_ID="drone-1"
readonly MISSION_INSTANCE="$PHYSICAL_ROOT/data/harbor_world/mission1_instances/demo-001"
readonly WORKSPACE_LABEL="mission1-live-demo"

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "Usage: $0 <herdr-session-name>" >&2
    exit 2
fi

sessname="$1"

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

# Keep each live demo isolated while retaining all generated state under the
# repository's conventional var directory.
mkdir -p "$AGENT_ROOT/var/live_demo_with_wm"
run_root="$(mktemp -d "$AGENT_ROOT/var/live_demo_with_wm/run.XXXXXX")"
transport_root="$run_root/transport"
physical_state_root="$run_root/physical-state"
agent_storage_root="$run_root/agent-storage"
planner_artifacts_root="$run_root/planner-artifacts"
environment_artifacts_root="$run_root/environment-artifacts"
agent_config="$run_root/onr_agent_params.yaml"
environment_config="$run_root/environment_physical.yaml"

mkdir -p \
    "$transport_root" \
    "$physical_state_root" \
    "$agent_storage_root" \
    "$planner_artifacts_root" \
    "$environment_artifacts_root"

sed \
    -e "s|^environment_profile: .*|environment_profile: $environment_config|" \
    -e "s|^  root: var/transport$|  root: $transport_root|" \
    -e "s|^  root: var/storage$|  root: $agent_storage_root|" \
    -e "s|^  planner_artifacts: var/planner-artifacts$|  planner_artifacts: $planner_artifacts_root|" \
    "$AGENT_ROOT/conf/onr_agent_params.yaml" > "$agent_config"

sed \
    -e "s|^  planning_artifact_root: var/environment$|  planning_artifact_root: $environment_artifacts_root|" \
    "$AGENT_ROOT/conf/environment_physical.yaml" > "$environment_config"

initial_event="$transport_root/identity/event-environment-update%3Amission%3Ademo%3Ainitial.json"

physical_inner="set -e; source '$CONDA_INIT'; conda activate onr; cd '$PHYSICAL_ROOT'; exec python -m onr_physical_runtime.agent.service --scenario-config '$PHYSICAL_ROOT/config/harbor_world.yaml' --transport-root '$transport_root' --state-root '$physical_state_root' --mission-id '$MISSION_ID' --vehicle-id '$VEHICLE_ID' --mission1-instance-dir '$MISSION_INSTANCE'"
printf -v physical_command 'bash -lc %q' "$physical_inner"

agent_inner="set -e; source '$CONDA_INIT'; conda activate onr; cd '$AGENT_ROOT'; echo 'Waiting for the physical runtime initial update...'; for attempt in {1..120}; do [ -f '$initial_event' ] && break; sleep 1; done; if [ ! -f '$initial_event' ]; then echo 'Physical runtime did not publish its initial update within 120 seconds.' >&2; exit 1; fi; exec python -m onr.runtime.cli --mission-file '$AGENT_ROOT/examples/mission.json' --repo-root '$AGENT_ROOT' --config-path '$agent_config' --skip-runtime-artifact-rollover"
printf -v agent_command 'bash -lc %q' "$agent_inner"

# A live demo owns one workspace label. Closing any prior matching workspace
# also terminates its pane processes while preserving its run data under var.
existing_workspace_ids="$(
    HERDR_SESSION="$sessname" herdr workspace list |
        jq -r --arg label "$WORKSPACE_LABEL" \
            '.result.workspaces[] | select(.label == $label) | .workspace_id'
)"
for existing_workspace_id in $existing_workspace_ids; do
    HERDR_SESSION="$sessname" herdr workspace close "$existing_workspace_id"
done

create_out="$(HERDR_SESSION="$sessname" herdr workspace create --cwd "$AGENT_ROOT" --label "$WORKSPACE_LABEL" --no-focus)"
physical_pane="$(printf '%s' "$create_out" | jq -r '.result.root_pane.pane_id')"
workspace_id="$(printf '%s' "$create_out" | jq -r '.result.workspace.workspace_id')"

HERDR_SESSION="$sessname" herdr pane rename "$physical_pane" "physical-runtime"
HERDR_SESSION="$sessname" herdr pane run "$physical_pane" "$physical_command"

agent_pane="$(HERDR_SESSION="$sessname" herdr pane split "$physical_pane" --direction right --no-focus | jq -r '.result.pane.pane_id')"
HERDR_SESSION="$sessname" herdr pane rename "$agent_pane" "agent-slim"
HERDR_SESSION="$sessname" herdr pane run "$agent_pane" "$agent_command"

echo "Created workspace '$WORKSPACE_LABEL' ($workspace_id) in herdr session '$sessname'."
echo "Run data: $run_root"
echo "Attach with: herdr --session $sessname"
