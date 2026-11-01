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
        ttl, entry = await self.backend.get("server")
        self.assertEqual(ttl, 0)
        self.assertEqual(entry, self.entry)

        self.now += 0.5
        self.assertEqual(await self.backend.get("server"), (0, None))
        self.assertEqual(self.backend._store, {})

    async def test_capacity_evicts_least_recently_used_entry(self):
        await self.backend.set("first", self.entry, 60)
        await self.backend.set("second", self.entry, 60)
        await self.backend.get("first")
        await self.backend.set("third", self.entry, 60)

        self.assertEqual(await self.backend.get("second"), (0, None))
        self.assertEqual((await self.backend.get("first"))[1], self.entry)
        self.assertEqual((await self.backend.get("third"))[1], self.entry)

    async def test_writes_remove_expired_entries_before_evicting_live_entries(self):
        await self.backend.set("expired", self.entry, 1)
        await self.backend.set("live", self.entry, 60)
        self.now += 1
        await self.backend.set("new", self.entry, 60)

        self.assertNotIn("expired", self.backend._store)
        self.assertEqual((await self.backend.get("live"))[1], self.entry)
        self.assertEqual(len(self.backend._store), 2)

    async def test_instances_do_not_share_entries(self):
        await self.backend.set("server", self.entry, 60)
        other = MemoryCacheBackend()
        self.assertEqual(await other.get("server"), (0, None))


class RedisCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = MagicMock()
        self.redis.set = AsyncMock()
        self.pipeline = MagicMock()
        self.pipeline.execute = AsyncMock()
        self.redis.pipeline.return_value.__aenter__.return_value = self.pipeline
        self.backend = RedisCacheBackend(self.redis)
        self.entry = CacheEntry(body=b'{"result":true}', created_at=datetime.now(timezone.utc))

    async def test_roundtrip_preserves_body_creation_time_and_etag(self):
        await self.backend.set("server", self.entry, 60)
        self.redis.set.assert_awaited_once_with(
            CACHE_PREFIX + "server",
            self.entry.encode(),
            ex=60,
        )

        encoded_entry = self.entry.encode()
        self.pipeline.execute.return_value = [encoded_entry.encode("utf-8"), 42500]
        ttl, entry = await self.backend.get("server")
        self.assertEqual(ttl, 42)
        self.assertEqual(entry, self.entry)
        self.assertEqual(entry.etag, self.entry.etag)
        self.redis.pipeline.assert_called_once_with(transaction=True)
        self.pipeline.get.assert_called_once_with(CACHE_PREFIX + "server")
        self.pipeline.pttl.assert_called_once_with(CACHE_PREFIX + "server")

    async def test_missing_expired_or_persistent_keys_are_not_used(self):
        values = [(None, -2), (self.entry.encode(), 0), (self.entry.encode(), -1)]
        for value, ttl in values:
            with self.subTest(ttl=ttl):
                self.pipeline.execute.return_value = [value, ttl]
                self.assertEqual(await self.backend.get("server"), (0, None))

    async def test_etag_depends_on_content_not_process_or_cache_timestamp(self):
        other = CacheEntry(
            body=self.entry.body,
            created_at=datetime(
                2020,
                1,
                1,
                tzinfo=timezone.utc,
            ),
        )
        self.assertEqual(other.etag, self.entry.etag)
        changed = CacheEntry(body=b'{"result":false}', created_at=other.created_at)
        self.assertNotEqual(changed.etag, self.entry.etag)


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
        ttl, cached = await read_backend.get(key)
        self.assertGreaterEqual(ttl, 59)
        self.assertLessEqual(ttl, 60)
        self.assertEqual(cached, entry)
        self.assertEqual(cached.etag, entry.etag)
        self.assertGreater(await writer.pttl(CACHE_PREFIX + key), 0)

        await writer.pexpire(CACHE_PREFIX + key, 0)
        self.assertEqual(await read_backend.get(key), (0, None))


if __name__ == "__main__":
    unittest.main()
