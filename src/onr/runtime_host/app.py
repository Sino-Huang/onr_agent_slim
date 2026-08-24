"""FastAPI adapter for the loopback-oriented Runtime Host."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from onr.runtime.config import RuntimeConfig, load_runtime_config
from onr.runtime_host.host import (
    HostAuthorizationError,
    HostConflictError,
    RuntimeHost,
    RuntimeWorkerOptions,
)


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_request_id: str = Field(min_length=1)
    console_session_id: str = Field(min_length=1)
    mission_intent: str = Field(min_length=1)
    source_authority: str = Field(min_length=1)

    @field_validator(
        "activation_request_id",
        "console_session_id",
        "mission_intent",
        "source_authority",
    )
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("activation field must not be blank")
        return value


class CancellationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancellation_request_id: str = Field(min_length=1)

    @field_validator("cancellation_request_id")
    @classmethod
    def reject_blank_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cancellation request ID must not be blank")
        return value


def create_app(
    *,
    host: RuntimeHost | None = None,
    config: RuntimeConfig | None = None,
    repo_root: Path | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    """Create an in-process app; configuration is loaded only when needed."""

    selected = host
    if selected is None:
        root = (Path.cwd() if repo_root is None else repo_root).resolve()
        if config is None:
            config = load_runtime_config(config_path, repo_root=root)
        selected = RuntimeHost(
            config,
            clock=lambda: datetime.now(UTC).isoformat(),
            generate_id=lambda kind: f"{kind}-{uuid4()}",
            worker_options=RuntimeWorkerOptions(repo_root=root.resolve()),
        )

    app = FastAPI(title="ONR Runtime Host")
    app.state.runtime_host = selected

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _invalid_request()

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "api_version": {"major": 1, "minor": 0}}

    @app.post("/api/v1/mission-activations", status_code=202)
    def activate(
        activation: ActivationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        credential = _bearer_credential(authorization)
        if credential is None:
            return _invalid_request()
        try:
            return selected.activate(
                activation_request_id=activation.activation_request_id,
                console_session_id=activation.console_session_id,
                mission_intent=activation.mission_intent,
                source_authority=activation.source_authority,
                credential=credential,
            )
        except HostConflictError as exc:
            return _error(409, exc.code, exc.message)

    @app.get("/api/v1/mission-runs/current")
    def current_run() -> dict[str, object]:
        return {"mission_run": selected.current_run()}

    @app.get("/api/v1/mission-runs/{mission_run_id}/mission-intent")
    def mission_intent(
        mission_run_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        credential = _bearer_credential(authorization)
        if credential is None:
            return _authorization_failed()
        try:
            return selected.mission_intent(mission_run_id, credential)
        except HostAuthorizationError:
            return _authorization_failed()

    @app.post("/api/v1/mission-runs/{mission_run_id}/cancellations", status_code=202)
    def cancel(
        mission_run_id: str,
        cancellation: CancellationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        credential = _bearer_credential(authorization)
        if credential is None:
            return _authorization_failed()
        try:
            return selected.cancel(
                mission_run_id=mission_run_id,
                cancellation_request_id=cancellation.cancellation_request_id,
                credential=credential,
            )
        except HostAuthorizationError:
            return _authorization_failed()
        except HostConflictError as exc:
            return _error(409, exc.code, exc.message)

    return app


def _bearer_credential(value: str | None) -> str | None:
    if value is None:
        return None
    scheme, separator, credential = value.partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not credential.strip():
        return None
    return credential.strip()


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _invalid_request() -> JSONResponse:
    return _error(422, "invalid_request", "request body or authorization is invalid")


def _authorization_failed() -> JSONResponse:
    return _error(403, "authorization_failed", "request is not authorized")
