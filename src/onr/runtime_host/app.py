"""FastAPI adapter for the loopback-oriented Runtime Host."""

from __future__ import annotations

import ipaddress
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from onr.adapters.run_narrative_summarizer import ModelRunNarrativeSummarizer
from onr.runtime.composition import create_chat_model
from onr.runtime.config import RuntimeConfig, load_runtime_config
from onr.runtime_host.artifacts import (
    MAX_PAGE_SIZE,
    MAX_PREVIEW_BYTES,
    ArtifactNotFoundError,
    ArtifactUnavailableError,
)
from onr.runtime_host.host import (
    HISTORY_DEFAULT_LIMIT,
    HISTORY_MAX_LIMIT,
    HostAuthorizationError,
    HostConflictError,
    HostNotFoundError,
    RunRootUnavailableError,
    RuntimeHost,
    RuntimeWorkerOptions,
)
from onr.runtime_host.observations import InvalidCursorError
from onr.runtime_host.operator_projection import (
    OPERATOR_DEFAULT_LIMIT,
    OPERATOR_MAX_LIMIT,
    OperatorSection,
)
from onr.runtime_host.stack.presets import StackRequestError
from onr.runtime_host.world import SOURCES, FrameUnavailableError

_OPERATOR_SECTIONS = {
    "overview",
    "agents",
    "environment",
    "artifacts",
    "progress",
    "beliefs",
    "context",
    "world",
    "stack",
}


class StackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    preset_id: str | None = None
    airsim: bool | None = None
    perception: Literal["off", "ideal", "yolo"] | None = None
    update_ownership: Literal["coordinator_driven", "environment_driven"] | None = None
    simulation_limit_seconds: float | None = Field(default=None, gt=0)

    @field_validator(
        "preset_id",
        "airsim",
        "perception",
        "update_ownership",
        "simulation_limit_seconds",
    )
    @classmethod
    def reject_null_options(cls, value: object) -> object:
        if value is None:
            raise ValueError("stack options cannot be null")
        return value


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_request_id: str = Field(min_length=1)
    console_session_id: str = Field(min_length=1)
    mission_intent: str = Field(min_length=1)
    source_authority: str = Field(min_length=1)
    stack: StackRequest | None = None

    @field_validator("stack")
    @classmethod
    def reject_null_stack(cls, value: StackRequest | None) -> StackRequest:
        if value is None:
            raise ValueError("stack must be an object when supplied")
        return value

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


class ReceiptExportRequest(BaseModel):
    """Empty: an export always writes the run's one receipt file."""

    model_config = ConfigDict(extra="forbid")


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
            runs_root=root / "var" / "runtime-host" / "runs",
            narrative_summarizer=ModelRunNarrativeSummarizer(create_chat_model(config)),
        )
    runtime_host = selected

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        runtime_host.close()

    app = FastAPI(title="ONR Runtime Host", lifespan=lifespan)
    app.state.runtime_host = selected

    @app.middleware("http")
    async def strict_request(request: Request, call_next):
        allowed: set[str] = set()
        path = request.url.path
        if path.endswith("/stack/preflight"):
            allowed = {
                "preset_id",
                "airsim",
                "perception",
                "update_ownership",
                "simulation_limit_seconds",
            }
        elif path.endswith("/operator-view"):
            allowed = {"section", "limit", "cursor", "before", "raw"}
        elif path == "/api/v1/mission-runs":
            allowed = {"limit", "before"}
        elif path.endswith("/world-frame"):
            allowed = {"source"}
        elif path.endswith(("/observations", "/activities", "/artifacts", "/entries")):
            allowed = {"cursor", "limit"}
        elif path.endswith("/content"):
            allowed = {"offset", "limit"}
        items = list(request.query_params.multi_items())
        if any(key not in allowed for key, _ in items) or len(
            {key for key, _ in items}
        ) != len(items):
            return _invalid_request()
        if request.method == "POST":

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("repeated body field")
                    result[key] = value
                return result

            try:
                json.loads(await request.body(), object_pairs_hook=unique_object)
            except (ValueError, UnicodeError):
                return _invalid_request()
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _invalid_request()

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "api_version": {"major": 1, "minor": 5}}

    @app.get("/api/v1/stack/presets")
    def stack_presets() -> Any:
        return selected.stack_presets()

    @app.get("/api/v1/stack/preflight")
    def stack_preflight(request: Request) -> Any:
        try:
            values: dict[str, object] = dict(request.query_params)
            if "airsim" in values:
                if values["airsim"] not in {"true", "false"}:
                    raise ValueError("invalid airsim")
                values["airsim"] = values["airsim"] == "true"
            if "simulation_limit_seconds" in values:
                number = float(str(values["simulation_limit_seconds"]))
                if not math.isfinite(number) or number <= 0:
                    raise ValueError("invalid simulation limit")
                values["simulation_limit_seconds"] = number
            StackRequest.model_validate(values)
            return selected.stack_preflight(values)
        except (StackRequestError, ValueError):
            return _invalid_request()

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
                stack=None
                if activation.stack is None
                else activation.stack.model_dump(exclude_unset=True),
            )
        except HostConflictError as exc:
            return _error(409, exc.code, exc.message)
        except StackRequestError:
            return _invalid_request()

    @app.get("/api/v1/mission-runs/current")
    def current_run() -> dict[str, object]:
        return {"mission_run": selected.current_run()}

    @app.get("/api/v1/mission-runs")
    def mission_runs(request: Request) -> Any:
        # Rows name every run on this Host: loopback only, like operator-view.
        if not _loopback(request):
            return _authorization_failed()
        try:
            limit, before = _history_query(request)
            return selected.mission_runs(limit=limit, before=before)
        except InvalidCursorError as exc:
            return _evidence_error(exc)
        except ValueError:
            return _error(422, "invalid_request", "run history query is invalid")

    @app.get("/api/v1/mission-runs/{mission_run_id}/observations")
    def observations(
        mission_run_id: str,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(gt=0, le=500)] = None,
    ) -> Any:
        try:
            return selected.observations(mission_run_id, cursor=cursor, limit=limit)
        except (HostNotFoundError, InvalidCursorError) as exc:
            return _evidence_error(exc)

    @app.get("/api/v1/mission-runs/{mission_run_id}/narrative")
    def narrative(mission_run_id: str) -> Any:
        try:
            return selected.narrative(mission_run_id)
        except HostNotFoundError as exc:
            return _evidence_error(exc)

    @app.get("/api/v1/mission-runs/{mission_run_id}/activities")
    def activities(
        mission_run_id: str,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(gt=0, le=500)] = None,
    ) -> Any:
        try:
            return selected.activities(mission_run_id, cursor=cursor, limit=limit)
        except (HostNotFoundError, InvalidCursorError) as exc:
            return _evidence_error(exc)

    @app.get("/api/v1/mission-runs/{mission_run_id}/operator-view")
    def operator_view(mission_run_id: str, request: Request) -> Any:
        if not _loopback(request):
            return _authorization_failed()
        try:
            section, limit, cursor, before, raw = _operator_view_query(request)
            return selected.operator_view(
                mission_run_id,
                section=section,
                limit=limit,
                cursor=cursor,
                before=before,
                raw=raw,
            )
        except HostNotFoundError as exc:
            return _evidence_error(exc)
        except InvalidCursorError as exc:
            return _evidence_error(exc)
        except ValueError:
            return _operator_invalid_request()

    @app.get("/api/v1/mission-runs/{mission_run_id}/world-frame")
    def world_frame(mission_run_id: str, request: Request) -> Any:
        source = request.query_params.get("source", "world")
        if source not in SOURCES:
            return _invalid_request()
        try:
            frame = selected.world_frame(mission_run_id, source)
            headers = {"ETag": frame.etag, "X-Frame-Sequence": str(frame.sequence)}
            if frame.mission_time_seconds is not None:
                headers["X-Mission-Time"] = str(frame.mission_time_seconds)
            if request.headers.get("if-none-match") == frame.etag:
                return Response(status_code=304, headers=headers)
            return Response(frame.data, media_type=frame.media_type, headers=headers)
        except HostNotFoundError as exc:
            return _evidence_error(exc)
        except FrameUnavailableError:
            return _error(404, "frame_unavailable", "World frame is unavailable")

    @app.get("/api/v1/mission-runs/{mission_run_id}/artifacts")
    def artifacts(
        mission_run_id: str,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(gt=0, le=MAX_PAGE_SIZE)] = None,
    ) -> Any:
        try:
            return selected.artifacts(mission_run_id, cursor=cursor, limit=limit)
        except (HostNotFoundError, InvalidCursorError) as exc:
            return _evidence_error(exc)

    @app.get("/api/v1/mission-runs/{mission_run_id}/artifacts/{artifact_id}/content")
    def artifact_content(
        mission_run_id: str,
        artifact_id: str,
        request: Request,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(gt=0, le=MAX_PREVIEW_BYTES)] = 4096,
    ) -> Any:
        if (
            artifact_id == "worker-log" or artifact_id.startswith("service-log-")
        ) and not _loopback(request):
            return _authorization_failed()
        try:
            return selected.artifact_content(
                mission_run_id, artifact_id, offset=offset, limit=limit
            )
        except HostNotFoundError as exc:
            return _evidence_error(exc)
        except ArtifactNotFoundError:
            return _artifact_not_found()
        except ArtifactUnavailableError:
            return _artifact_unavailable()
        except ValueError:
            return _invalid_request()

    @app.get("/api/v1/mission-runs/{mission_run_id}/artifacts/{artifact_id}/entries")
    def conversation_entries(
        mission_run_id: str,
        artifact_id: str,
        cursor: str | None = None,
        limit: Annotated[int | None, Query(gt=0, le=MAX_PAGE_SIZE)] = None,
    ) -> Any:
        try:
            return selected.conversation_entries(
                mission_run_id,
                artifact_id,
                cursor=cursor,
                limit=limit,
            )
        except (HostNotFoundError, InvalidCursorError) as exc:
            return _evidence_error(exc)
        except ArtifactNotFoundError:
            return _artifact_not_found()

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

    @app.post("/api/v1/mission-runs/{mission_run_id}/receipt-exports")
    def export_receipt(
        mission_run_id: str,
        request: Request,
        export: ReceiptExportRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        del export  # empty: the Host chooses the one receipt path
        # The response names a Run Root path: loopback owners only.
        credential = _bearer_credential(authorization)
        if credential is None or not _loopback(request):
            return _authorization_failed()
        try:
            return selected.export_receipt(mission_run_id, credential)
        except HostAuthorizationError:
            return _authorization_failed()
        except HostConflictError as exc:
            return _error(409, exc.code, exc.message)

    return app


def _loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _bearer_credential(value: str | None) -> str | None:
    if value is None:
        return None
    scheme, separator, credential = value.partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not credential.strip():
        return None
    return credential.strip()


def _operator_view_query(
    request: Request,
) -> tuple[OperatorSection, int, str | None, str | None, bool]:
    items = list(request.query_params.multi_items())
    allowed = {"section", "limit", "cursor", "before", "raw"}
    if any(key not in allowed for key, _ in items):
        raise ValueError("unknown operator-view query parameter")
    values: dict[str, str] = {}
    for key, value in items:
        if key in values:
            raise ValueError("repeated operator-view query parameter")
        values[key] = value
    section = values.get("section")
    if section not in _OPERATOR_SECTIONS:
        raise ValueError("invalid operator-view section")
    limit_text = values.get("limit")
    if limit_text is None:
        limit = OPERATOR_DEFAULT_LIMIT
    elif (
        not limit_text.isascii()
        or not limit_text.isdecimal()
        or limit_text.startswith("0")
    ):
        raise ValueError("invalid operator-view limit")
    else:
        limit = int(limit_text)
    if not 1 <= limit <= OPERATOR_MAX_LIMIT:
        raise ValueError("invalid operator-view limit")
    cursor = values.get("cursor")
    before = values.get("before")
    if cursor == "" or before == "" or (cursor is not None and before is not None):
        raise ValueError("invalid operator-view paging query")
    if "raw" in values:
        if section != "environment" or values["raw"] not in {"true", "false"}:
            raise ValueError("invalid operator-view raw query")
        raw = values["raw"] == "true"
    else:
        raw = False
    return cast(OperatorSection, section), limit, cursor, before, raw


def _history_query(request: Request) -> tuple[int, str | None]:
    values = dict(request.query_params)
    limit_text = values.get("limit")
    if limit_text is None:
        limit = HISTORY_DEFAULT_LIMIT
    elif (
        not limit_text.isascii()
        or not limit_text.isdecimal()
        or limit_text.startswith("0")
        or not 1 <= int(limit_text) <= HISTORY_MAX_LIMIT
    ):
        raise ValueError("invalid run history limit")
    else:
        limit = int(limit_text)
    before = values.get("before")
    if before == "":
        raise ValueError("invalid run history cursor")
    return limit, before


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _invalid_request() -> JSONResponse:
    return _error(422, "invalid_request", "request body or authorization is invalid")


def _operator_invalid_request() -> JSONResponse:
    return _error(422, "invalid_request", "operator-view query is invalid")


def _evidence_error(exc: HostNotFoundError | InvalidCursorError) -> JSONResponse:
    if isinstance(exc, RunRootUnavailableError):
        return _error(
            404,
            "run_root_unavailable",
            "Run Root of this Mission Run is missing on this Host; its evidence "
            "cannot be read",
        )
    if isinstance(exc, HostNotFoundError):
        return _error(
            404,
            "mission_run_not_found",
            "Mission Run is unknown to this Runtime Host",
        )
    return _error(
        422,
        "invalid_cursor",
        "cursor is malformed, expired, or does not belong to this Mission Run",
    )


def _authorization_failed() -> JSONResponse:
    return _error(403, "authorization_failed", "request is not authorized")


def _artifact_not_found() -> JSONResponse:
    return _error(404, "artifact_not_found", "Artifact is unknown to this Mission Run")


def _artifact_unavailable() -> JSONResponse:
    return _error(
        404,
        "artifact_unavailable",
        "Artifact content failed validation or is unavailable",
    )
