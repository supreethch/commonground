"""Rate limiting, and the client-identity problem underneath it.

Measured on this machine: login answers in 27ms (p50), almost all of it Argon2.
That is the right cost for a password check and it also means an unthrottled
endpoint accepts roughly **37 password attempts per second** from one client.
Argon2 makes each guess expensive; it does not make a million guesses
impossible.

Two implementations, chosen by configuration, exactly like the broadcaster:

  * `MemoryLimiter` -- correct for one process, which is what the free tier runs.
  * `RedisLimiter` -- correct across processes.

## Identifying the client

Behind a TLS-terminating proxy every request arrives from the proxy's address.
Counting that would put every visitor in one bucket, so the first person to
mistype a password would lock out everyone else.

`X-Forwarded-For` is therefore honoured **only when TRUST_PROXY is set**, because
a client can send that header itself. Trusting it unconditionally would let an
attacker rotate a header value and never be limited at all -- turning a rate
limiter into a decoration. The rightmost entry is not used either: proxies
append, so the *left* end is the original client, and the trusted deployment has
exactly one hop.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

logger = logging.getLogger("commonground.ratelimit")


@dataclass(frozen=True)
class Rule:
    """`limit` requests per `window` seconds."""

    name: str
    limit: int
    window: int


# Deliberately generous for a portfolio demo -- the point is to make automated
# guessing impractical, not to inconvenience a recruiter clicking around.
# 20/minute, not 10. Shared NAT is normal -- an office or a university sends
# many legitimate users from one address -- and 10 was tight enough that a
# handful of real people could lock each other out. 20 still cuts an
# unthrottled ~2,200 attempts a minute by more than 99%.
LOGIN = Rule("login", limit=20, window=60)
SIGNUP = Rule("signup", limit=5, window=300)
GENERATE = Rule("generate", limit=20, window=60)
IMPORT = Rule("import", limit=5, window=300)


class MemoryLimiter:
    """Sliding window counters held in this process."""

    name = "memory"

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, rule: Rule) -> int | None:
        """Return None if allowed, or the seconds to wait if not."""
        now = time.monotonic()
        window = self._hits[key]
        cutoff = now - rule.window
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= rule.limit:
            return max(1, int(window[0] + rule.window - now) + 1)

        window.append(now)

        # Keys are unbounded otherwise: one entry per address per rule, held for
        # the life of the process. Sweeping on write keeps that bounded without
        # a background task.
        if len(self._hits) > 10_000:
            self._sweep(cutoff)
        return None

    def _sweep(self, cutoff: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]


class RedisLimiter:
    """Counters in Redis, so several API processes share one budget."""

    name = "redis"

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None
        self._fallback = MemoryLimiter()

    def _redis(self):
        if self._client is None:
            import redis

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    def check(self, key: str, rule: Rule) -> int | None:
        try:
            client = self._redis()
            # A fixed window rather than a sliding one: INCR plus EXPIRE is two
            # commands and needs no stored history. The cost is that a burst
            # spanning a boundary can reach 2x the limit briefly, which is an
            # acceptable trade for the operational simplicity here.
            bucket = int(time.time() // rule.window)
            redis_key = f"rl:{rule.name}:{key}:{bucket}"
            count = client.incr(redis_key)
            if count == 1:
                client.expire(redis_key, rule.window)
            if count > rule.limit:
                return client.ttl(redis_key) or rule.window
            return None
        except Exception as exc:  # noqa: BLE001
            # Redis being down must not take authentication with it. Fall back
            # to per-process limiting, which is weaker but not absent, and say
            # so rather than failing open in silence.
            logger.warning("rate limiter falling back to memory: %s", exc)
            return self._fallback.check(key, rule)


_limiter: MemoryLimiter | RedisLimiter | None = None


def get_limiter() -> MemoryLimiter | RedisLimiter:
    global _limiter
    if _limiter is None:
        from .config import get_settings

        settings = get_settings()
        _limiter = RedisLimiter(settings.redis_url) if settings.redis_url else MemoryLimiter()
        logger.info("rate limiting via %s", _limiter.name)
    return _limiter


def reset_limiter() -> None:
    """Drop the limiter. Tests use this so counters do not leak between them."""
    global _limiter
    _limiter = None


def client_key(request: Request) -> str:
    from .config import get_settings

    settings = get_settings()
    if settings.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Leftmost is the original client; proxies append as they go.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(request: Request, rule: Rule) -> None:
    from .config import get_settings

    # Off for the e2e suite and the latency benchmark; see the comment on
    # Settings.rate_limit_enabled.
    if not get_settings().rate_limit_enabled:
        return

    retry_after = get_limiter().check(f"{rule.name}:{client_key(request)}", rule)
    if retry_after is None:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Too many requests. Try again in {retry_after}s.",
        headers={"Retry-After": str(retry_after)},
    )
