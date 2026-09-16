#!/usr/bin/env bash

# Start the physical runtime and Agent Slim in a new two-pane
# workspace inside an existing herdr session.
# Optional ONR_DEMO_SCENARIO_CONFIG and ONR_DEMO_MISSION1_INSTANCE select a
# caller-supplied task without changing the default harbor demo or CLI arguments.
# Mission 1 prepares public native-camera views unless the caller supplies an
# explicit ONR_DEMO_MISSION1_PLANNING_INPUT.
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
readonly MISSION3_DESCRIPTION="${ONR_DEMO_MISSION3_DESCRIPTION:-$AGENT_ROOT/examples/mission3_description.json}"
readonly MISSION3_FIXTURE="${ONR_DEMO_MISSION3_FIXTURE:-}"
readonly MISSION4_PACKAGE="${ONR_DEMO_MISSION4_PACKAGE:-$PHYSICAL_ROOT/docs/mission_desc/mission4_package.json}"
readonly MISSION4_FIXTURE="${ONR_DEMO_MISSION4_FIXTURE:-$PHYSICAL_ROOT/docs/mission_desc/mission4_fixture.json}"
readonly MISSION4_REQUESTS="${ONR_DEMO_MISSION4_REQUESTS:-$AGENT_ROOT/examples/mission4_requests.json}"
readonly MISSION4_WORKER_TIMEOUT_SECONDS="${ONR_DEMO_MISSION4_WORKER_TIMEOUT_SECONDS:-3600}"
readonly VIEWER_PORT="${ONR_DEMO_VIEWER_PORT:-5066}"
readonly WORKSPACE_LABEL="$MISSION_MODE-live-demo"
if [ "$MISSION_MODE" = "mission1" ] || [ "$MISSION_MODE" = "joint" ]; then
    default_maneuver_seconds=30
else
    default_maneuver_seconds=300
fi
readonly MANEUVER_SECONDS="${ONR_DEMO_MANEUVER_SECONDS:-$default_maneuver_seconds}"
case "$MISSION_MODE" in
    mission1) default_mission_file="$AGENT_ROOT/examples/mission.json" ;;
    mission2) default_mission_file="$AGENT_ROOT/examples/mission2.json" ;;
    mission3) default_mission_file="$AGENT_ROOT/examples/mission3.json" ;;
    mission4) default_mission_file="$AGENT_ROOT/examples/mission4.json" ;;
    joint) default_mission_file="$AGENT_ROOT/examples/mission1-and-2.json" ;;
    *) echo "ONR_DEMO_MISSION_MODE must be mission1, mission2, mission3, mission4 or joint." >&2; exit 2 ;;
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
if [ -n "$DIAGNOSTIC_PRIOR" ] && { { [ "$MISSION_MODE" != "mission1" ] && [ "$MISSION_MODE" != "joint" ]; } || [ ! -r "$DIAGNOSTIC_PRIOR/manifest.json" ]; }; then
    echo "A readable diagnostic prior bundle requires Mission 1 mode (alone or joint)." >&2
    exit 1
fi
if [ -n "$MISSION1_PLANNING_INPUT" ] && { { [ "$MISSION_MODE" != "mission1" ] && [ "$MISSION_MODE" != "joint" ]; } || [ ! -r "$MISSION1_PLANNING_INPUT" ]; }; then
    echo "A readable Mission 1 planning input requires Mission 1 mode (alone or joint)." >&2
    exit 1
fi
mission_args=()
if [ "$MISSION_MODE" = "mission1" ] || [ "$MISSION_MODE" = "joint" ]; then
    if [ ! -r "$MISSION_INSTANCE/events_report.json" ]; then
        echo "Mission 1 report stream is missing." >&2; exit 1
    fi
    mission_args+=(--mission1-instance-dir "$MISSION_INSTANCE")
fi
if [ "$MISSION_MODE" = "mission2" ] || [ "$MISSION_MODE" = "joint" ]; then
    if [ ! -r "$MISSION2_SCENARIO/ships/events.json" ]; then
        echo "Mission 2 scenario is missing." >&2; exit 1
    fi
    if [ "$MISSION_MODE" = "joint" ] && [ -z "${ONR_DEMO_MISSION1_INSTANCE:-}" ]; then
        echo "Joint mode requires ONR_DEMO_MISSION1_INSTANCE with reports for the selected moving scenario." >&2; exit 1
    fi
    mission_args+=(--mission-mode "$MISSION_MODE" --mission2-scenario-dir "$MISSION2_SCENARIO")
fi
if [ "$MISSION_MODE" = "mission3" ]; then
    if [ ! -r "$MISSION3_DESCRIPTION" ]; then
        echo "Mission 3 description is missing." >&2; exit 1
    fi
    mission_args+=(--mission-mode mission3 --mission3-selection "$MISSION3_DESCRIPTION")
    if [ -n "$MISSION3_FIXTURE" ]; then
        if [ ! -r "$MISSION3_FIXTURE" ]; then
            echo "Mission 3 fixture is missing." >&2; exit 1
        fi
        mission_args+=(--mission3-fixture "$MISSION3_FIXTURE")
    fi
fi
if [ "$MISSION_MODE" = "mission4" ]; then
    if [ ! -r "$MISSION4_PACKAGE" ] || [ ! -r "$MISSION4_FIXTURE" ] || [ ! -r "$MISSION4_REQUESTS" ]; then
        echo "Mission 4 package, fixture or request script is missing." >&2; exit 1
    fi
    mission_args+=(--mission-mode mission4 --mission4-package "$MISSION4_PACKAGE" --mission4-fixture "$MISSION4_FIXTURE")
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

if ! python - "$VIEWER_PORT" <<'PY'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    try:
        probe.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        raise SystemExit(1) from None
PY
then
    echo "Viewer address 127.0.0.1:$VIEWER_PORT is already in use." >&2
    echo "Close the owning workspace or process, or set ONR_DEMO_VIEWER_PORT to a free port." >&2
    exit 1
fi
fi

# Keep each live demo isolated while retaining all generated state under the
# repository's conventional var directory. Missions 2-4 use mission-specific
# parents so their output cannot be mistaken for the original Mission 1 runs.
run_parent="$AGENT_ROOT/var/live_demo_with_wm"
case "$MISSION_MODE" in
    mission2|mission3|mission4) run_parent="$run_parent/$MISSION_MODE" ;;
esac
mkdir -p "$run_parent"
run_root="$(mktemp -d "$run_parent/run.XXXXXX")"
transport_root="$run_root/transport"
physical_state_root="$run_root/physical-state"
agent_storage_root="$run_root/agent-storage"
planner_artifacts_root="$run_root/planner-artifacts"
environment_artifacts_root="$run_root/environment-artifacts"
closed_loop_result="$run_root/closed-loop-result.json"
mission4_worker_state="$run_root/mission4-worker-session.json"
mission4_worker_ready="$run_root/mission4-worker-ready.json"
agent_config="$run_root/onr_agent_params.yaml"
environment_config="$run_root/environment_physical.yaml"
resolved_mission1_planning_input="$MISSION1_PLANNING_INPUT"
prepare_mission1_planning_input=0
if [ "$MISSION_MODE" = "mission1" ] && [ -z "$resolved_mission1_planning_input" ]; then
    resolved_mission1_planning_input="$run_root/mission1-planning-input/environment.json"
    prepare_mission1_planning_input=1
fi

mkdir -p \
    "$transport_root" \
    "$physical_state_root" \
    "$agent_storage_root" \
    "$planner_artifacts_root" \
    "$environment_artifacts_root"

sed \
    -e "s|^environment_profile: .*|environment_profile: $environment_config|" \
    -e "s|^  maneuver_seconds: .*|  maneuver_seconds: $MANEUVER_SECONDS|" \
    -e "s|^  root: var/transport$|  root: $transport_root|" \
    -e "s|^  root: var/storage$|  root: $agent_storage_root|" \
    -e "s|^  planner_artifacts: var/planner-artifacts$|  planner_artifacts: $planner_artifacts_root|" \
    "$AGENT_ROOT/conf/onr_agent_params.yaml" > "$agent_config"

sed \
    -e "s|^  planning_artifact_root: var/environment$|  planning_artifact_root: $environment_artifacts_root|" \
    -e "s|^  mission1_planning_input_path: null$|  mission1_planning_input_path: ${resolved_mission1_planning_input:-null}|" \
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
    --vehicle-id "$VEHICLE_ID" "${mission_args[@]}" --viewer-host 127.0.0.1 --viewer-port "$VIEWER_PORT")
printf -v physical_python '%q ' "${physical_args[@]}"
physical_inner="set -e; source '$CONDA_INIT'; conda activate onr; cd '$PHYSICAL_ROOT'; exec $physical_python"
printf -v physical_command 'bash -lc %q' "$physical_inner"

agent_args=(python -u -m onr.runtime.cli --mission-file "$MISSION_FILE" --repo-root "$AGENT_ROOT"
    --config-path "$agent_config" --skip-runtime-artifact-rollover --result-path "$closed_loop_result")
printf -v agent_python '%q ' "${agent_args[@]}"
planning_preparation=""
if [ "$prepare_mission1_planning_input" = "1" ]; then
    public_input_root="$run_root/mission1-public-input"
    planning_input_root="$run_root/mission1-planning-input"
    public_input_args=(python "$AGENT_ROOT/scripts/prepare_live_mission1_public_inputs.py"
        --transport-root "$transport_root" --mission-id "$MISSION_ID" --output "$public_input_root")
    view_input_args=(env "PYTHONPATH=$PHYSICAL_ROOT/src:$AGENT_ROOT/src" python
        "$PHYSICAL_ROOT/scripts/prepare_surveillance_views.py" --scenario "$SCENARIO_CONFIG"
        --environment "$public_input_root/environment.json" --belief "$public_input_root/belief.json"
        --agent-var "$AGENT_ROOT/var" --output "$planning_input_root")
    printf -v public_input_python '%q ' "${public_input_args[@]}"
    printf -v view_input_python '%q ' "${view_input_args[@]}"
    planning_preparation="$public_input_python&& $view_input_python&& "
fi
agent_ready_file="$initial_event"
agent_ready_description="physical runtime initial update"
if [ "$MISSION_MODE" = "mission4" ]; then
    agent_ready_file="$mission4_worker_ready"
    agent_ready_description="initial Mission 4 worker request receipt"
fi
agent_inner="set -e; source '$CONDA_INIT'; conda activate onr; cd '$AGENT_ROOT'; echo 'Waiting for the $agent_ready_description...'; for attempt in {1..120}; do [ -f '$agent_ready_file' ] && break; sleep 1; done; if [ ! -f '$agent_ready_file' ]; then echo '$agent_ready_description was not available within 120 seconds.' >&2; exit 1; fi; echo 'Waiting for the configured vLLM endpoint...'; for attempt in {1..120}; do curl -fsS --max-time 2 http://127.0.0.1:11411/v1/models >/dev/null 2>&1 && break; sleep 1; done; if ! curl -fsS --max-time 2 http://127.0.0.1:11411/v1/models >/dev/null 2>&1; then echo 'configured vLLM endpoint was not available within 120 seconds.' >&2; exit 1; fi; ${planning_preparation}exec $agent_python"
printf -v agent_command 'bash -lc %q' "$agent_inner"

worker_command=""
if [ "$MISSION_MODE" = "mission4" ]; then
    worker_args=(python -u -m onr.adapters.mission4_worker --mission-id "$MISSION_ID"
        --session "$mission4_worker_state" --request-directory "$physical_state_root/search_requests"
        --transport-root "$transport_root" --script "$MISSION4_REQUESTS" --ready-file "$mission4_worker_ready"
        --timeout-seconds "$MISSION4_WORKER_TIMEOUT_SECONDS")
    printf -v worker_python '%q ' "${worker_args[@]}"
    worker_inner="set -e; source '$CONDA_INIT'; conda activate onr; cd '$AGENT_ROOT'; exec $worker_python"
    printf -v worker_command 'bash -lc %q' "$worker_inner"
fi

audit_args=(python scripts/audit_live_demo.py --run-root "$run_root" --mission-mode "$MISSION_MODE")
if [ "$MISSION_MODE" = "mission2" ]; then
    audit_args+=(--mission2-scenario-dir "$MISSION2_SCENARIO")
fi
printf -v audit_command '%q ' "${audit_args[@]}"

if [ "$DRY_RUN" = "1" ]; then
    printf 'DRY RUN: mode=%s; no services started\nRun configuration: %s\nPhysical command: %s\nAgent command: %s\nTerminal audit: %s\n' \
        "$MISSION_MODE" "$run_root" "$physical_command" "$agent_command" "$audit_command"
    if [ -n "$worker_command" ]; then printf 'Worker command: %s\n' "$worker_command"; fi
    exit 0
fi

create_out="$(HERDR_SESSION="$sessname" herdr workspace create --cwd "$AGENT_ROOT" --label "$WORKSPACE_LABEL" --no-focus)"
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

echo "Created workspace '$WORKSPACE_LABEL' ($workspace_id) in herdr session '$sessname'."
echo "Run data: $run_root"
echo "Scenario: $SCENARIO_CONFIG"
echo "Mission mode: $MISSION_MODE; Mission Input: $MISSION_FILE"
if [ "$MISSION_MODE" = "mission1" ] || [ "$MISSION_MODE" = "joint" ]; then echo "Mission 1 instance: $MISSION_INSTANCE"; fi
if [ "$MISSION_MODE" = "mission2" ] || [ "$MISSION_MODE" = "joint" ]; then echo "Mission 2 scenario: $MISSION2_SCENARIO"; fi
if [ "$MISSION_MODE" = "mission3" ]; then echo "Mission 3 description: $MISSION3_DESCRIPTION; fixture: ${MISSION3_FIXTURE:-live}"; fi
if [ "$MISSION_MODE" = "mission4" ]; then echo "Mission 4 package: $MISSION4_PACKAGE; fixture: $MISSION4_FIXTURE; requests: $MISSION4_REQUESTS"; fi
echo "Terminal audit: $audit_command"
echo "World-model frame stream: http://127.0.0.1:$VIEWER_PORT"
echo "Attach with: herdr --session $sessname"
