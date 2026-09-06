"""Room events, and the two ways of fanning them out.

`Broadcaster` has two implementations, and which one runs is a deployment
decision rather than a code change:

  * `InProcessBroadcaster` fans out to the sockets this process holds. Correct
    whenever there is exactly one API process, which is what the free tier runs.
  * `RedisBroadcaster` publishes to Redis so several processes agree. Correct
    when there is more than one.

The distributed path exists and is exercised by `docker compose`, rather than
being written and never run -- but the free deployment does not pay for
infrastructure it cannot use. Same reasoning a2transit applies by polling
in-process when there is no worker to run.

**Votes are written over REST and only broadcast here.** A dropped socket then
costs a member their live updates but never their vote, and validation,
authorisation and rate limiting live in one place instead of two.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("commonground.realtime")

CHANNEL_PREFIX = "room:"


def channel_for(room_id: uuid.UUID) -> str:
    return f"{CHANNEL_PREFIX}{room_id}"


@dataclass
class Event:
    """Something that happened in a room, as it reaches a client."""

    type: str
    room_id: str
    payload: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> str:
        return json.dumps({"type": self.type, "room_id": self.room_id, **self.payload})


class Broadcaster(Protocol):
    async def publish(self, room_id: uuid.UUID, event: Event) -> None: ...

    async def subscribe(self, room_id: uuid.UUID, queue: asyncio.Queue) -> None: ...

    async def unsubscribe(self, room_id: uuid.UUID, queue: asyncio.Queue) -> None: ...


class InProcessBroadcaster:
    """Fan-out to the sockets this process holds."""

    name = "in-process"

    def __init__(self) -> None:
        self._rooms: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, room_id: uuid.UUID, event: Event) -> None:
        async with self._lock:
            listeners = list(self._rooms.get(str(room_id), ()))
        for queue in listeners:
            # put_nowait, not await put: one client whose queue is full must not
            # stall the request that produced the event. A slow reader loses
            # updates and re-syncs over REST, which is the whole reason the
            # socket is never the only route to a fact.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    async def subscribe(self, room_id: uuid.UUID, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._rooms.setdefault(str(room_id), set()).add(queue)

    async def unsubscribe(self, room_id: uuid.UUID, queue: asyncio.Queue) -> None:
        async with self._lock:
            listeners = self._rooms.get(str(room_id))
            if listeners:
                listeners.discard(queue)
                if not listeners:
                    del self._rooms[str(room_id)]

    def listener_count(self, room_id: uuid.UUID) -> int:
        return len(self._rooms.get(str(room_id), ()))


class RedisBroadcaster:
    """Publish through Redis so several API processes agree.

    One subscriber task per room per process, not per socket: a room with forty
    viewers would otherwise open forty Redis subscriptions to carry identical
    messages.
    """

    name = "redis"

    def __init__(self, url: str) -> None:
        self._url = url
        self._local = InProcessBroadcaster()
        self._tasks: dict[str, asyncio.Task] = {}
        self._client = None
        self._lock = asyncio.Lock()

    async def _redis(self):
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, room_id: uuid.UUID, event: Event) -> None:
        client = await self._redis()
        await client.publish(channel_for(room_id), event.encode())

    async def _pump(self, room_id: uuid.UUID) -> None:
        client = await self._redis()
        pubsub = client.pubsub()
        await pubsub.subscribe(channel_for(room_id))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    body = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("undecodable room event dropped")
                    continue
                await self._local.publish(
                    room_id,
                    Event(
                        type=body.pop("type", "unknown"),
                        room_id=body.pop("room_id", str(room_id)),
                        payload=body,
                    ),
                )
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel_for(room_id))
                await pubsub.aclose()

    async def subscribe(self, room_id: uuid.UUID, queue: asyncio.Queue) -> None:
        await self._local.subscribe(room_id, queue)
        async with self._lock:
            key = str(room_id)
            if key not in self._tasks or self._tasks[key].done():
                self._tasks[key] = asyncio.create_task(self._pump(room_id))

    async def unsubscribe(self, room_id: uuid.UUID, queue: asyncio.Queue) -> None:
        await self._local.unsubscribe(room_id, queue)
        async with self._lock:
            key = str(room_id)
            if self._local.listener_count(room_id) == 0 and key in self._tasks:
                self._tasks.pop(key).cancel()


_broadcaster: Broadcaster | None = None


def get_broadcaster() -> Broadcaster:
    """The process-wide broadcaster, chosen from configuration."""
    global _broadcaster
    if _broadcaster is None:
        from .config import get_settings

        settings = get_settings()
        _broadcaster = (
            RedisBroadcaster(settings.redis_url) if settings.redis_url else InProcessBroadcaster()
        )
        logger.info("broadcasting via %s", _broadcaster.name)
    return _broadcaster


def reset_broadcaster() -> None:
    """Drop the cached broadcaster. Tests use this; nothing else should."""
    global _broadcaster
    _broadcaster = None
