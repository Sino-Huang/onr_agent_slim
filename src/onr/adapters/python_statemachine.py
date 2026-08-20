"""python-statemachine adapter for validated declarative Statecharts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from statemachine import State, StateMachine

from onr.contracts.fsm import Statechart


def _attribute(prefix: str, index: int, value: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_") or "value"
    return f"{prefix}_{index}_{suffix}"


@dataclass(slots=True)
class PythonRunningStateMachine:
    machine: StateMachine
    event_attributes: dict[str, str]

    @property
    def current_state(self) -> str:
        return str(self.machine.current_state_value)

    @property
    def allowed_events(self) -> tuple[str, ...]:
        allowed_attributes = {event.id for event in self.machine.allowed_events}
        return tuple(
            event
            for event, attribute in self.event_attributes.items()
            if attribute in allowed_attributes
        )

    def send(self, event: str) -> None:
        try:
            attribute = self.event_attributes[event]
        except KeyError as exc:
            raise ValueError("Statechart event is not declared") from exc
        self.machine.send(attribute)


class PythonStateMachineFactory:
    """Build one dynamic python-statemachine class from immutable topology."""

    def build(
        self, statechart: Statechart, *, start_state: str | None = None
    ) -> PythonRunningStateMachine:
        if not isinstance(statechart, Statechart):
            raise TypeError("State machine construction requires a Statechart")
        selected_start = statechart.entry_state if start_state is None else start_state
        if selected_start not in statechart.states:
            raise ValueError("State machine start state is not declared")

        state_attributes: dict[str, str] = {}
        attributes: dict[str, Any] = {}
        states: dict[str, State] = {}
        for index, state_id in enumerate(statechart.states):
            attribute = _attribute("state", index, state_id)
            state_attributes[state_id] = attribute
            state = State(
                name=state_id,
                value=state_id,
                initial=state_id == statechart.entry_state,
                final=state_id in statechart.terminal_states,
            )
            states[state_id] = state
            attributes[attribute] = state

        event_attributes: dict[str, str] = {}
        for index, transition in enumerate(statechart.transitions):
            attribute = _attribute("event", index, transition.event)
            event_attributes[transition.event] = attribute
            attributes[attribute] = states[transition.source].to(
                states[transition.target]
            )

        machine_type = type(
            f"Statechart_{statechart.plan_revision}",
            (StateMachine,),
            attributes,
        )
        machine = machine_type(start_value=selected_start)
        running = PythonRunningStateMachine(machine, event_attributes)
        if running.current_state != selected_start:
            raise RuntimeError("python-statemachine did not restore the requested state")
        return running


__all__ = ["PythonRunningStateMachine", "PythonStateMachineFactory"]
