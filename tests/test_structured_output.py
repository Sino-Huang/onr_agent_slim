import json
from collections.abc import Mapping
from typing import cast

import pytest
from langchain_core.messages import HumanMessage

from onr.agents.structured_output import (
    StructuralIssue,
    StructuredOutputFailure,
    StructuredOutputRetriesExhausted,
    invoke_with_structured_output_recovery,
)


def _message_content(message: HumanMessage) -> str:
    return cast(str, message.content)


def test_later_valid_candidate_succeeds_with_only_original_and_safe_feedback() -> None:
    original = {"mission": "m-1", "nested": {"priority": 2}}
    calls: list[Mapping[str, object]] = []
    candidates = iter(("rejected SECRET candidate", {"mission_id": "m-1"}))

    def invoke(payload: Mapping[str, object]) -> object:
        calls.append(payload)
        return next(candidates)

    def parse(candidate: object) -> str:
        if not isinstance(candidate, dict):
            raise StructuredOutputFailure(
                (StructuralIssue("invalid_type", "$.mission_id", "string"),)
            )
        return cast(str, candidate["mission_id"])

    result = invoke_with_structured_output_recovery(invoke, original, 1, parse)

    assert result == "m-1"
    assert original == {"mission": "m-1", "nested": {"priority": 2}}
    assert len(calls) == 2
    first_messages = cast(list[HumanMessage], calls[0]["messages"])
    second_messages = cast(list[HumanMessage], calls[1]["messages"])
    assert [_message_content(message) for message in first_messages] == [
        '{"mission":"m-1","nested":{"priority":2}}'
    ]
    assert len(second_messages) == 2
    assert _message_content(second_messages[0]) == _message_content(first_messages[0])
    feedback = _message_content(second_messages[1])
    assert json.loads(feedback) == {
        "errors": [
            {
                "attempt": 1,
                "code": "invalid_type",
                "expected": "string",
                "path": "$.mission_id",
                "retries_remaining": 1,
            }
        ]
    }
    assert "SECRET" not in feedback


def test_zero_retries_makes_one_call_and_exhausts() -> None:
    call_count = 0

    def invoke(_: Mapping[str, object]) -> object:
        nonlocal call_count
        call_count += 1
        return None

    def parse(_: object) -> str:
        raise StructuredOutputFailure(
            (StructuralIssue("missing_required_field", "$.result", "string"),)
        )

    with pytest.raises(StructuredOutputRetriesExhausted) as caught:
        invoke_with_structured_output_recovery(invoke, {"request": 1}, 0, parse)

    assert call_count == 1
    assert caught.value.code == "output_structure_retries_exhausted"
    assert caught.value.structural_code == "missing_required_field"


def test_exhaustion_is_stable_and_does_not_expose_raw_error() -> None:
    raw_error = "database password is hunter2"

    def parse(_: object) -> str:
        try:
            raise ValueError(raw_error)
        except ValueError as exc:
            raise StructuredOutputFailure(
                (StructuralIssue("invalid_value", "$.result", "known status"),)
            ) from exc

    with pytest.raises(StructuredOutputRetriesExhausted) as caught:
        invoke_with_structured_output_recovery(lambda _: {}, {}, 1, parse)

    error = caught.value
    assert error.code == "output_structure_retries_exhausted"
    assert error.structural_code == "invalid_value"
    assert raw_error not in str(error)
    assert raw_error not in repr(error)


def test_feedback_sorts_deduplicates_and_caps_issues() -> None:
    calls: list[Mapping[str, object]] = []

    def invoke(payload: Mapping[str, object]) -> object:
        calls.append(payload)
        return len(calls)

    issues = tuple(
        StructuralIssue("invalid_value", f"$.items[{index}]", "allowed value")
        for index in range(12, -1, -1)
    ) + (
        StructuralIssue("invalid_value", "$.items[3]", "allowed value"),
    )

    def parse(candidate: object) -> str:
        if candidate == 1:
            raise StructuredOutputFailure(issues)
        return "valid"

    assert invoke_with_structured_output_recovery(invoke, {"request": 1}, 1, parse) == "valid"
    retry_messages = cast(list[HumanMessage], calls[1]["messages"])
    feedback = cast(dict[str, object], json.loads(_message_content(retry_messages[1])))
    errors = cast(list[dict[str, object]], feedback["errors"])

    assert len(errors) == 8
    assert feedback["additional_errors_omitted"] is True
    assert [(entry["code"], entry["path"], entry["expected"]) for entry in errors] == sorted(
        (entry["code"], entry["path"], entry["expected"]) for entry in errors
    )
    assert all(entry["attempt"] == 1 for entry in errors)
    assert all(entry["retries_remaining"] == 1 for entry in errors)


def test_unstructured_errors_propagate_without_retry() -> None:
    failure = RuntimeError("authority failure")
    call_count = 0

    def invoke(_: Mapping[str, object]) -> object:
        nonlocal call_count
        call_count += 1
        raise failure

    with pytest.raises(RuntimeError) as caught:
        invoke_with_structured_output_recovery(invoke, {}, 3, lambda value: value)

    assert caught.value is failure
    assert call_count == 1
