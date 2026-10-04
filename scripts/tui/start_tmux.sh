#!/usr/bin/env sh
# Create or attach the Operator Console tmux session with image passthrough.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SESSION=${1:-onr-tui}

if ! command -v conda >/dev/null 2>&1; then
    printf 'Error: conda is required to locate the tmux environment.\n' >&2
    exit 1
fi
TMUX_BIN=$(conda info --base)/envs/tmux/bin
if [ ! -x "$TMUX_BIN/tmux" ]; then
    printf 'Error: tmux >= 3.3 is required for pixel graphics. Install it with:\n  conda create -n tmux -c conda-forge --override-channels tmux\n' >&2
    exit 1
fi

# Panes inherit this PATH, so `tmux` inside the session talks to this server
# version rather than an older system tmux.
PATH=$TMUX_BIN:$PATH
export PATH
export TMUX_TMPDIR="$ROOT/var/tmp"
mkdir -p "$TMUX_TMPDIR"
cd "$ROOT"
exec tmux -f "$ROOT/scripts/tui/tmux.conf" new-session -A -s "$SESSION"
