# Mission 3 selected-fleet inspection

For the terminal-audited vLLM/runtime launcher, see
[Model-backed live demos for Missions 2–4](live-demo-missions-2-4.md).

Mission 3 consumes the physical runtime's versioned `world_model_info.mission3`
section. The normalized `selected_ship_ids` roster is authoritative for the round;
incidental visible ships never expand it. `target_observations` contains only public
camera/GPS-derived estimates and drives travel decisions. Fixture truth and reveal
requirements never enter Agent context.

The code-owned advisory policy chooses the nearest feasible unobserved ship for
screening, using `navigate` for a public position approach or `pursue` for a current
camera sighting. Usable inconclusive/suspicious evidence immediately adds
`investigate`, so deeper service can be interleaved within a visit. After one
inconclusive orbit it screens other feasible ships before one bounded revisit.
Meaningful target movement, new evidence and terminal failures can trigger a replan;
identical snapshots produce no command. A failed target remains deferred until its
evidence or public target sample changes.

Only a producer's sufficient `normal` or `abnormal` verdict resolves a ship. Either
verdict permits the next maneuver to replace/cancel an active investigation. Orbit
completion, no detection, unavailable perception and missing confidence remain
unresolved. At the Mission-description budget, recording end, or when no bounded
recovery remains, the report lists every selected ship, verdict evidence and explicit
incomplete reason. Mission 1 reporting belief and its 10% replan gate are not created
for Mission 3; Mission 2 predictions are not required.

## Mission description and launcher

`examples/mission3_description.json` is the structured source for optional
`target_ids`, inclusive starting-position NED `area`, and `mission_time_budget_s`.
The launcher passes that same file to the physical runtime, which normalizes the
filters once. Omitting both filters selects the whole declared fleet; an explicit
empty list remains empty.

Validate the real two-process launcher without starting services:

```bash
ONR_DEMO_DRY_RUN=1 \
ONR_DEMO_MISSION_MODE=mission3 \
ONR_DEMO_SCENARIO_CONFIG=/data/ccu/sukaih/ONR/onr_physical_runtime/config/mission3_smoke/scenario.yaml \
ONR_DEMO_MISSION3_DESCRIPTION=/data/ccu/sukaih/ONR/onr_agent_slim/examples/mission3_description.json \
ONR_DEMO_MISSION3_FIXTURE=/data/ccu/sukaih/ONR/onr_physical_runtime/config/mission3_smoke/private_fixture.json \
bash scripts/live_demo_with_wm/herdr_start_live_demo.sh onr
```

Remove `ONR_DEMO_DRY_RUN` to run the configured Agent model against the deterministic
standalone producer. The two herdr panes stream physical and Agent progress; the dry
run prints both exact commands and the isolated run directory. Fixture runs validate
behavioral integration, not model or live-perception quality. Omitting
`ONR_DEMO_MISSION3_FIXTURE` selects the live boundary, whose producer/camera setup and
real acceptance remain workstream #26.

## Bounded policy inspection

To inspect one captured public environment payload without launching a model:

```bash
conda activate onr
python -m onr.application.mission3_planning \
  /path/to/environment.json \
  --output /tmp/mission3-decision.json \
  --dry-run
```

Dry-run prints the source label, mission time and proposed action/reason without
writing the output. Removing `--dry-run` writes the auditable decision artifact. The
deterministic policy and test fixtures are explicitly mocked decision evidence; they
do not establish end-to-end model quality or live camera acceptance.
