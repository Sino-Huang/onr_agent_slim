"""Repository-local scratch space.

The workstation mounts `/tmp` as a small partition: defaults from
`tempfile` fill it and break unrelated services. All scratch space comes
from the repository's own `var/tmp` tree instead.
"""

from __future__ import annotations

from pathlib import Path


def repo_tmp_root() -> Path:
    """Return the repository's scratch directory, creating it on demand."""
    root = Path(__file__).resolve().parents[2] / "var" / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root
