"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .api import auth, imports, profiles
from .config import get_settings
from .db import get_engine

logger = logging.getLogger("commonground")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="CommonGround API",
        version="0.1.0",
        summary="Group music recommendation with per-track explanations",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(profiles.router)
    app.include_router(imports.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        """Liveness plus the one dependency that actually matters.

        Reporting the database honestly rather than always returning 200: a
        health check that cannot fail tells a deployment nothing, and on a
        scale-to-zero database the interesting failure is exactly this one.
        """
        database = "ok"
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - the point is to report any failure
            logger.warning("health check could not reach the database: %s", exc)
            database = "unavailable"

        return {
            "status": "ok" if database == "ok" else "degraded",
            "environment": settings.environment,
            "database": database,
            # Unset means the in-process broadcaster, which is correct for a
            # single instance. See docs/architecture.md.
            "broadcaster": "redis" if settings.redis_url else "in-process",
        }

    return app


app = create_app()
