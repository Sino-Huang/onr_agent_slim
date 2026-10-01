#!/usr/bin/env bash
set -euo pipefail

# Mission 1 against the real Harbor engine and learned (YOLO) ship perception.
# ONR_DEMO_PERCEPTION=ideal swaps in native instance segmentation instead.
readonly PHYSICAL_ROOT="/data/ccu/sukaih/ONR/onr_physical_runtime"
readonly AGENT_ROOT="/data/ccu/sukaih/ONR/onr_agent_slim"
export ONR_DEMO_MISSION_MODE=mission1
export ONR_DEMO_PERCEPTION="${ONR_DEMO_PERCEPTION:-yolo}"
export ONR_DEMO_SCENARIO_CONFIG="${ONR_DEMO_SCENARIO_CONFIG:-$PHYSICAL_ROOT/config/mission1_airsim_live.yaml}"
export ONR_DEMO_MISSION1_INSTANCE="${ONR_DEMO_MISSION1_INSTANCE:-$PHYSICAL_ROOT/data/offshore_dock_1/mission1_instances/airsim-live-001}"
export ONR_DEMO_ENGINE_SCENARIO="${ONR_DEMO_ENGINE_SCENARIO:-/data/ccu/sukaih/ONR/onr_scenario/offshore_dock_1/non_collision/0}"
export ONR_DEMO_MISSION_FILE="${ONR_DEMO_MISSION_FILE:-$AGENT_ROOT/examples/mission1_airsim_live.json}"
# The engine scenario ends at 299.5 s; stop the Agent loop before the scene does.
export ONR_DEMO_SIMULATION_LIMIT_SECONDS="${ONR_DEMO_SIMULATION_LIMIT_SECONDS:-290}"
exec bash "$(dirname "$0")/herdr_start_live_demo.sh" "$@"
