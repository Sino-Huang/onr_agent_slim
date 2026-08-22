"""Invocation context shared by agent callbacks and raw LLM recording."""

from __future__ import annotations

from contextvars import ContextVar

_llm_invocation_id: ContextVar[str | None] = ContextVar(
    "onr_llm_invocation_id", default=None
)


def current_llm_invocation_id() -> str | None:
    return _llm_invocation_id.get()


def enter_llm_invocation(invocation_id: str) -> None:
    _llm_invocation_id.set(invocation_id)


def leave_llm_invocation(invocation_id: str) -> None:
    if _llm_invocation_id.get() == invocation_id:
        _llm_invocation_id.set(None)


__all__ = [
    "current_llm_invocation_id",
    "enter_llm_invocation",
    "leave_llm_invocation",
]
