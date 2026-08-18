"""Public Mission Memory seam for a single Mission/role episode."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MissionRoleContext:
    """Expose only scoped Mission Memory to a role episode."""

    mission_id: str
    role: str
    memory_store: object

    def read_memory(self) -> str | None:
        read = getattr(self.memory_store, "read", None)
        if not callable(read):
            raise TypeError("Mission Memory store must expose read")
        value = read(self.mission_id, self.role)
        if value is not None and not isinstance(value, str):
            raise TypeError("Mission Memory store returned non-text contents")
        return value

    def write_memory(self, contents: str) -> None:
        write = getattr(self.memory_store, "write", None)
        if not callable(write):
            raise TypeError("Mission Memory store must expose write")
        write(self.mission_id, self.role, contents)


@dataclass(slots=True)
class RoleEpisode:
    """Delegate a Deep Agent while retaining its public role-context seam."""

    agent: object
    context: MissionRoleContext

    def invoke(self, *args: object, **kwargs: object) -> object:
        invoke = getattr(self.agent, "invoke", None)
        if not callable(invoke):
            raise TypeError("Deep role agent must expose invoke")
        return invoke(*args, **kwargs)

    def read_memory(self) -> str | None:
        return self.context.read_memory()

    def write_memory(self, contents: str) -> None:
        self.context.write_memory(contents)

    def __getattr__(self, name: str) -> object:
        return getattr(self.agent, name)


__all__ = ["MissionRoleContext", "RoleEpisode"]
