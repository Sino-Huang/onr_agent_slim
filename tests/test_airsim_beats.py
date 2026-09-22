from __future__ import annotations

import pytest

from onr.demo.airsim_reconstruction.beats import (
    MAX_ITERATIONS,
    MICRO_STEP_S,
    STEP_CHUNK_S,
    advance_to_phase,
    beat_landing_passed,
)


class _BeatSimulation:
    def __init__(
        self,
        phase: float,
        *,
        step_inflation: float = 1.0,
        scripted_inflation: list[float] | None = None,
        run_rate: float = 1.0,
        pause_latency_s: float = 0.05,
    ) -> None:
        self.now = 0.0
        self.phase = phase
        self.step_inflation = step_inflation
        self.scripted_inflation = iter(scripted_inflation or [])
        self.run_rate = run_rate
        self.pause_latency_s = pause_latency_s
        self.running = False
        self.steps: list[float] = []
        self.pause_calls = 0

    def step(self, seconds: float) -> None:
        assert not self.running
        self.steps.append(seconds)
        try:
            inflation = next(self.scripted_inflation)
        except StopIteration:
            inflation = self.step_inflation
        self.phase += seconds * inflation
        self.now += seconds

    def resume(self) -> None:
        self.running = True

    def pause(self) -> None:
        if self.running:
            self.phase += self.pause_latency_s * self.run_rate
            self.now += self.pause_latency_s
        self.running = False
        self.pause_calls += 1

    def sleep(self, seconds: float) -> None:
        if self.running:
            self.phase += seconds * self.run_rate
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def read_phases(self) -> dict[str, float]:
        return {"1": self.phase, "10": self.phase + 0.01}


def _advance(simulation: _BeatSimulation, target: float = 2.0) -> dict:
    return advance_to_phase(
        simulation,
        simulation.read_phases,
        target,
        sleep=simulation.sleep,
        monotonic=simulation.monotonic,
    )


def test_landing_boundaries() -> None:
    assert beat_landing_passed({"1": -0.25, "10": 0.25})
    assert not beat_landing_passed({"1": 0.250001})


@pytest.mark.parametrize("inflation", [1.0, 2.5, 3.0])
def test_coarse_and_fine_steps_converge_before_poll(inflation: float) -> None:
    simulation = _BeatSimulation(0.0, step_inflation=inflation)

    result = _advance(simulation)

    assert all(step <= MICRO_STEP_S for step in simulation.steps)
    assert result["poll_iterations"] > 0
    assert beat_landing_passed(result["landing_errors_s"])


@pytest.mark.parametrize("pause_latency_s", [0.05, 0.15])
def test_poll_pause_latency_bounds_final_landing(pause_latency_s: float) -> None:
    simulation = _BeatSimulation(0.0, pause_latency_s=pause_latency_s)

    result = _advance(simulation)
    minimum_error = min(result["landing_errors_s"].values())

    assert -0.200001 <= minimum_error <= 0.050001
    assert simulation.pause_calls == 1
    assert beat_landing_passed(result["landing_errors_s"])


def test_final_approach_avoids_scripted_extreme_step_spike() -> None:
    simulation = _BeatSimulation(
        0.0, scripted_inflation=[3.0, 3.0, 3.0, 8.0]
    )

    result = _advance(simulation)

    assert len(simulation.steps) == 3
    assert result["poll_iterations"] > 0
    assert beat_landing_passed(result["landing_errors_s"])


def test_poll_timeout_pauses_then_raises() -> None:
    simulation = _BeatSimulation(1.4, run_rate=0.0)

    with pytest.raises(TimeoutError, match="poll approach"):
        _advance(simulation)

    assert simulation.pause_calls == 1
    assert simulation.running is False


def test_advance_to_phase_already_past_target() -> None:
    simulation = _BeatSimulation(3.0)

    result = _advance(simulation)

    assert result["early_return"] is True
    assert result["poll_iterations"] == 0
    assert simulation.steps == []
    assert simulation.pause_calls == 0


def test_advance_to_phase_chunks_large_gap() -> None:
    simulation = _BeatSimulation(0.0)

    result = _advance(simulation, target=25.0)

    assert simulation.steps[:2] == [STEP_CHUNK_S, STEP_CHUNK_S]
    assert all(step <= STEP_CHUNK_S for step in simulation.steps)
    assert beat_landing_passed(result["landing_errors_s"])


def test_advance_to_phase_stops_at_iteration_cap() -> None:
    simulation = _BeatSimulation(0.0, step_inflation=0.0)

    result = _advance(simulation)

    assert len(simulation.steps) == MAX_ITERATIONS
    assert result["max_iterations_reached"] is True
    assert result["poll_iterations"] == 0
    assert not beat_landing_passed(result["landing_errors_s"])
