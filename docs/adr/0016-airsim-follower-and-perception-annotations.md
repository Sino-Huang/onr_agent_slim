# AirSim runs with or without perception, and what the console shows

Status: accepted

Extends ADR 0015. Every Mission Run that enables AirSim starts the Harbor engine. Which process owns AirSim time depends on whether a perception module is in the loop.

**Perception off: the AirSim Follower.** The world model runs on simulated information. AirSim only visualizes it. This is the issue #65 reconstruction approach run live:

1. A prep step builds a lead-in fixture of the world model's own ship trajectories. The source is the scenario's `trajectory_directory` plus `trajectory_ned_offset`. It works for the `harbor_world` vessels and the `offshore_dock_1` engine scenario.
2. `live_engine` freezes Harbor on that fixture.
3. The `airsim-visualizer` stack service, `onr.demo.airsim_reconstruction.follower`, then handles each new world-model Mission time read from `environment-data`:
   - it steps the frozen scene to `lead_in + mission_time`;
   - it teleports the drone to the world-model pose and flushes one 0.1 s render step;
   - it captures the front Scene, front Segmentation and third-person Scene images;
   - it publishes raw frames plus ideal instance-segmentation boxes.

The same playback offset made `SceneClock.establish` (onr_physical_runtime) reject perception runs with `scene_clock_initial_phase_mismatch` whenever spawn latency exceeded its 0.25 s tolerance; it was 0.4–1.4 s on 7 launches under host CPU contention. The scene clock now measures the offset once at establishment and provisions `mission_epoch_s` from the calibrated ship phase. The hold, step and 0.25 s agreement checks run against that calibrated phase, so ships that disagree with each other still fail.

How the follower keeps the scene on Mission time:

- **Scene time.** It advances the shim's private wall clock by the measured physics interval, as `SceneClock.prepare` does.
- **Ship timing.** Harbor starts ship playback a variable interval after `scenario_start_time`. That offset also differs between the synthetic lead-in and the recorded rows. So every beat re-measures ship phase against the fixture trajectories and closes any lag before the flush. Live measurement: the residual is at most 8 ms.
- **Lead-in.** It is 30 s. That puts Mission time 0 ahead of the phase where the engine freezes, so the scene only ever steps forward.

The follower never feeds the world model, and the world model never waits for it. When the follower falls behind, it skips to the newest state and reports the lag. Its service is `required: false`: it must start before the closed loop runs, but a later failure is a warning, never a Mission Run failure. Its engine settings capture at 960×540, which is the same frustum as the perception settings at a quarter of the pixels. One beat costs about 0.7 s of wall time for 0.5 s of Mission time.

**Perception ideal/yolo: the scene clock.** The physical runtime's `SceneClock` owns AirSim time. The perception producer feeds the world model, which is unchanged from ADR 0015. The Run Worker's read-only `CameraCapture` additionally publishes a Perception Annotation: the front camera frame with that producer's output for the same frame.

- **YOLO.** Boxes come from `perception_audit.jsonl`. A row is matched to a frame by its `airsim_image_timestamp_ns`, falling back to its Mission time.
- **Ideal perception.** It writes no audit. Boxes outline only the ships its `entity.observed` events reported, taken from the frame's own segmentation.
- **Timing.** A sample matches only when its Mission time trails the frame by 0 to 0.25 s. That is the scene clock's admitted sensor lag.
- **No match.** A frame without a matching sample says so. Older boxes are never moved onto a newer scene.

Both mechanisms write the same files under `world-frames/` through `CameraFrameStore`:

- `camera_front`, `camera_third_person` and `camera_front_annotated` generations;
- `camera-capture.json`;
- an `airsim.json` status object (mode, perception, state, frame and world Mission time, lag, ship phase error, annotation).

The Host serves these as the v1.3 `world.airsim` object and the `camera_front_annotated` frame source.

## Consequences

- `mission1-harbor` supports AirSim on with perception off. `mission1-airsim` supports perception `off`, `ideal` and `yolo`. Perception still requires AirSim.
- An annotated frame always carries a disclosure strip naming the box provenance: ideal segmentation (not agent perception), YOLO detections, or ideal-perception reports.
- AirSim frames in follower mode are visualization only. Canonical FoV, observations, events and planning remain world-model-authoritative.
- Host API minor 3 adds the frame source and status object. The console still accepts a v1.2 Host, which simply has neither.
