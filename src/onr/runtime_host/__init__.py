"""Public Runtime Host API.

Exports resolve lazily so light subpackages (``onr.runtime_host.stack``, used by
the herdr shell launcher) import without loading the FastAPI application.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from onr.runtime_host.app import create_app
    from onr.runtime_host.host import (
        RuntimeHost,
        RuntimeWorkerOptions,
        WorkerContext,
        runtime_worker,
    )

_EXPORTS = {
    "RuntimeHost": "onr.runtime_host.host",
    "RuntimeWorkerOptions": "onr.runtime_host.host",
    "WorkerContext": "onr.runtime_host.host",
    "create_app": "onr.runtime_host.app",
    "runtime_worker": "onr.runtime_host.host",
}

__all__ = [
    "RuntimeHost",
    "RuntimeWorkerOptions",
    "WorkerContext",
    "create_app",
    "runtime_worker",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)
