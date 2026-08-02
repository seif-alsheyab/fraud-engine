"""Error translation.

A domain error carries its own status and code, so the handler maps it
directly. Anything else is unexpected: log the detail internally, return a
generic message externally. Internal messages leak table names, file paths
and query fragments that help an attacker map the system.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from fraud_engine.lib.errors import AppError
from fraud_engine.lib.logging import get_logger


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    get_logger().warn(
        "request rejected",
        code=exc.code, status=exc.status,
        path=request.url.path, method=request.method,
    )
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message,
                           "details": exc.details}},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    get_logger().error(
        "unhandled error",
        error_type=type(exc).__name__, error=str(exc),
        path=request.url.path, method=request.method,
    )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR",
                           "message": "An unexpected error occurred."}},
    )
