#!/usr/bin/env sh
# Serves the presentation for access through an SSH port forward.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8788}
PAGE=mission1_methodology_pitch.html

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  printf 'Error: python3 or python is required to serve the presentation.\n' >&2
  exit 1
fi

printf 'Serving methodology pitch at http://%s:%s/%s\n' "$HOST" "$PORT" "$PAGE"
printf 'SSH forward example: ssh -L %s:127.0.0.1:%s <user>@<server>\n' "$PORT" "$PORT"
exec "$PYTHON" -m http.server "$PORT" --bind "$HOST" --directory "$HERE"
