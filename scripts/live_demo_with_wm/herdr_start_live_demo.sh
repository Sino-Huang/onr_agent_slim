#!/usr/bin/env bash

# Start the physical runtime and Agent Slim in a new two-pane
# workspace inside an existing herdr session.
# Optional ONR_DEMO_SCENARIO_CONFIG and ONR_DEMO_MISSION1_INSTANCE select a
# caller-supplied task without changing the default harbor demo or CLI arguments.
# ONR_DEMO_DIAGNOSTIC_PRIOR optionally installs an explicit oracle/flattening
# control bundle through the existing initial-belief store and outbox.

set -euo pipefail

readonly AGENT_ROOT="/data/ccu/sukaih/ONR/onr_agent_slim"
readonly PHYSICAL_ROOT="/data/ccu/sukaih/ONR/onr_physical_runtime"
readonly CONDA_INIT="/home/sukaih/miniconda3/etc/profile.d/conda.sh"
readonly MISSION_ID="mission:demo"
readonly VEHICLE_ID="drone-1"
readonly SCENARIO_CONFIG="${ONR_DEMO_SCENARIO_CONFIG:-$PHYSICAL_ROOT/config/harbor_world.yaml}"
readonly MISSION_INSTANCE="${ONR_DEMO_MISSION1_INSTANCE:-$PHYSICAL_ROOT/data/harbor_world/mission1_instances/demo-001}"
readonly MISSION_MODE="${ONR_DEMO_MISSION_MODE:-mission1}"
readonly MISSION2_SCENARIO="${ONR_DEMO_MISSION2_SCENARIO:-/data/ccu/sukaih/ONR/onr_scenario/offshore_dock_1/collision/0}"
readonly DRY_RUN="${ONR_DEMO_DRY_RUN:-0}"
readonly DIAGNOSTIC_PRIOR="${ONR_DEMO_DIAGNOSTIC_PRIOR:-}"
readonly MISSION1_PLANNING_INPUT="${ONR_DEMO_MISSION1_PLANNING_INPUT:-}"
readonly WORKSPACE_LABEL="$MISSION_MODE-live-demo"
case "$MISSION_MODE" in
    mission1) default_mission_file="$AGENT_ROOT/examples/mission.json" ;;
    mission2) default_mission_file="$AGENT_ROOT/examples/mission2.json" ;;
    joint) default_mission_file="$AGENT_ROOT/examples/mission1-and-2.json" ;;
    *) echo "ONR_DEMO_MISSION_MODE must be mission1, mission2 or joint." >&2; exit 2 ;;
esac
readonly MISSION_FILE="${ONR_DEMO_MISSION_FILE:-$default_mission_file}"

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "Usage: $0 <herdr-session-name>" >&2
    exit 2
fi

sessname="$1"

if [ ! -r "$SCENARIO_CONFIG" ] || [ ! -r "$MISSION_FILE" ]; then
    echo "Scenario configuration or Mission Input file is missing." >&2
    exit 1
fi
if [ -n "$DIAGNOSTIC_PRIOR" ] && { [ "$MISSION_MODE" = "mission2" ] || [ ! -r "$DIAGNOSTIC_PRIOR/manifest.json" ]; }; then
    echo "A readable diagnostic prior bundle requires Mission 1 mode (alone or joint)." >&2
    exit 1
fi
if [ -n "$MISSION1_PLANNING_INPUT" ] && { [ "$MISSION_MODE" = "mission2" ] || [ ! -r "$MISSION1_PLANNING_INPUT" ]; }; then
    echo "A readable Mission 1 planning input requires Mission 1 mode (alone or joint)." >&2
    exit 1
fi
mission_args=()
if [ "$MISSION_MODE" != "mission2" ]; then
    if [ ! -r "$MISSION_INSTANCE/events_report.json" ]; then
        echo "Mission 1 report stream is missing." >&2; exit 1
    fi
    mission_args+=(--mission1-instance-dir "$MISSION_INSTANCE")
fi
if [ "$MISSION_MODE" != "mission1" ]; then
    if [ ! -r "$MISSION2_SCENARIO/ships/events.json" ]; then
        echo "Mission 2 scenario is missing." >&2; exit 1
    fi
    if [ "$MISSION_MODE" = "joint" ] && [ -z "${ONR_DEMO_MISSION1_INSTANCE:-}" ]; then
        echo "Joint mode requires ONR_DEMO_MISSION1_INSTANCE with reports for the selected moving scenario." >&2; exit 1
    fi
    mission_args+=(--mission-mode "$MISSION_MODE" --mission2-scenario-dir "$MISSION2_SCENARIO")
fi

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
    -e "s|^  mission1_planning_input_path: null$|  mission1_planning_input_path: ${MISSION1_PLANNING_INPUT:-null}|" \
    "$AGENT_ROOT/conf/environment_physical.yaml" > "$environment_config"

initial_event="$transport_root/identity/event-environment-update%3Amission%3Ademo%3Ainitial.json"

if [ -n "$DIAGNOSTIC_PRIOR" ]; then
    (
    source "$CONDA_INIT"
    conda activate onr
    python "$AGENT_ROOT/scripts/prepare_reporting_prior.py" --agent-var "$AGENT_ROOT/var" install \
        --bundle "$DIAGNOSTIC_PRIOR" --storage-root "$agent_storage_root" --mission-id "$MISSION_ID"
    )
fi

physical_args=(python -u -m onr_physical_runtime.agent.service --scenario-config "$SCENARIO_CONFIG"
    --transport-root "$transport_root" --state-root "$physical_state_root" --mission-id "$MISSION_ID"
    --vehicle-id "$VEHICLE_ID" "${mission_args[@]}" --viewer-host 127.0.0.1 --viewer-port 5066)
printf -v physical_python '%q ' "${physical_args[@]}"
physical_inner="set -e; source '$CONDA_INIT'; conda activate onr; cd '$PHYSICAL_ROOT'; exec $physical_python"
printf -v physical_command 'bash -lc %q' "$physical_inner"

agent_args=(python -u -m onr.runtime.cli --mission-file "$MISSION_FILE" --repo-root "$AGENT_ROOT"
    --config-path "$agent_config" --skip-runtime-artifact-rollover)
printf -v agent_python '%q ' "${agent_args[@]}"
agent_inner="set -e; source '$CONDA_INIT'; conda activate onr; cd '$AGENT_ROOT'; echo 'Waiting for the physical runtime initial update...'; for attempt in {1..120}; do [ -f '$initial_event' ] && break; sleep 1; done; if [ ! -f '$initial_event' ]; then echo 'Physical runtime did not publish its initial update within 120 seconds.' >&2; exit 1; fi; exec $agent_python"
printf -v agent_command 'bash -lc %q' "$agent_inner"

if [ "$DRY_RUN" = "1" ]; then
    printf 'DRY RUN: mode=%s; no services started\nRun configuration: %s\nPhysical command: %s\nAgent command: %s\n' \
        "$MISSION_MODE" "$run_root" "$physical_command" "$agent_command"
    exit 0
fi

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
echo "Scenario: $SCENARIO_CONFIG"
echo "Mission mode: $MISSION_MODE; Mission Input: $MISSION_FILE"
if [ "$MISSION_MODE" != "mission2" ]; then echo "Mission 1 instance: $MISSION_INSTANCE"; fi
if [ "$MISSION_MODE" != "mission1" ]; then echo "Mission 2 scenario: $MISSION2_SCENARIO"; fi
echo "World-model frame stream: http://127.0.0.1:5066"
echo "Attach with: herdr --session $sessname"
