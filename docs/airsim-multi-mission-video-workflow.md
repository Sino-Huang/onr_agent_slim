# AirSim Multi-Mission Demo Video Workflow

Use this workflow to turn one accepted Mission Run into an AirSim-augmented replay. The video is an offline reconstruction of that run, not a second execution or a perception evaluation. Run the Python commands after activating the repository’s `onr` environment.

## Reuse boundary

| Reusable media stage | Mission-combination-specific stage |
| --- | --- |
| AirSim fixture manifest and object mapping; three-stream capture; capture closeout; video composition; seven-gate validation | Runtime support and acceptance audit; run-derived timeline, metrics, storyboard, world-pane evidence, and profile |

`src/onr/demo/airsim_reconstruction/render.py` and `validate.py` consume the derived bundle and a `MissionProfile`. Keep their input contract stable. `scripts/derive_joint34_video_bundle.py` is specifically Joint34: it requires `joint34-complete`, a Mission 3 selection, and Mission 4 package/fixture inputs. Do not use it unchanged for M1+M3 or M1+M4. First verify that the runtime can execute and audit that pairing; then reuse the capture/render/validation stages and provide the pairing-specific bundle and profile. The current built-in profiles cover Mission 1 and Joint34.

For live-run launch and audit conventions, see [`live-demo-missions-2-4.md`](live-demo-missions-2-4.md). The Joint34 example and its limitations are recorded in [issue #71](https://github.com/Sino-Huang/onr_agent_slim/issues/71).

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
  --pane overview \
  --run "$RUN" \
  --scenario-config "$SCENARIO_CONFIG" \
  --mission3-selection "$M3_SELECTION" \
  --mission4-package "$M4_PACKAGE" \
  --mission4-fixture "$M4_FIXTURE" \
  --output "$ROOT/bundle"
```

Add `--surface-alignment "$ALIGNMENT"` when the paired pose/surface audit JSON exists. The derived profile must load through `load_profile_from_file`; a similarly named but incompatible JSON is not sufficient.

Populate overlays only from evidence available at or before each frame. Gate active search polygon/path/progress to the active `search_area` maneuver. Keep static AOI separate from the active search boundary, planned path, and actual track. Do not render private fixture truth or future evidence as agent knowledge.

## 4. Choose the world-pane scale intentionally

The current Joint34 scenario uses a 2 m runtime grid, 64-cell partition (128 m per side), and a 15 m visibility cap. The video’s default `overview` projection instead spans 2 km using 256 cells at 7.8125 m per cell, rendered with two pixels per cell and then resized to 440×440 pixels. The configured range is unchanged, but a 15 m footprint is only about 3 pixels in radius in that overview and its edge is coarser. This is normal for the overview, not evidence that runtime visibility changed.

Use `--pane overview` for harbor context; use `--pane local` when the viewer needs to compare the visibility footprint with the runtime’s partition-local display. Distinguish sensor visibility/fog from Mission 4 dock-search coverage: they are different quantities.

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

The validator’s seven gates cover full decode, browser playback/seek, editorial pause/freeze, metric timing, disclosure strings, accepted-command presence, and artifact retention. Pass the matching receipt explicitly when rendering to a non-default path.

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
