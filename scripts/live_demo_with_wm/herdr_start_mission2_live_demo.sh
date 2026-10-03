#!/usr/bin/env bash
set -euo pipefail

# The mission2 preset (conf/stack_presets.yaml) supplies the scenario and inputs.
export ONR_DEMO_MISSION_MODE=mission2
export ONR_DEMO_PRESET=mission2
exec bash "$(dirname "$0")/herdr_start_live_demo.sh" "$@"
