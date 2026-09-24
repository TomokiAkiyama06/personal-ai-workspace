"""Uniform error responses.

Every non-2xx response produced by the application has the shape::

    {"error": {"code": "not_found", "message": "Not Found", "request_id": "..."}}

``code`` is a stable machine-readable identifier; ``message`` is for humans.
Validation failures add ``details`` (location, message, error type) without
echoing the submitted values.
"""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


class ErrorDetail(BaseModel):
    loc: list[str | int]
    message: str
    type: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# OpenAPI ``responses`` shared by every operation (see ``create_app``).
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "4XX": {"model": ErrorResponse, "description": "Client error"},
    "5XX": {"model": ErrorResponse, "description": "Server error"},
}


class ApiError(Exception):
    """Raise from application code to return a specific error response."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str | None,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, request_id=request_id, details=details
        )
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status_code,
        headers=headers,
    )


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


async def _handle_api_error(request: Request, error: ApiError) -> JSONResponse:
    return error_response(
        error.status_code, error.code, error.message, request_id=_request_id(request)
    )


async def _handle_http_exception(
    request: Request, error: StarletteHTTPException
) -> JSONResponse:
    message = (
        error.detail if isinstance(error.detail, str) else _phrase(error.status_code)
    )
    return error_response(
        error.status_code,
        _STATUS_CODES.get(error.status_code, "http_error"),
        message,
        request_id=_request_id(request),
        headers=dict(error.headers) if error.headers else None,
    )


async def _handle_validation_error(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    details = [
        ErrorDetail(
            loc=list(item["loc"]),
            message=item["msg"],
            type=item["type"],
        )
        for item in error.errors()
    ]
    return error_response(
        422,
        "validation_error",
        "Request validation failed",
        request_id=_request_id(request),
        details=details,
    )


class UnhandledErrorMiddleware:
    """Turn an unexpected exception into a generic 500 in the error format.

    Starlette's own handler for ``Exception`` runs *outside* the user
    middleware stack, so its response would miss the request-id and security
    headers. Answering here, inside those middlewares, keeps them on 500s.
    The exception is logged with its traceback; the client sees no details.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            logger.exception("Unhandled error while processing a request")
            if response_started:
                raise
            response = error_response(
                500,
                "internal_error",
                "Internal server error",
                request_id=scope.get("state", {}).get("request_id"),
            )
            await response(scope, receive, send)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_middleware(UnhandledErrorMiddleware)
