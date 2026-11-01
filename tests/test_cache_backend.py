import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.backends.redis import RedisBackend
from mcstatus import JavaServer
from mcstatus.status_response import JavaStatusResponse

from app import main as api


class CacheBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FastAPICache.reset()
        self.addCleanup(FastAPICache.reset)
        memory_backend = InMemoryBackend()
        await memory_backend.clear(namespace="mcstatus-api-cache")
        self.addAsyncCleanup(
            memory_backend.clear,
            namespace="mcstatus-api-cache",
        )

    async def test_memory_caches_responses_without_creating_redis_client(self):
        status = JavaStatusResponse.build(
            {
                "version": {"name": "1.21.1", "protocol": 767},
                "players": {"online": 3, "max": 20},
                "description": "Test server",
            },
            latency=12.75,
        )
        server = JavaServer("127.0.0.1", 25565)
        server.async_status = AsyncMock(return_value=status)
        lookup = AsyncMock(return_value=server)

        with (
            patch.dict(os.environ, {"CACHE_BACKEND": "memory"}),
            patch.object(api.aioredis, "from_url") as redis_factory,
            patch.object(api.JavaServer, "async_lookup", lookup),
        ):
            async with api.lifespan(api.app):
                self.assertIsInstance(FastAPICache.get_backend(), InMemoryBackend)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api.app),
                    base_url="http://test",
                ) as client:
                    first = await client.get("/java/example.com:25565")
                    cached = await client.get("/java/example.com:25565")

            redis_factory.assert_not_called()

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["result"])
        self.assertEqual(first.headers["X-FastAPI-Cache"], "MISS")
        self.assertEqual(cached.headers["X-FastAPI-Cache"], "HIT")
        self.assertEqual(cached.json(), first.json())
        lookup.assert_awaited_once()

    async def test_redis_default_and_explicit_selection_close_client(self):
        configurations = [
            ({}, "redis://localhost"),
            (
                {"CACHE_BACKEND": "redis", "REDIS_URL": "redis://cache:6379/2"},
                "redis://cache:6379/2",
            ),
        ]
        for environment, expected_url in configurations:
            with self.subTest(environment=environment):
                FastAPICache.reset()
                redis = MagicMock()
                redis.close = AsyncMock()

                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        api.aioredis,
                        "from_url",
                        return_value=redis,
                    ) as factory,
                ):
                    async with api.lifespan(api.app):
                        self.assertIsInstance(FastAPICache.get_backend(), RedisBackend)
                        factory.assert_called_once_with(expected_url)
                        redis.close.assert_not_awaited()

                redis.close.assert_awaited_once()

    async def test_invalid_backend_fails_startup_without_creating_redis_client(self):
        with (
            patch.dict(os.environ, {"CACHE_BACKEND": "invalid"}),
            patch.object(api.aioredis, "from_url") as factory,
        ):
            with self.assertRaisesRegex(ValueError, "CACHE_BACKEND"):
                async with api.lifespan(api.app):
                    self.fail("The app must not start with an invalid cache backend.")

            factory.assert_not_called()

        self.assertIsNone(FastAPICache._backend)


if __name__ == "__main__":
    unittest.main()
