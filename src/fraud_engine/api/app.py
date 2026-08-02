"""FastAPI application.

Kept separate from the server entry point so tests can drive the app with
httpx without ever opening a network port.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fraud_engine.api.errors import app_error_handler, unhandled_error_handler
from fraud_engine.api.routes import analytics, decisions, labels, reference
from fraud_engine.db.pool import close_pool, get_pool, open_pool
from fraud_engine.lib.errors import AppError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the pool at startup, close it at shutdown.

    Opening lazily on the first request would make that request pay the
    connection cost, and a cold start is exactly when latency matters least
    to the user and most to a health check.
    """
    await open_pool()
    yield
    await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="fraud-engine",
        version="0.1.0",
        description=(
            "Real-time payment fraud decision engine: versioned rules, "
            "velocity features, shared-attribute linking, reproducible "
            "decisions, and backtesting against labelled outcomes."
        ),
        lifespan=lifespan,
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.include_router(decisions.router)
    app.include_router(labels.router)
    app.include_router(analytics.router)
    app.include_router(reference.router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        """Liveness. Deliberately does NOT touch the database.

        A health check that queries Postgres reports the process dead during
        a brief database blip and triggers a pointless restart.
        """
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    async def ready() -> dict:
        """Readiness. This one DOES check the database.

        An instance that cannot reach Postgres should not receive traffic.
        """
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
            return {"status": "ready", "database": "reachable"}
        except Exception as exc:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "unreachable",
                         "reason": str(exc)},
            )

    return app
