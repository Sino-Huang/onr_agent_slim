"""Production Runtime Host factory with an isolated deterministic Run Worker."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from onr.runtime import (
    HeartbeatsConfig,
    LLMConfig,
    PlannerConfig,
    PlannersConfig,
    RuntimeConfig,
    ServicesConfig,
    StorageConfig,
    TransportConfig,
)
from onr.runtime.config import DEFAULT_ENVIRONMENT_PROFILE
from onr.runtime_host.app import create_app
from onr.runtime_host.host import RuntimeHost, WorkerContext


def stub_worker(context: WorkerContext) -> None:
    """Exercise real Host worker lifecycle without launching the environment stack."""
    context.run_root.agent_storage.mkdir(parents=True, exist_ok=True)
    (context.run_root.agent_storage / "interop-worker-ran").write_text(
        context.mission_run_id, encoding="utf-8"
    )


def main() -> None:
    port = int(sys.argv[1])
    root = Path(sys.argv[2]).resolve()
    environment = replace(
        DEFAULT_ENVIRONMENT_PROFILE,
        fake=replace(
            DEFAULT_ENVIRONMENT_PROFILE.fake, artifact_root=root / "environment"
        ),
    )
    config = RuntimeConfig(
        llm=LLMConfig("openai", "http://127.0.0.1:1/v1", "offline", "EMPTY", 0),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1),
            PlannerConfig(Path(__file__), 1, Path(__file__)),
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", root / "transport"),
        storage=StorageConfig(root / "storage"),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
        debug=True,
        agent_name="interop-fixture",
        environment_profile=environment,
    )
    counts: dict[str, int] = {}

    def generate_id(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"{kind}-{counts[kind]}"

    host = RuntimeHost(
        config,
        clock=lambda: datetime.now(UTC).isoformat(),
        generate_id=generate_id,
        worker_entrypoint=stub_worker,
        runs_root=root / "runs",
    )
    uvicorn.run(create_app(host=host), host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    main()
