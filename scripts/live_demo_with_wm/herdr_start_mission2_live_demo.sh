#!/usr/bin/env bash
set -euo pipefail

readonly PHYSICAL_ROOT="/data/ccu/sukaih/ONR/onr_physical_runtime"
export ONR_DEMO_MISSION_MODE=mission2
export ONR_DEMO_SCENARIO_CONFIG="${ONR_DEMO_SCENARIO_CONFIG:-$PHYSICAL_ROOT/config/mission2_live_demo.yaml}"
exec bash "$(dirname "$0")/herdr_start_live_demo.sh" "$@"
