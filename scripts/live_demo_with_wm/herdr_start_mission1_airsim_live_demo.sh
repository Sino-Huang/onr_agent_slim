#!/usr/bin/env bash
set -euo pipefail

# Mission 1 against the real Harbor engine and learned (YOLO) ship perception.
# ONR_DEMO_PERCEPTION=ideal swaps in native instance segmentation instead.
# The mission1-airsim preset (conf/stack_presets.yaml) supplies the scenario,
# Mission 1 instance, engine scenario, Mission Input and the 290 s limit that
# stops the Agent loop before the 299.5 s engine scenario ends.
export ONR_DEMO_MISSION_MODE=mission1
export ONR_DEMO_PRESET=mission1-airsim
exec bash "$(dirname "$0")/herdr_start_live_demo.sh" "$@"
