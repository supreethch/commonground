"""Structured request logging.

One JSON line per request, carrying method, path, status, duration and a request
id. JSON rather than prose because the only place these are read is a hosted log
viewer, where grepping a formatted sentence is worse than filtering a field.

The request id is echoed back as `X-Request-Id`. When someone reports "it failed
around three o'clock", that header is the difference between finding the request
and guessing at it -- and an id supplied by the caller is preserved, so a trace
survives the hop from the frontend.

Deliberately not a middleware that also swallows exceptions: an unhandled error
should still reach FastAPI's own handler and still be a 500. This times and
labels, it does not intercept.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("commonground.access")

# Health checks are polled constantly by the host and say nothing when they
# succeed. Logging every one buries the requests that matter.
QUIET_PATHS = {"/health"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    """Install one handler on the root logger.

    Called once at app creation. Uvicorn installs its own handlers; this leaves
    them alone and adds structure to the application's own records rather than
    fighting the server for control of the root logger.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if as_json
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    )

    root = logging.getLogger("commonground")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Otherwise every record is emitted twice: once here and once by whatever
    # the root logger is configured with.
    root.propagate = False


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            logger.exception(
                "request failed",
                extra={
                    "context": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": round(duration, 1),
                    }
                },
            )
            # Re-raised, not swallowed: this middleware labels and times, it
            # does not decide what a failure returns.
            raise

        duration = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = request_id

        if request.url.path not in QUIET_PATHS:
            logger.info(
                "request",
                extra={
                    "context": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "duration_ms": round(duration, 1),
                    }
                },
            )
        return response
