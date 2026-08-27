#!/usr/bin/env sh
# Start the local operator console and bootstrap its Runtime Host.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

if ! command -v conda >/dev/null 2>&1; then
  printf 'Error: conda is required to activate the onr environment.\n' >&2
  exit 1
fi

. "$(conda info --base)/etc/profile.d/conda.sh"
conda activate onr

exec cargo run --manifest-path "$ROOT/operator-console/Cargo.toml" -- --bootstrap-host "$@"
