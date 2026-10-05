# AirSim Multi-Mission Demo Video Workflow

Use this workflow to turn one accepted Mission Run into an AirSim-augmented replay. The video is an offline reconstruction of that run, not a second execution or a perception evaluation. Run the Python commands after activating the repository’s `onr` environment.

> **Current visualization defaults — keep them for every new video.**
>
> - **Smooth turning (capture, always on).** Capture never replays the recorded cardinal heading verbatim; `smooth_yaw_profile` rate-limits yaw at 45°/s with 1 s pre-yaw, mirroring the live AirSync engine. See §5. A capture directory made before this change snaps the aircraft 90° per turn — recapture, never `--resume` into it.
> - **256 m windowed world pane (derive default).** `derive_joint34_video_bundle.py` defaults to `--pane windowed`: a drone-following 256 m window at 2 m cells. The 2 km `overview` was too coarse to read targets and the 128 m `local` window too tight. See §4.
> - **12 000 kbps VP8 render (render default).** Lower bitrates fail the validator's hold-frame gate on dense harbor content. See §6.

## Reuse boundary

| Reusable media stage | Mission-combination-specific stage |
| --- | --- |
| AirSim fixture manifest and object mapping; three-stream capture; capture closeout; video composition; seven-gate validation | Runtime support and acceptance audit; run-derived timeline, metrics, storyboard, world-pane evidence, and profile |

`src/onr/demo/airsim_reconstruction/render.py` and `validate.py` consume the derived bundle and a `MissionProfile`. Keep their input contract stable. `scripts/derive_joint34_video_bundle.py` is specifically Joint34: it requires `joint34-complete`, a Mission 3 selection, and Mission 4 package/fixture inputs. Do not use it unchanged for M1+M3 or M1+M4. First verify that the runtime can execute and audit that pairing; then reuse the capture/render/validation stages and provide the pairing-specific bundle and profile. The current built-in profiles cover Mission 1 and Joint34.

For live-run launch and audit conventions, see [live-demo-missions-2-4.md](live-demo-missions-2-4.md). Issue #71 records the accepted Joint34 baseline and its limits; issue #72 covers the resolved Mission 3 inspection follow-up.

## 1. Select one accepted run

- Use a terminal, audited run. Keep its run ID, configuration, Mission inputs, recorded observations, and answer/package artifacts together; never combine data from different runs.
- Keep the final outcome honest. Record inconclusive Mission 3 vessels as unresolved. When Mission 4 `search_area` is involved, prove physical ingress from maneuver feedback; coverage or `all_found` alone does not prove entry.
- Record the run duration and compute `LAST_TICK = round(duration_seconds / 0.5)`. The captured mission ticks are `0..LAST_TICK`.

## 2. Align the AirSim fixture to run coordinates

The world pane follows recorded runtime NED telemetry. AirSim vessel actors follow fixture trajectories. Compare source trajectory rows and fixture rows at matching mission times (including the fixture lead-in) before trusting a camera frame.

`src/onr/demo/airsim_reconstruction/fixture.py` exposes `--trajectory-ned-offset NORTH EAST DOWN`. Its default `[253.7, 45.5, 0] m` is retained for the canonical Mission 1 reconstruction. Use an explicit offset for each new pairing: use zero only when the source tracks already share the run’s NED frame. The Joint34 harbor vessel files do; applying the Mission 1 offset moved vessels without moving the recorded drone. Confirm the fixture manifest records the intended offset and compare representative, then all canonical, trajectory rows.

Keep fixture truth, visible labels, object IDs, and `mapping.json` in agreement. If scene actors or meshes change, create a fresh sibling fixture and run a certification that actually covers that fixture; a receipt for another scenario is not evidence for it. Keep map alignment corrections in the source/map or fixture transform, never in a render-only offset.

## 3. Derive the pairing-specific bundle

Build a fresh, non-existing sibling tree, for example `var/demo-video/<pair>-<date>-airsim/`, so accepted runs, fixtures, captures, and videos remain untouched. The bundle must provide:

- `frame-metadata.json`: recorded mission time and NED pose per tick;
- `mission-metrics.json`, `story.json`, and `video-profile.json`: pairing-specific recorded outcomes and timing;
- `world-frames/`: the time-gated world pane; and
- the fixture `mapping.json` used to interpret segmentation IDs.

For a new pairing, supply a pairing-specific derivation/profile only when the existing bundle/profile cannot represent its runtime evidence and outcomes; keep pair-specific logic out of the common renderer. Set the inputs below from the run's launcher and audit receipts. Joint34 invocation:

```bash
# Use unused output paths and inputs resolved for this Joint34 run.
ROOT=var/demo-video/pair-YYYYMMDD-airsim
RUN=/path/to/accepted-run
SCENARIO_CONFIG=/path/to/joint34_demo.yaml
M3_SELECTION=/path/to/mission3-selection.json
M4_PACKAGE=/path/to/mission4-package.json
M4_FIXTURE=/path/to/mission4-fixture.json
python scripts/derive_joint34_video_bundle.py \
  --pane windowed \
  --run "$RUN" \
  --scenario-config "$SCENARIO_CONFIG" \
  --mission3-selection "$M3_SELECTION" \
  --mission4-package "$M4_PACKAGE" \
  --mission4-fixture "$M4_FIXTURE" \
  --output "$ROOT/bundle"
```

Add `--surface-alignment "$ALIGNMENT"` when the paired pose/surface audit JSON exists. The derived profile must load through `load_profile_from_file`; a similarly named but incompatible JSON is not sufficient.

Populate overlays only from evidence available at or before each frame. Gate active search polygon/path/progress to the active `search_area` maneuver. Keep static AOI separate from the active search boundary, planned path, and actual track. Do not render private fixture truth or future evidence as agent knowledge.

Joint34 frame derivation reads the run's state observations (0.5 s through the
terminal tick), world-model publications (0 s through the preceding tick),
accepted maneuver commands, and per-tick maneuver feedback. The first recorded
state supplies the tick-0 pose; routes are recomputed from each recorded pose.
Search feedback does not publish its transient frontier target, so the search
route is an approximation toward the recorded polygon's centroid. Search phase
and coverage progress, however, are taken directly from feedback; see the
bundle's `reconstruction-receipt.json` for the derivation boundary.

## 4. Choose the world-pane scale intentionally

The Joint34 runs behind the existing videos used a 2 m runtime grid, 64-cell partition (128 m per side), and a 15 m visibility cap. The Joint34 scenario now uses 256-cell (512 m) partitions, a 300 m camera range for ships and a 100 m small-object range (`onr_physical_runtime` README, "Camera range and partitions"); re-derive bundles from runs recorded with those settings. The derive script offers three projections:

| `--pane` | Coverage | Cell / tile | Use |
| --- | --- | --- | --- |
| `windowed` (**default**) | 256 m, follows the drone | 2 m / 4 px | Every new video: targets, AOI, and search coverage stay readable with surrounding harbor context |
| `overview` | 2 km, fixed north-up | 7.8125 m / 2 px | Whole-harbor context only; a 15 m footprint is about 3 px in radius, which is a rendering limit, not a runtime visibility change |
| `local` | 128 m runtime partition | 2 m / 8 px | Comparing the visibility footprint against the runtime's 128 m partition of those runs; too tight for general viewing. The live World source now shows the runtime's 512 m partition |

Distinguish sensor visibility/fog from Mission 4 dock-search coverage: they are different quantities.

## 5. Capture every tick and close it out

Start the matching AirSim scene/fixture and use the same run’s frame metadata and observation directory:

```bash
ROOT=var/demo-video/pair-YYYYMMDD-airsim
RUN=/path/to/accepted-run # replace with the audited run directory
LAST_TICK=599 # replace with round(duration_seconds / 0.5) for this run

python -m onr.demo.airsim_reconstruction.capture \
  --fixture "$ROOT/fixture" \
  --output "$ROOT/capture" \
  --start-tick 0 \
  --stop-tick "$((LAST_TICK + 1))" \
  --frame-metadata "$ROOT/bundle/frame-metadata.json" \
  --observations "$RUN/physical-state/observations"
```

`--stop-tick` is exclusive. The command above requests `LAST_TICK + 1` ticks and requires `front-rgb`, `front-seg`, and `third-rgb` for each tick. A resumed capture is complete only when its manifest contains the full contiguous range and all streams; use the closeout rather than trusting a partial command summary.

Capture does not replay the recorded heading verbatim. State observations carry the world model's cardinal grid heading, so a quarter turn would snap the aircraft 90° between two ticks. `smooth_yaw_profile` in `capture.py` applies the live AirSync engine's turning scheme instead (rate-limited yaw that starts `PRE_YAW_TIME_S` before the recorded corner, as in `onr_physical_runtime`'s `SyncEngineConfig`) at 45°/s, so each captured tick rotates at most 22.5°. Positions are unchanged. Capture from before this change must be redone to pick it up; do not `--resume` into an old capture directory.

```bash
python scripts/author_capture_closeout.py \
  --capture-dir "$ROOT/capture" \
  --ticks "$((LAST_TICK + 1))"
```

The closeout recomputes image hashes, timestamps, aircraft pose/heading errors, segmentation IDs, and capture-beat landing tolerances. Resolve any gap before rendering.

## 6. Render and run all video gates

```bash
VIDEO="$ROOT/output/airsim-augmented.webm"

python -m onr.demo.airsim_reconstruction.render \
  --profile-file "$ROOT/bundle/video-profile.json" \
  --capture-manifest "$ROOT/capture/capture-manifest.json" \
  --mapping "$ROOT/fixture/mapping.json" \
  --metadata "$ROOT/bundle/frame-metadata.json" \
  --metrics "$ROOT/bundle/mission-metrics.json" \
  --storyboard "$ROOT/bundle/story.json" \
  --world-frames "$ROOT/bundle/world-frames" \
  --run "$RUN" \
  --ticks "0-$LAST_TICK" \
  --output "$VIDEO"

python -m onr.demo.airsim_reconstruction.validate \
  --video "$VIDEO" \
  --receipt "${VIDEO%.webm}.receipt.json" \
  --capture-manifest "$ROOT/capture/capture-manifest.json" \
  --metadata "$ROOT/bundle/frame-metadata.json" \
  --metrics "$ROOT/bundle/mission-metrics.json" \
  --storyboard "$ROOT/bundle/story.json" \
  --profile-file "$ROOT/bundle/video-profile.json"
```

The validator’s seven gates cover full decode, browser playback/seek, editorial pause/freeze, metric timing, disclosure strings, accepted-command presence, and artifact retention. Pass the matching receipt explicitly when rendering to a non-default path. Render encodes at `DEFAULT_BITRATE_KBPS` (12 000); do not lower `--bitrate-kbps`, since the hold-frame gate fails on harbor content at 3000.

## 7. Review and record the deliverable

Review decoded frames at the actual inset size for the pairing’s M1/M3/M4 maneuvers, requests, target findings, and terminal report. When Mission 4 is present, include first AOI entry and interior search/coverage progress. Confirm the map/overlay scale is readable and every label is time-correct; validator PASS does not replace this visual review.

Before closing the issue, record the Mission Run ID and audit, relevant receipts, unresolved outcomes, fixture and capture hashes, capture closeout, video SHA-256, validator JSON, frame count, and perception/map limitations. State which evidence is simulated. Preserve prior approved artifact trees byte-for-byte.

## Source entry points

- `src/onr/demo/airsim_reconstruction/fixture.py`
- `src/onr/demo/airsim_reconstruction/capture.py`
- `scripts/author_capture_closeout.py`
- `scripts/derive_joint34_video_bundle.py` (Joint34-specific)
- `src/onr/demo/airsim_reconstruction/render.py`
- `src/onr/demo/airsim_reconstruction/validate.py`
- `src/onr/demo/airsim_reconstruction/profile.py`
