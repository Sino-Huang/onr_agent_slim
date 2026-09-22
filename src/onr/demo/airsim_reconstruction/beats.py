"""Stepped-beat advancement for the Harbor reconstruction."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

LANDING_TOLERANCE_S = 0.25
LANDING_MARGIN_S = 0.10
# Capture load inflated a 43 ms final step to 437 ms wall (~10.3x). Keep
# stepped work away from the final 0.7 s, then use pause-latency-bounded polling.
COARSE_BUFFER_S = 2.0
POLL_BAND_S = 0.7
FINE_AIM_S = 0.45
MICRO_STEP_S = 0.15
STEP_CHUNK_S = 10.0
# 48 x STEP_CHUNK_S covers a single jump across the full ~360 s scenario
# (stills captures start at an arbitrary tick instead of 0.5 s beats).
MAX_ITERATIONS = 48
POLL_AIM_S = 0.20
POLL_SLEEP_S = 0.03
POLL_TIMEOUT_S = 5.0
MAX_POLLS = 200


def beat_landing_passed(errors_s: Mapping[str, float]) -> bool:
    """Return whether every ship landed within the half-tick honesty bound."""
    return bool(errors_s) and all(
        abs(float(error_s)) <= LANDING_TOLERANCE_S
        for error_s in errors_s.values()
    )


def _read(read_phases: Callable[[], Mapping[str, float]]) -> dict[str, float]:
    phases = {str(key): float(value) for key, value in read_phases().items()}
    if not phases:
        raise ValueError("read_phases returned no ships")
    return phases


def advance_to_phase(
    freeze: Any,
    read_phases: Callable[[], Mapping[str, float]],
    target_phase_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Advance a paused scene to a target using bounded server-side steps."""
    started = monotonic()
    target = float(target_phase_s)
    phases = _read(read_phases)
    polls = 1
    minimum = min(phases.values())
    if minimum >= target - LANDING_MARGIN_S:
        return {
            "target_phase_s": target,
            "landing_errors_s": {
                ship_id: phase - target for ship_id, phase in phases.items()
            },
            "rate_estimate": None,
            "wall_s": float(monotonic() - started),
            "polls": polls,
            "early_return": True,
            "iterations": [],
            "max_iterations_reached": False,
            "poll_iterations": 0,
        }

    iterations: list[dict[str, float]] = []
    for _ in range(MAX_ITERATIONS):
        remaining = target - minimum
        if remaining <= POLL_BAND_S:
            break
        if remaining > COARSE_BUFFER_S:
            requested_s = min(
                remaining - COARSE_BUFFER_S, STEP_CHUNK_S
            )
        else:
            requested_s = min(remaining - FINE_AIM_S, MICRO_STEP_S)
        freeze.step(requested_s)
        phases = _read(read_phases)
        polls += 1
        minimum = min(phases.values())
        iterations.append(
            {
                "requested_s": float(requested_s),
                "landed_min_phase_s": float(minimum),
            }
        )
    remaining = target - minimum
    max_iterations_reached = (
        len(iterations) >= MAX_ITERATIONS and remaining > POLL_BAND_S
    )
    poll_iterations = 0
    if not max_iterations_reached:
        freeze.resume()
        poll_started = monotonic()
        while True:
            phases = _read(read_phases)
            polls += 1
            poll_iterations += 1
            minimum = min(phases.values())
            remaining = target - minimum
            if remaining <= POLL_AIM_S:
                freeze.pause()
                break
            if (
                monotonic() - poll_started >= POLL_TIMEOUT_S
                or poll_iterations >= MAX_POLLS
            ):
                freeze.pause()
                raise TimeoutError("beat poll approach exceeded safety limit")
            sleep(POLL_SLEEP_S)
        phases = _read(read_phases)
        polls += 1
    return {
        "target_phase_s": target,
        "landing_errors_s": {
            ship_id: phase - target for ship_id, phase in phases.items()
        },
        "wall_s": float(monotonic() - started),
        "polls": polls,
        "early_return": False,
        "iterations": iterations,
        "max_iterations_reached": max_iterations_reached,
        "poll_iterations": poll_iterations,
    }
