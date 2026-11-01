import asyncio
import unittest
from unittest.mock import AsyncMock

from app.cache import MemoryCacheBackend, StatusCache


class SingleflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = MemoryCacheBackend()
        self.cache = StatusCache(self.backend)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.query = AsyncMock(side_effect=self.query_server)
        self.requests = []
        self.addAsyncCleanup(self.cache.close)
        self.addAsyncCleanup(self.cancel_requests)

    async def query_server(self, address):
        self.started.set()
        await self.release.wait()
        return {"result": True, "address": address}

    def request(self, address="example.com"):
        task = asyncio.create_task(self.cache.respond(address, self.query))
        self.requests.append(task)
        return task

    async def cancel_requests(self):
        for task in self.requests:
            task.cancel()
        await asyncio.gather(*self.requests, return_exceptions=True)

    async def test_same_address_shares_lookup_and_cache_write(self):
        self.backend.set = AsyncMock(wraps=self.backend.set)
        leader = self.request()
        await asyncio.wait_for(self.started.wait(), timeout=1)
        followers = [self.request() for _ in range(10)]
        await asyncio.sleep(0)
        self.release.set()
        responses = await asyncio.gather(leader, *followers)

        self.query.assert_awaited_once_with("example.com")
        self.backend.set.assert_awaited_once()
        self.assertEqual(self.cache._inflight, {})
        first = responses[0]
        for response in responses:
            self.assertEqual(response.body, first.body)
            self.assertEqual(response.headers["X-Cache"], "MISS")
            self.assertEqual(
                response.headers["X-Cache-Created-At"],
                first.headers["X-Cache-Created-At"],
            )

        cached = await self.cache.respond("example.com", self.query)
        self.assertEqual(cached.headers["X-Cache"], "HIT")
        self.query.assert_awaited_once()

    async def test_different_addresses_query_in_parallel(self):
        both_started = asyncio.Event()
        addresses = set()

        async def query(address):
            addresses.add(address)
            if len(addresses) == 2:
                both_started.set()
            await self.release.wait()
            return {"address": address}

        self.query.side_effect = query
        first = self.request("first.example.com")
        second = self.request("second.example.com")
        await asyncio.wait_for(both_started.wait(), timeout=1)
        self.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.query.await_count, 2)

    async def test_canceling_one_request_does_not_cancel_shared_lookup(self):
        leader = self.request()
        await asyncio.wait_for(self.started.wait(), timeout=1)
        follower = self.request()
        await asyncio.sleep(0)
        leader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await leader

        self.assertFalse(follower.done())
        self.release.set()
        response = await asyncio.wait_for(follower, timeout=1)
        self.assertEqual(response.status_code, 200)
        self.query.assert_awaited_once()
        self.assertEqual(self.cache._inflight, {})

    async def test_shared_failure_is_not_cached_and_next_request_retries(self):
        async def fail(address):
            await self.query_server(address)
            raise RuntimeError("query failed")

        self.query.side_effect = fail
        first = self.request()
        await asyncio.wait_for(self.started.wait(), timeout=1)
        second = self.request()
        await asyncio.sleep(0)
        self.release.set()
        errors = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(error, RuntimeError) for error in errors))
        self.query.assert_awaited_once()
        self.assertEqual(self.cache._inflight, {})
        self.assertEqual(self.backend._store, {})

        self.query.side_effect = self.query_server
        recovered = await self.cache.respond("example.com", self.query)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(self.query.await_count, 2)

    async def test_shared_lookup_is_released_after_all_waiters_cancel(self):
        leader = self.request()
        await asyncio.wait_for(self.started.wait(), timeout=1)
        shared_task = next(iter(self.cache._inflight.values()))
        leader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await leader

        self.assertFalse(shared_task.cancelled())
        self.release.set()
        await asyncio.wait_for(asyncio.shield(shared_task), timeout=1)
        self.assertEqual(self.cache._inflight, {})
        cached = await self.cache.respond("example.com", self.query)
        self.assertEqual(cached.headers["X-Cache"], "HIT")
        self.query.assert_awaited_once()

    async def test_shutdown_cancels_and_releases_pending_lookups(self):
        request = self.request()
        await asyncio.wait_for(self.started.wait(), timeout=1)
        await self.cache.close()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(self.cache._inflight, {})
        self.assertEqual(self.backend._store, {})

    async def test_cache_outage_still_shares_lookup(self):
        self.backend.get = AsyncMock(side_effect=ConnectionError("cache unavailable"))
        with self.assertLogs("app.cache", level="WARNING"):
            first = self.request()
            await asyncio.wait_for(self.started.wait(), timeout=1)
            second = self.request()
            await asyncio.sleep(0)
            self.release.set()
            responses = await asyncio.gather(first, second)

        self.query.assert_awaited_once()
        for response in responses:
            self.assertEqual(response.headers["X-Cache"], "BYPASS")
            self.assertNotIn("X-Cache-Created-At", response.headers)
        self.assertEqual(self.cache._inflight, {})


if __name__ == "__main__":
    unittest.main()
