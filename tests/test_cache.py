import os
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from redis import asyncio as aioredis

from app.cache import CACHE_PREFIX, CacheEntry, MemoryCacheBackend, RedisCacheBackend


class MemoryCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1000.0
        self.backend = MemoryCacheBackend(max_entries=2, clock=lambda: self.now)
        self.entry = CacheEntry(body=b'{"result":true}', created_at=datetime.now(timezone.utc))

    async def test_entry_expires_at_deadline_without_extending_on_reads(self):
        await self.backend.set("server", self.entry, 60)
        self.now += 59.5
        entry = await self.backend.get("server")
        self.assertEqual(entry, self.entry)

        self.now += 0.5
        self.assertIsNone(await self.backend.get("server"))
        self.assertEqual(self.backend._store, {})

    async def test_capacity_evicts_least_recently_used_entry(self):
        await self.backend.set("first", self.entry, 60)
        await self.backend.set("second", self.entry, 60)
        await self.backend.get("first")
        await self.backend.set("third", self.entry, 60)

        self.assertIsNone(await self.backend.get("second"))
        self.assertEqual(await self.backend.get("first"), self.entry)
        self.assertEqual(await self.backend.get("third"), self.entry)

    async def test_writes_remove_expired_entries_before_evicting_live_entries(self):
        await self.backend.set("expired", self.entry, 1)
        await self.backend.set("live", self.entry, 60)
        self.now += 1
        await self.backend.set("new", self.entry, 60)

        self.assertNotIn("expired", self.backend._store)
        self.assertEqual(await self.backend.get("live"), self.entry)
        self.assertEqual(len(self.backend._store), 2)

    async def test_instances_do_not_share_entries(self):
        await self.backend.set("server", self.entry, 60)
        other = MemoryCacheBackend()
        self.assertIsNone(await other.get("server"))


class RedisCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = MagicMock()
        self.redis.set = AsyncMock()
        self.redis.get = AsyncMock()
        self.backend = RedisCacheBackend(self.redis)
        self.entry = CacheEntry(body=b'{"result":true}', created_at=datetime.now(timezone.utc))

    async def test_roundtrip_preserves_body_and_creation_time(self):
        await self.backend.set("server", self.entry, 60)
        self.redis.set.assert_awaited_once_with(
            CACHE_PREFIX + "server",
            self.entry.encode(),
            ex=60,
        )

        encoded_entry = self.entry.encode()
        self.redis.get.return_value = encoded_entry.encode("utf-8")
        entry = await self.backend.get("server")
        self.assertEqual(entry, self.entry)
        self.redis.get.assert_awaited_once_with(CACHE_PREFIX + "server")

    async def test_missing_key_is_a_cache_miss(self):
        self.redis.get.return_value = None
        self.assertIsNone(await self.backend.get("server"))


@unittest.skipUnless(os.getenv("TEST_REDIS_URL"), "TEST_REDIS_URL is not configured")
class LiveRedisCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_clients_share_entry_metadata_and_expiration(self):
        redis_url = os.environ["TEST_REDIS_URL"]
        writer = aioredis.from_url(redis_url)
        reader = aioredis.from_url(redis_url)
        self.addAsyncCleanup(writer.close, close_connection_pool=True)
        self.addAsyncCleanup(reader.close, close_connection_pool=True)
        key_id = uuid.uuid4()
        key = key_id.hex
        self.addAsyncCleanup(writer.delete, CACHE_PREFIX + key)
        entry = CacheEntry(body=b'{"result":true}', created_at=datetime.now(timezone.utc))
        write_backend = RedisCacheBackend(writer)
        read_backend = RedisCacheBackend(reader)

        await write_backend.set(key, entry, 60)
        cached = await read_backend.get(key)
        self.assertEqual(cached, entry)
        self.assertGreater(await writer.pttl(CACHE_PREFIX + key), 0)

        await writer.pexpire(CACHE_PREFIX + key, 0)
        self.assertIsNone(await read_backend.get(key))


if __name__ == "__main__":
    unittest.main()
