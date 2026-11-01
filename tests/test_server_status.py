import asyncio
import json
import logging
import socket
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
from dns.resolver import NXDOMAIN, NoNameservers
from fastapi.encoders import jsonable_encoder
from mcstatus import JavaServer
from mcstatus.status_response import JavaStatusResponse

from app import main as api
from app.cache import MemoryCacheBackend, StatusCache

RAW_STATUS = {
    "version": {"name": "1.21.1", "protocol": 767},
    "players": {"online": 3, "max": 20},
    "description": {"text": "Snowfrost"},
}
ADDRESS = "example.com:25565"


class TestCache(MemoryCacheBackend):
    """Advance time explicitly to test expiration without waiting."""

    def __init__(self):
        self.now = 1000
        super().__init__(clock=self.current_time)

    def current_time(self):
        return self.now


def encode_varint(value):
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7

    encoded.append(value)
    return bytes(encoded)


async def read_packet(reader):
    length = 0
    shift = 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 127) << shift
        if byte < 128:
            break

        shift += 7

    return await reader.readexactly(length)


class ServerStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root_logger = logging.getLogger()
        log_level = root_logger.level
        root_logger.setLevel(logging.WARNING)
        self.addCleanup(root_logger.setLevel, log_level)
        self.backend = TestCache()
        cache_patch = patch.object(
            api.app.state,
            "status_cache",
            StatusCache(self.backend),
            create=True,
        )
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.status = JavaStatusResponse.build(RAW_STATUS, latency=12.75)
        self.server = JavaServer("127.0.0.1", 25565)
        self.server.async_status = AsyncMock(return_value=self.status)
        self.lookup = AsyncMock(return_value=self.server)
        lookup_patch = patch.object(api.JavaServer, "async_lookup", self.lookup)
        lookup_patch.start()
        self.addCleanup(lookup_patch.stop)
        logger_patch = patch.object(api, "logger")
        logger_patch.start()
        self.addCleanup(logger_patch.stop)

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)

    async def request(self, address=ADDRESS, **kwargs):
        return await self.client.get(f"/java/{address}", **kwargs)

    async def test_online_preserves_existing_fields_and_latency(self):
        response = await self.request()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"result", "timestamp", "status", "error"})
        self.assertTrue(data["result"])
        self.assertIsNone(data["error"])
        self.assertEqual(data["status"], jsonable_encoder(self.status))
        self.assertEqual(data["status"]["players"], {"online": 3, "max": 20, "sample": None})
        self.assertEqual(data["status"]["latency"], 12.75)
        self.assertTrue(data["timestamp"].endswith("+00:00"))
        self.lookup.assert_awaited_once_with(ADDRESS, timeout=api.QUERY_TIMEOUT_SECONDS)

    async def test_unknown_latency_is_null_but_server_stays_online(self):
        for latency in (None, float("nan"), float("inf"), -1, "unknown", True):
            with self.subTest(latency=latency):
                self.status.latency = latency
                response = await self.request(headers={"Cache-Control": "no-store"})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["result"])
                self.assertIsNone(response.json()["status"]["latency"])
                self.assertEqual(response.json()["status"]["players"]["online"], 3)

    async def test_measured_zero_is_valid(self):
        self.status.latency = 0.0
        response = await self.request()
        self.assertEqual(response.json()["status"]["latency"], 0.0)

    async def test_connection_failure_and_timeout_are_offline(self):
        for error in (ConnectionRefusedError("refused"), TimeoutError("timed out"), NXDOMAIN()):
            with self.subTest(error=type(error).__name__):
                self.lookup.side_effect = error
                response = await self.request(headers={"Cache-Control": "no-store"})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertFalse(data["result"])
                self.assertIsNone(data["status"])
                self.assertIsInstance(data["error"], str)

    async def test_unknown_api_error_is_not_offline_or_cached(self):
        self.server.async_status.side_effect = RuntimeError("private implementation details")
        response = await self.request()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"]["code"], "INTERNAL_ERROR")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("result", response.json())
        self.assertNotIn("private", response.text)
        self.assertEqual(self.backend._store, {})

        self.server.async_status.side_effect = None
        recovered = await self.request()
        self.assertTrue(recovered.json()["result"])

    async def test_query_errors_are_not_offline_or_cached(self):
        for error in (OSError("Received invalid JSON"), NoNameservers()):
            with self.subTest(error=type(error).__name__):
                self.server.async_status.side_effect = error
                response = await self.request()
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["detail"]["code"], "UPSTREAM_ERROR")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("result", response.json())
                self.assertEqual(self.backend._store, {})

    async def test_invalid_address_is_bad_request(self):
        for address in ("example.com:no", "example.com:65536", ":25565"):
            with self.subTest(address=address):
                response = await self.request(address)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["detail"]["code"], "INVALID_ADDRESS")

        self.lookup.assert_not_awaited()
        self.assertEqual(self.backend._store, {})

    async def test_cache_reuses_latency_timestamp_and_expires(self):
        first = await self.request()
        self.assertEqual(first.headers["X-Cache"], "MISS")
        self.assertEqual(first.headers["X-FastAPI-Cache"], "MISS")
        created_at = first.headers["X-Cache-Created-At"]
        self.assertIsNotNone(datetime.fromisoformat(created_at).tzinfo)
        self.assertEqual(first.headers["Cache-Control"], "max-age=60")
        self.status.latency = 99
        self.backend.now += 59
        cached = await self.request()
        self.assertEqual(cached.headers["X-FastAPI-Cache"], "HIT")
        self.assertEqual(cached.headers["Cache-Control"], "max-age=1")
        self.assertEqual(cached.headers["X-Cache"], "HIT")
        self.assertEqual(cached.headers["X-Cache-Created-At"], created_at)
        self.assertEqual(cached.json(), first.json())
        self.lookup.assert_awaited_once()

        self.backend.now += 2
        refreshed = await self.request()
        self.assertEqual(refreshed.headers["X-FastAPI-Cache"], "MISS")
        self.assertNotEqual(refreshed.headers["X-Cache-Created-At"], created_at)
        self.assertEqual(refreshed.json()["status"]["latency"], 99)
        self.assertNotEqual(refreshed.json()["timestamp"], first.json()["timestamp"])
        self.assertEqual(self.lookup.await_count, 2)

    async def test_offline_is_cached_then_online_after_expiry(self):
        self.lookup.side_effect = ConnectionRefusedError("refused")
        offline = await self.request()
        self.lookup.side_effect = None
        cached = await self.request()
        self.assertEqual(cached.json(), offline.json())
        self.assertEqual(cached.headers["X-FastAPI-Cache"], "HIT")
        self.lookup.assert_awaited_once()

        self.backend.now += 61
        refreshed = await self.request()
        self.assertTrue(refreshed.json()["result"])

    async def test_null_latency_survives_cache_roundtrip(self):
        self.status.latency = None
        first = await self.request()
        cached = await self.request()
        self.assertEqual(cached.json(), first.json())
        self.assertIsNone(cached.json()["status"]["latency"])
        self.lookup.assert_awaited_once()

    async def test_conditional_cache_hit_returns_304(self):
        first = await self.request()
        cached = await self.request(headers={"If-None-Match": first.headers["ETag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b"")
        self.assertEqual(cached.headers["X-Cache"], "HIT")
        self.assertEqual(
            cached.headers["X-Cache-Created-At"],
            first.headers["X-Cache-Created-At"],
        )
        self.lookup.assert_awaited_once()

    async def test_conditional_request_matches_strong_tag_in_a_list(self):
        first = await self.request()
        strong_tag = first.headers["ETag"].removeprefix("W/")
        cached = await self.request(headers={"If-None-Match": f'"other", {strong_tag}'})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.content, b"")
        self.lookup.assert_awaited_once()

    async def test_no_store_bypasses_reads_and_writes_without_replacing_cached_value(self):
        first = await self.request()
        self.status.latency = 99
        with (
            patch.object(self.backend, "get", AsyncMock()) as read,
            patch.object(self.backend, "set", AsyncMock()) as write,
        ):
            fresh = await self.request(headers={"Cache-Control": "max-age=0, No-Store"})

        self.assertEqual(fresh.json()["status"]["latency"], 99)
        self.assertEqual(fresh.headers["X-Cache"], "BYPASS")
        self.assertEqual(fresh.headers["Cache-Control"], "no-store")
        self.assertNotIn("X-Cache-Created-At", fresh.headers)
        read.assert_not_awaited()
        write.assert_not_awaited()
        cached = await self.request()
        self.assertEqual(cached.json(), first.json())

    async def test_no_cache_refreshes_body_and_cache_creation_time(self):
        first = await self.request()
        self.status.latency = 99
        fresh = await self.request(headers={"Cache-Control": "max-age=0, No-Cache"})
        self.assertEqual(fresh.headers["X-Cache"], "MISS")
        self.assertEqual(fresh.json()["status"]["latency"], 99)
        self.assertNotEqual(
            fresh.headers["X-Cache-Created-At"],
            first.headers["X-Cache-Created-At"],
        )
        cached = await self.request()
        self.assertEqual(cached.json(), fresh.json())
        self.assertEqual(cached.headers["X-Cache"], "HIT")
        self.assertEqual(self.lookup.await_count, 2)

    async def test_redis_read_failure_falls_back_to_actual_probe(self):
        with patch.object(self.backend, "get", AsyncMock(side_effect=ConnectionError())):
            with self.assertLogs("app.cache", level="WARNING"):
                response = await self.request()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"])
        self.assertEqual(response.headers["X-Cache"], "BYPASS")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("X-Cache-Created-At", response.headers)
        self.lookup.assert_awaited_once()

    async def test_redis_write_failure_keeps_actual_result(self):
        with patch.object(self.backend, "set", AsyncMock(side_effect=ConnectionError())):
            with self.assertLogs("app.cache", level="WARNING"):
                response = await self.request()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"])
        self.assertEqual(response.json()["status"]["latency"], 12.75)
        self.assertEqual(response.headers["X-Cache"], "BYPASS")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("X-Cache-Created-At", response.headers)

    async def test_dns_is_included_in_total_timeout(self):
        async def stalled_lookup(*args, **kwargs):
            lookup_finished = asyncio.Event()
            await lookup_finished.wait()

        self.lookup.side_effect = stalled_lookup
        with patch.object(api, "QUERY_TIMEOUT_SECONDS", 0.02):
            response = await asyncio.wait_for(self.request(), timeout=1)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["result"])
        self.assertIsNone(response.json()["status"])

    async def test_system_dns_name_absence_is_offline_and_cached(self):
        self.lookup.return_value = JavaServer("missing.invalid", 25565)
        missing_name_errors = {socket.EAI_NONAME}
        if hasattr(socket, "EAI_NODATA"):
            missing_name_errors.add(socket.EAI_NODATA)

        for error_code in missing_name_errors:
            with self.subTest(error_code=error_code):
                self.backend._store.clear()
                connection = AsyncMock(
                    side_effect=socket.gaierror(error_code, "Name or service not known"),
                )
                with patch("asyncio.open_connection", connection):
                    response = await self.request()
                    cached = await self.request()

                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["result"])
                self.assertIsNone(response.json()["status"])
                self.assertEqual(cached.json(), response.json())
                self.assertEqual(cached.headers["X-FastAPI-Cache"], "HIT")
                connection.assert_awaited_once()

    async def test_temporary_system_dns_failure_is_not_offline_or_cached(self):
        self.lookup.return_value = JavaServer("example.com", 25565)
        connection = AsyncMock(
            side_effect=socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        )
        with patch("asyncio.open_connection", connection):
            response = await self.request()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["code"], "UPSTREAM_ERROR")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.backend._store, {})

    async def request_real_status(self, raw):
        closed = asyncio.Event()

        async def handle(reader, writer):
            try:
                if raw is None:
                    await read_packet(reader)
                    await read_packet(reader)
                    # Send an orderly EOF rather than resetting the TCP connection.
                    writer.write_eof()
                    await reader.read()
                    return

                # Respond to each of the three attempts mcstatus makes on the same connection.
                for _ in range(3):
                    await read_packet(reader)
                    await read_packet(reader)
                    raw_json = json.dumps(raw)
                    data = raw_json.encode()
                    packet = b"\x00" + encode_varint(len(data)) + data
                    writer.write(encode_varint(len(packet)) + packet)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        listener = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with listener:
            port = listener.sockets[0].getsockname()[1]
            self.lookup.return_value = JavaServer("127.0.0.1", port)
            response = await self.request()
            await asyncio.wait_for(closed.wait(), timeout=1)

        return response

    async def test_real_tcp_eof_is_offline_and_cached(self):
        response = await self.request_real_status(None)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["result"])
        self.assertIsNone(response.json()["status"])

        cached = await self.request()
        self.assertEqual(cached.json(), response.json())
        self.assertEqual(cached.headers["X-FastAPI-Cache"], "HIT")
        self.lookup.assert_awaited_once()

    async def test_real_malformed_status_is_upstream_error_and_not_cached(self):
        malformed_responses = [
            {},
            {**RAW_STATUS, "players": None},
            [],
        ]
        for raw in malformed_responses:
            with self.subTest(raw=raw):
                response = await self.request_real_status(raw)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["detail"]["code"], "UPSTREAM_ERROR")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(self.backend._store, {})

    async def test_real_minecraft_status_exchange_measures_latency_without_ping(self):
        requests = []
        closed = asyncio.Event()

        async def handle(reader, writer):
            try:
                requests.append(await read_packet(reader))
                requests.append(await read_packet(reader))
                await asyncio.sleep(0.03)
                raw_json = json.dumps(RAW_STATUS)
                raw = raw_json.encode()
                packet = b"\x00" + encode_varint(len(raw)) + raw
                writer.write(encode_varint(len(packet)) + packet)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        listener = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with listener:
            port = listener.sockets[0].getsockname()[1]
            self.lookup.return_value = JavaServer("127.0.0.1", port)
            response = await self.request()
            await asyncio.wait_for(closed.wait(), timeout=1)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"])
        self.assertGreaterEqual(response.json()["status"]["latency"], 20)
        self.assertEqual(response.json()["status"]["raw"], RAW_STATUS)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1], b"\x00")

    async def test_real_tcp_connection_refusal_is_offline(self):
        # Bind without listening to reproduce a refused connection.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            self.lookup.return_value = JavaServer("127.0.0.1", port)
            response = await self.request()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["result"])
        self.assertIsNone(response.json()["status"])


if __name__ == "__main__":
    unittest.main()
