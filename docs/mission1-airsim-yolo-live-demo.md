# Mission 1 live demo: AirSim + YOLO perception + world model + Agent

Tracking: [#73](https://github.com/Sino-Huang/onr_agent_slim/issues/73).

This run puts every real component in the loop:

```
Harbor5_6 engine (freeze shim, offshore_dock_1/non_collision/0)
  -> onr_solution sukai_interface.run --perception yolo   (RGB + DepthPlanar -> YOLO11s-seg -> depth/pose localisation -> fleet-GPS identity gating)
  -> onr_physical_runtime agent.service (SceneClock owner, MultiGrid world model, Mission 1 report comparison)
  -> onr_agent_slim onr.runtime.cli (vLLM Hyper/Maneuver agents)
```

## Prerequisites

- conda `onr` activated; a running herdr session (`herdr --session <session>`).
- vLLM serving on `127.0.0.1:11411` (`bash scripts/vllm/start_vllm.sh`). vLLM
  uses two GPUs; YOLO defaults to `cuda:1` (override `ONR_DEMO_YOLO_DEVICE`).
- `onr_solution/yolo_weight/best.pt` present; `ultralytics` installed in `onr`.
- No other engine or producer: ports 41451, 8766 and the viewer port are free.
- `conf/env/settings_airsim.json` (runtime checkout) captures Scene and
  DepthPlanar at the same resolution on `front_center_custom`.

## Run

```bash
bash scripts/live_demo_with_wm/herdr_start_mission1_airsim_live_demo.sh <session>
```

The workspace has four panes:

- `airsim-engine` runs `onr_physical_runtime.sim.experimental_freeze.live_engine`.
  It stages the scene into Harbor's `EnvironmentConfigFiles/environment.json`
  and `object_ids.txt`, launches the engine under the freeze shim, and freezes
  it once all ships spawn. It restores both files when it exits. A detached
  guardian does the same if the pane is closed or killed.
- `perception` runs the Sukai producer in runtime-clocked mode, with
  `--perception yolo`.
- `physical-runtime` waits for producer health, provisions the scene clock
  epoch at the measured scene phase, and serves the world model.
- `agent-slim` builds the public Mission 1 planning views from the initial
  environment, then runs the Agent loop. The loop stops at 290 s mission time,
  before the recorded scene ends at 299.5 s.

Defaults, each overridable with its `ONR_DEMO_*` variable:

| Variable | Default |
| --- | --- |
| `ONR_DEMO_PERCEPTION` | `yolo` (`ideal` = native instance segmentation) |
| `ONR_DEMO_ENGINE_SCENARIO` | `onr_scenario/offshore_dock_1/non_collision/0` |
| `ONR_DEMO_SCENARIO_CONFIG` | runtime `config/mission1_airsim_live.yaml` |
| `ONR_DEMO_MISSION1_INSTANCE` | runtime `data/offshore_dock_1/mission1_instances/airsim-live-001` |
| `ONR_DEMO_MISSION_FILE` | `examples/mission1_airsim_live.json` |
| `ONR_DEMO_SIMULATION_LIMIT_SECONDS` | `290` |

The same `ONR_DEMO_PERCEPTION` switch works with the shared
`herdr_start_live_demo.sh` for any Mission 1 scene the engine can play; it
cannot be combined with `ONR_DEMO_AIRSIM_RPC_URL`, because the scene clock owns
the aircraft. Use `ONR_DEMO_DRY_RUN=1` to print all four commands without
starting anything.

## Verify

After the Agent pane reports a terminal result, run the audit command that the
launcher printed:

```bash
python scripts/audit_live_demo.py --run-root <run> --mission-mode mission1 --perception yolo
```

It writes `<run>/live-acceptance.json`. The audit passes only when all of these
hold:

- The FSM reached a terminal state, with a physical maneuver, a replan, and no
  operational or model errors or dead letters.
- Every camera-visible ship in the world model is labelled
  `yolo_camera_perception`.
- At least one Mission 1 `event_report_check` has
  `mission1_comparison.visibility_source == "external_camera"`.
- The producer manifest (`<run>/perception/runs/*/manifest.json`) records
  `perception: yolo`.

For per-frame detections, localisation and gating reasons, see
`<run>/perception/runs/*/perception_audit.jsonl`.

## Limits

- Mission 1 action checks remain the ideal scenario comparison. The detector
  supplies visibility, identity and position; it does not recognise actions.
- The freeze shim is still required (onr_solution #5).
- The v12 calibration reports `precision_floor_met=false`, and that status
  stays unchanged.
