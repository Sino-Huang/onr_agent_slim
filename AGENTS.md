## Environment

We are using `onr` conda environment, please always activate this conda environment before running commands. If the environment is missing packages, install it and update the `requirements.txt` file.

### Live-demo launchers

The `scripts/live_demo_with_wm/*.sh` launchers need `jq` and `python` in their own shell. `jq` is vendored at `modules/jq/jq` and the launcher prepends it to `PATH` automatically (the hyper agent's shell backend sources jq the same way). `python` and `herdr` must come from the caller's environment, so run the launchers with the `onr` conda environment activated; the launcher fails fast with an explicit message when a required tool is missing.

## Agent skills

### Issue tracker

Issues are tracked in this repository's GitHub Issues using the `gh` CLI on `Sino-Huang/onr_agent_slim`. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical triage roles use the default five GitHub label strings. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain documentation layout. See `docs/agents/domain.md`.
