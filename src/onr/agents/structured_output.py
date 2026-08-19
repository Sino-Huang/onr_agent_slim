"""Deterministic, sanitized recovery for model structured output."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias, TypeVar

from langchain.agents.structured_output import (
    MultipleStructuredOutputsError,
    StructuredOutputValidationError,
)
from langchain_core.messages import HumanMessage


StructuralCode: TypeAlias = Literal[
    "invalid_type",
    "invalid_value",
    "malformed_structured_output",
    "missing_required_field",
    "multiple_structured_outputs",
    "unexpected_field",
]

_FEEDBACK_ISSUE_LIMIT: Final = 8
_LANGCHAIN_MALFORMED_ISSUE: Final = (
    "malformed_structured_output",
    "$",
    "valid structured output",
)
_LANGCHAIN_MULTIPLE_ISSUE: Final = (
    "multiple_structured_outputs",
    "$",
    "exactly one structured output",
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True, order=True)
class StructuralIssue:
    """Stable, safe structural information suitable for model feedback."""

    code: StructuralCode
    path: str
    expected: str


class StructuredOutputFailure(Exception):
    """A parse failure containing only allowlisted structural issues."""

    def __init__(self, issues: Sequence[StructuralIssue]) -> None:
        normalized = tuple(issues)
        if not normalized:
            raise ValueError("at least one structural issue is required")
        self.issues = normalized
        super().__init__("structured output validation failed")


class StructuredOutputRetriesExhausted(Exception):
    """Stable terminal failure after the structured-output retry budget."""

    code: Literal["output_structure_retries_exhausted"] = (
        "output_structure_retries_exhausted"
    )

    def __init__(self, structural_code: StructuralCode) -> None:
        self.structural_code = structural_code
        super().__init__(f"{self.code}: {structural_code}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _langchain_issue(
    error: MultipleStructuredOutputsError | StructuredOutputValidationError,
) -> StructuralIssue:
    values = (
        _LANGCHAIN_MULTIPLE_ISSUE
        if isinstance(error, MultipleStructuredOutputsError)
        else _LANGCHAIN_MALFORMED_ISSUE
    )
    return StructuralIssue(*values)


def _feedback(
    issues: Sequence[StructuralIssue],
    *,
    attempt: int,
    retries_remaining: int,
) -> str:
    normalized = sorted(set(issues))
    selected = normalized[:_FEEDBACK_ISSUE_LIMIT]
    payload: dict[str, object] = {
        "errors": [
            {
                "attempt": attempt,
                "code": issue.code,
                "expected": issue.expected,
                "path": issue.path,
                "retries_remaining": retries_remaining,
            }
            for issue in selected
        ]
    }
    if len(normalized) > len(selected):
        payload["additional_errors_omitted"] = True
    return _canonical_json(payload)


def invoke_with_structured_output_recovery(
    invoke: Callable[[Mapping[str, object]], object],
    original_input: Mapping[str, object],
    max_retries: int,
    parse: Callable[[object], T],
) -> T:
    """Invoke and parse a model response within a bounded retry budget."""

    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise TypeError("max_retries must be an integer")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")

    original_content = _canonical_json(original_input)
    feedback: str | None = None

    for attempt in range(1, max_retries + 2):
        messages = [HumanMessage(content=original_content)]
        if feedback is not None:
            messages.append(HumanMessage(content=feedback))

        try:
            candidate = invoke({"messages": messages})
            return parse(candidate)
        except StructuredOutputFailure as error:
            issues = error.issues
        except (
            MultipleStructuredOutputsError,
            StructuredOutputValidationError,
        ) as error:
            issues = (_langchain_issue(error),)

        normalized = sorted(set(issues))
        if attempt > max_retries:
            raise StructuredOutputRetriesExhausted(normalized[0].code) from None
        feedback = _feedback(
            normalized,
            attempt=attempt,
            retries_remaining=max_retries + 1 - attempt,
        )

    raise AssertionError("unreachable")
