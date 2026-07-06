"""Unified error response format (per API.md "Errors" section).

All ClawsomeFlow API error responses share this shape:

    {"error": "ERROR_CODE", "message": "...", "details": {...}}

Public API:
* :class:`ApiError` — base exception, wraps an error code + HTTP status.
* :func:`register_exception_handlers` — attach handlers for known errors.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.services import hermes_agents as hermes_svc
from app.services.agent_store import AgentStoreError
from app.services.openclaw_agents import OpenclawAgentError
from app.storage import StorageVersionConflict
from app.validators import FlowValidationError


class ApiError(Exception):
    """Application-level error converted to the canonical JSON shape."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={
                "error": self.code,
                "message": self.message,
                "details": self.details,
            },
        )


# Single source of truth for HermesAgentError → API error-code mapping.
# Used both by the per-route ``raise _map_service_error(exc)`` pattern in
# ``api/hermes_agents.py`` and by the global fallback handler below (which
# catches any HermesAgentError that escapes a route without a local catch,
# so clients always get the canonical JSON shape instead of a bare 500).
_HERMES_ERROR_MAPPING: dict[type, tuple[str, int]] = {
    hermes_svc.AgentIdInvalid: ("INVALID_PAYLOAD", 400),
    hermes_svc.AgentAlreadyExists: ("AGENT_ALREADY_EXISTS", 409),
    hermes_svc.AgentNotFound: ("AGENT_NOT_FOUND", 404),
    hermes_svc.AgentInUse: ("AGENT_IN_USE", 409),
    hermes_svc.HermesUnavailable: ("HERMES_UNAVAILABLE", 503),
    hermes_svc.ProfileOpFailed: ("HERMES_CLI_FAILED", 502),
    hermes_svc.AgentCreateCancelled: ("AGENT_CREATE_CANCELLED", 409),
}


def map_hermes_agent_error(exc: hermes_svc.HermesAgentError) -> ApiError:
    """Translate a service-layer Hermes error into the canonical ApiError."""
    code, status = _HERMES_ERROR_MAPPING.get(type(exc), ("HERMES_ERROR", 500))
    return ApiError(code, str(exc), status_code=status, details=exc.details)


def register_exception_handlers(app: FastAPI) -> None:
    """Wire up exception handlers shared across all routers."""

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(FlowValidationError)
    async def _flow_validation_handler(
        _request: Request, exc: FlowValidationError
    ) -> JSONResponse:
        return ApiError(
            code=exc.code, message=exc.message, status_code=400, details=exc.details
        ).to_response()

    @app.exception_handler(StorageVersionConflict)
    async def _version_conflict_handler(
        _request: Request, exc: StorageVersionConflict
    ) -> JSONResponse:
        return ApiError(
            code="VERSION_CONFLICT",
            message=str(exc),
            status_code=409,
            details={
                "flow_id": exc.flow_id,
                "expected": exc.expected,
                "actual": exc.actual,
            },
        ).to_response()

    @app.exception_handler(OpenclawAgentError)
    async def _openclaw_agent_error_handler(
        _request: Request, exc: OpenclawAgentError
    ) -> JSONResponse:
        return ApiError(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            details=exc.details,
        ).to_response()

    @app.exception_handler(AgentStoreError)
    async def _agent_store_error_handler(
        _request: Request, exc: AgentStoreError
    ) -> JSONResponse:
        return ApiError(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            details=exc.details,
        ).to_response()

    @app.exception_handler(hermes_svc.HermesAgentError)
    async def _hermes_agent_error_handler(
        _request: Request, exc: hermes_svc.HermesAgentError
    ) -> JSONResponse:
        return map_hermes_agent_error(exc).to_response()


__all__ = ["ApiError", "map_hermes_agent_error", "register_exception_handlers"]
