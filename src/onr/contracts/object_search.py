"""Typed, immutable Mission 4 accumulated belief results."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchMatch:
    target_id: str
    track_id: str
    probability: float | None
    position: tuple[float, float, float]
    position_uncertainty_m: float | None
    observed_at_s: float
    supporting_observation_ids: tuple[str, ...]
    found: bool
    reason: str | None

    @property
    def uncertainty(self) -> float | None:
        return None if self.probability is None else 1-self.probability


@dataclass(frozen=True, slots=True)
class SearchBeliefSnapshot:
    mission_id: str
    request_revision: int
    belief_revision: int
    matches: tuple[SearchMatch, ...]
