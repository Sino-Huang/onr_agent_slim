"""Per-Mission-Run root directory layout (issue #75, decision D3).

Every Mission Run owns ``<runs_root>/<mission_run_id>/`` with the same layout as
the herdr live-demo launcher, so ``scripts/audit_live_demo.py --run-root`` works
on Host runs unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunRoot:
    """Paths inside one Mission Run root; constructing it touches no files."""

    path: Path

    @classmethod
    def for_run(cls, runs_root: Path, mission_run_id: str) -> RunRoot:
        if (
            not mission_run_id
            or Path(mission_run_id).name != mission_run_id
            or mission_run_id in {".", ".."}
        ):
            raise ValueError("mission run ID must be one path component")
        return cls(Path(runs_root) / mission_run_id)

    def create(self) -> RunRoot:
        """Create the run root and its service-log directory."""

        self.services_dir.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def worker_log(self) -> Path:
        return self.path / "worker.log"

    @property
    def services_dir(self) -> Path:
        return self.path / "services"

    def service_log(self, name: str) -> Path:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("service name must be one path component")
        return self.services_dir / f"{name}.log"

    @property
    def stack_plan(self) -> Path:
        return self.path / "stack.json"

    @property
    def stack_status(self) -> Path:
        return self.path / "stack-status.json"

    @property
    def transport(self) -> Path:
        return self.path / "transport"

    @property
    def agent_storage(self) -> Path:
        return self.path / "agent-storage"

    @property
    def operational_log(self) -> Path:
        return self.agent_storage / "operational-log"

    @property
    def planner_artifacts(self) -> Path:
        return self.path / "planner-artifacts"

    @property
    def environment_artifacts(self) -> Path:
        return self.path / "environment-artifacts"

    @property
    def physical_state(self) -> Path:
        return self.path / "physical-state"

    @property
    def engine(self) -> Path:
        return self.path / "engine"

    @property
    def perception(self) -> Path:
        return self.path / "perception"

    @property
    def world_frames(self) -> Path:
        return self.path / "world-frames"

    @property
    def latest_world_frame(self) -> Path:
        return self.world_frames / "latest.png"

    @property
    def closed_loop_result(self) -> Path:
        return self.path / "closed-loop-result.json"

    @property
    def agent_params(self) -> Path:
        return self.path / "onr_agent_params.yaml"

    @property
    def environment_profile(self) -> Path:
        return self.path / "environment_physical.yaml"


__all__ = ["RunRoot"]
