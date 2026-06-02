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


__all__ = ["ApiError", "register_exception_handlers"]
