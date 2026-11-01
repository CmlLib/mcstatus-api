import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from fastapi import Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from redis import asyncio as aioredis

logger = logging.getLogger(__name__)
CACHE_PREFIX = "mcstatus-api:status:v1:"


@dataclass(frozen=True)
class CacheEntry:
    body: bytes
    created_at: datetime

    def encode(self) -> str:
        return json.dumps({
            "body": self.body.decode("utf-8"),
            "created_at": self.created_at.isoformat(),
        })

    @classmethod
    def decode(cls, value: bytes) -> "CacheEntry":
        data = json.loads(value)
        created_at = datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("Cache creation time must include a timezone")

        return cls(
            body=data["body"].encode("utf-8"),
            created_at=created_at,
        )


class CacheBackend(Protocol):
    async def get(self, key: str) -> CacheEntry | None:
        ...

    async def set(self, key: str, entry: CacheEntry, expire: int) -> None:
        ...


class MemoryCacheBackend:
    def __init__(
        self,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")

        self._store: OrderedDict[str, tuple[float, CacheEntry]] = OrderedDict()
        self._max_entries = max_entries
        self._clock = clock

    async def get(self, key: str) -> CacheEntry | None:
        value = self._store.get(key)
        if value is None:
            return None

        deadline, entry = value
        if deadline <= self._clock():
            del self._store[key]
            return None

        self._store.move_to_end(key)
        return entry

    async def set(self, key: str, entry: CacheEntry, expire: int) -> None:
        now = self._clock()
        expired_keys = [
            stored_key
            for stored_key, (deadline, _) in self._store.items()
            if deadline <= now
        ]
        for expired_key in expired_keys:
            del self._store[expired_key]

        self._store[key] = (now + expire, entry)
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)


class RedisCacheBackend:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def get(self, key: str) -> CacheEntry | None:
        value = await self.redis.get(CACHE_PREFIX + key)
        if value is None:
            return None

        return CacheEntry.decode(value)

    async def set(self, key: str, entry: CacheEntry, expire: int) -> None:
        await self.redis.set(CACHE_PREFIX + key, entry.encode(), ex=expire)


class StatusCache:
    def __init__(self, backend: CacheBackend, expire: int = 60):
        self.backend = backend
        self.expire = expire

    async def respond(
        self,
        address: str,
        query: Callable[[str], Awaitable[object]],
    ) -> Response:
        address_hash = hashlib.sha256(address.encode("utf-8"))
        cache_key = address_hash.hexdigest()
        read_failed = False

        try:
            entry = await self.backend.get(cache_key)
        except Exception:
            logger.warning("Cache read failed; querying server directly", exc_info=True)
            read_failed = True
        else:
            if entry is not None:
                return self.cached_response(entry, "HIT")

        result = await query(address)
        body = JSONResponse(jsonable_encoder(result)).body
        if read_failed:
            return self.bypass_response(body)

        entry = CacheEntry(body=body, created_at=datetime.now(timezone.utc))
        try:
            await self.backend.set(cache_key, entry, self.expire)
        except Exception:
            logger.warning("Cache write failed; returning uncached result", exc_info=True)
            return self.bypass_response(body)

        return self.cached_response(entry, "MISS")

    @staticmethod
    def bypass_response(body: bytes) -> Response:
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "X-Cache": "BYPASS",
            },
        )

    @staticmethod
    def cached_response(
        entry: CacheEntry,
        cache_status: str,
    ) -> Response:
        headers = {
            "X-Cache": cache_status,
            "X-Cache-Created-At": entry.created_at.isoformat(),
        }
        return Response(content=entry.body, media_type="application/json", headers=headers)
