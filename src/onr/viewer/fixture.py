"""Loading and validating deterministic trace fixtures."""

from __future__ import annotations

from pathlib import Path

from onr.viewer.trace import TraceProjection, TraceViewItem


class TraceFixtureLoader:
    def __init__(self, projection: TraceProjection | None = None) -> None:
        self.projection = projection or TraceProjection()

    def load(self, path: Path) -> tuple[TraceViewItem, ...]:
        return self.projection.project_jsonl(Path(path).read_text(encoding="utf-8"))

    def validate(self, path: Path) -> tuple[TraceViewItem, ...]:
        items = self.load(path)
        errors = [item for item in items if item.event_kind == "error"]
        if errors:
            raise ValueError(f"trace fixture contains {len(errors)} invalid record(s)")
        return items


def load_trace_fixture(path: Path | str) -> tuple[TraceViewItem, ...]:
    return TraceFixtureLoader().validate(Path(path))


__all__ = ["TraceFixtureLoader", "load_trace_fixture"]
