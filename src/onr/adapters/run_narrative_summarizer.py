"""Chat-model adapter for the optional public Run Narrative projection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# The Narrative Input is already bounded (latest summaries plus run state);
# this is a safety net, not the normal limit.
DEFAULT_MAX_PROMPT_CHARACTERS = 64_000


class RunNarrativeSummarizationError(RuntimeError):
    """The configured chat model could not produce a Run Narrative."""


class ModelRunNarrativeSummarizer:
    """Invoke one chat model with the Run Narrative input only (D6)."""

    def __init__(
        self,
        model: Any,
        *,
        max_prompt_characters: int = DEFAULT_MAX_PROMPT_CHARACTERS,
    ) -> None:
        if max_prompt_characters < 1:
            raise ValueError("maximum prompt characters must be positive")
        self.model = model
        self.max_prompt_characters = max_prompt_characters

    def _prompt(
        self,
        *,
        mission_id: str,
        terminal: bool,
        narrative_input: Mapping[str, object],
    ) -> str:
        try:
            serialized = json.dumps(
                narrative_input,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RunNarrativeSummarizationError(
                "Run Narrative input is not JSON serializable"
            ) from exc
        prompt = (
            "Write the Run Narrative for this Mission Run: a short operator-facing "
            "account of what has happened and what is happening now, in at most "
            "six sentences. Build on PREVIOUS_NARRATIVE when present. The "
            "window summaries are model-written and non-authoritative; the run "
            "state, phase, FSM state and live records are authoritative and win "
            "on conflict. Do not invent events. Return only the narrative text.\n"
            f"MISSION_ID: {mission_id}\n"
            f"TERMINAL: {str(terminal).lower()}\n"
            "NARRATIVE INPUT (latest Mission Log Summaries plus current run state):\n"
            f"{serialized}"
        )
        if len(prompt) > self.max_prompt_characters:
            raise RunNarrativeSummarizationError("Run Narrative prompt is too large")
        return prompt

    @staticmethod
    def _response_text(response: object) -> str:
        value = (
            response
            if isinstance(response, str)
            else getattr(response, "content", None)
        )
        if not isinstance(value, str) or not value.strip():
            raise RunNarrativeSummarizationError("Run Narrative model returned no text")
        return value.strip()

    def summarize_narrative(
        self,
        *,
        mission_id: str,
        mission_run_id: str,
        terminal: bool,
        narrative_input: Mapping[str, object],
    ) -> str:
        del mission_run_id
        prompt = self._prompt(
            mission_id=mission_id,
            terminal=terminal,
            narrative_input=narrative_input,
        )
        invoke = getattr(self.model, "invoke", None)
        if not callable(invoke):
            raise RunNarrativeSummarizationError(
                "configured Run Narrative model has no invoke method"
            )
        try:
            response = invoke(
                prompt,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            return self._response_text(response)
        except RunNarrativeSummarizationError:
            raise
        except Exception as exc:
            raise RunNarrativeSummarizationError(
                "Run Narrative model invocation failed"
            ) from exc


__all__ = [
    "ModelRunNarrativeSummarizer",
    "RunNarrativeSummarizationError",
]
