#!/usr/bin/env bash
set -euo pipefail

# The mission4 preset (conf/stack_presets.yaml) supplies the scenario and inputs.
export ONR_DEMO_MISSION_MODE=mission4
export ONR_DEMO_PRESET=mission4
exec bash "$(dirname "$0")/herdr_start_live_demo.sh" "$@"
