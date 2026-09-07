"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .api import auth, imports, profiles, rooms
from .config import get_settings
from .db import get_engine
from .logging import RequestLogMiddleware, configure_logging
from .services.recommender import recommender_service

logger = logging.getLogger("commonground")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(
        settings.log_level, as_json=settings.log_json or settings.environment == "production"
    )

    app = FastAPI(
        title="CommonGround API",
        version="0.1.0",
        summary="Group music recommendation with per-track explanations",
    )

    app.add_middleware(RequestLogMiddleware)
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
    app.include_router(rooms.router)

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

        # The model snapshot's age is reported because a cold process answers
        # its first playlist request ~500ms slower, and that is worth being able
        # to see rather than guess at from a latency graph.
        snapshot = recommender_service.peek()

        return {
            "status": "ok" if database == "ok" else "degraded",
            "environment": settings.environment,
            "database": database,
            "model": (
                {
                    "fitted": True,
                    "users": snapshot.n_users,
                    "items": snapshot.n_items,
                    "interactions": snapshot.n_interactions,
                    "fit_seconds": round(snapshot.fit_seconds, 3),
                    "age_seconds": round(snapshot.age_seconds, 1),
                }
                if snapshot
                else {"fitted": False}
            ),
            # Unset means the in-process broadcaster, which is correct for a
            # single instance. See docs/architecture.md.
            "broadcaster": "redis" if settings.redis_url else "in-process",
        }

    return app


app = create_app()
