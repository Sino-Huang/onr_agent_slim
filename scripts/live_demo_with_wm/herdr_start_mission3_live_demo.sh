#!/usr/bin/env bash
set -euo pipefail

readonly PHYSICAL_ROOT="/data/ccu/sukaih/ONR/onr_physical_runtime"
export ONR_DEMO_MISSION_MODE=mission3
export ONR_DEMO_SCENARIO_CONFIG="${ONR_DEMO_SCENARIO_CONFIG:-$PHYSICAL_ROOT/config/mission3_smoke/scenario.yaml}"
export ONR_DEMO_MISSION3_FIXTURE="${ONR_DEMO_MISSION3_FIXTURE:-$PHYSICAL_ROOT/config/mission3_smoke/private_fixture.json}"
exec bash "$(dirname "$0")/herdr_start_live_demo.sh" "$@"
