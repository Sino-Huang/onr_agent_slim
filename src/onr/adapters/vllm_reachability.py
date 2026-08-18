"""Synchronous reachability checks for an OpenAI-compatible vLLM server."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit


class VLLMReachabilityError(RuntimeError):
    """Raised when a configured vLLM endpoint is not usable."""


def _diagnostic(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text[:1000] or "<empty response>"


def _failure(endpoint: str, model: str, diagnostic: object) -> VLLMReachabilityError:
    return VLLMReachabilityError(
        f"vLLM reachability check failed endpoint={endpoint} model={model}: "
        f"{_diagnostic(diagnostic)}"
    )


def _request(
    endpoint: str,
    *,
    model: str,
    api_key: str,
    timeout: float,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> bytes:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(endpoint, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", response.getcode())
            response_body = response.read()
    except HTTPError as exc:
        response_body = exc.read()
        raise _failure(endpoint, model, f"HTTP {exc.code}: {_diagnostic(response_body)}") from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise _failure(endpoint, model, f"{type(exc).__name__}: {exc}") from exc
    if not 200 <= status < 300:
        raise _failure(endpoint, model, f"HTTP {status}: {_diagnostic(response_body)}")
    return response_body


def _health_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def probe_vllm_reachability(
    base_url: str,
    model: str,
    api_key: str = "EMPTY",
    temperature: int | float = 0,
    *,
    timeout: float = 5.0,
) -> None:
    """Verify model discovery, health, and one real chat completion request.

    ``base_url`` is the OpenAI-compatible API root, normally ending in ``/v1``.
    The server is expected to already be running; this function deliberately
    performs no retry or polling.
    """

    models_endpoint = f"{base_url.rstrip('/')}/models"
    models_body = _request(
        models_endpoint,
        model=model,
        api_key=api_key,
        timeout=timeout,
    )
    try:
        models = json.loads(models_body)
        model_ids = {
            item.get("id")
            for item in models.get("data", [])
            if isinstance(item, dict)
        }
    except (AttributeError, json.JSONDecodeError, TypeError) as exc:
        raise _failure(models_endpoint, model, f"invalid JSON response: {_diagnostic(models_body)}") from exc
    if model not in model_ids:
        raise _failure(
            models_endpoint,
            model,
            f"configured model is not listed; response={_diagnostic(models_body)}",
        )

    health_endpoint = _health_endpoint(base_url)
    _request(health_endpoint, model=model, api_key=api_key, timeout=timeout)

    completion_endpoint = f"{base_url.rstrip('/')}/chat/completions"
    completion_body = _request(
        completion_endpoint,
        model=model,
        api_key=api_key,
        timeout=timeout,
        method="POST",
        payload={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "temperature": temperature,
            "max_tokens": 1,
        },
    )
    try:
        completion = json.loads(completion_body)
        choices = completion.get("choices")
    except (AttributeError, json.JSONDecodeError, TypeError) as exc:
        raise _failure(
            completion_endpoint,
            model,
            f"invalid completion JSON response: {_diagnostic(completion_body)}",
        ) from exc
    if not isinstance(choices, list) or not choices:
        raise _failure(
            completion_endpoint,
            model,
            f"completion response must contain a non-empty choices list; "
            f"response={_diagnostic(completion_body)}",
        )


# The shorter name is convenient for callers that already know the endpoint
# is vLLM, while the explicit name remains the public descriptive API.
probe_vllm = probe_vllm_reachability


__all__ = ["VLLMReachabilityError", "probe_vllm", "probe_vllm_reachability"]
