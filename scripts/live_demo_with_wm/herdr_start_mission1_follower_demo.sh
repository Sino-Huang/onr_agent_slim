#!/usr/bin/env bash
set -euo pipefail

# Mission 1 on simulated information with the AirSim Follower (ADR 0016): the
# world model runs on the mission1-harbor preset (conf/stack_presets.yaml),
# the airsim-fixture prep step builds a lead-in Harbor scene from the world
# model's own ship trajectories, and the airsim-visualizer pane steps that
# frozen scene to each world-model Mission time. AirSim only visualizes; it
# never feeds the world model. Perception stays off.
export ONR_DEMO_MISSION_MODE=mission1
export ONR_DEMO_PRESET=mission1-harbor
export ONR_DEMO_AIRSIM=1
exec bash "$(dirname "$0")/herdr_start_live_demo.sh" "$@"
