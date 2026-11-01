import asyncio
import logging
import math
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from dns.exception import DNSException
from dns.resolver import NXDOMAIN, NoAnswer
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from mcstatus import JavaServer
from mcstatus.address import Address
from redis import asyncio as aioredis

from app.cache import MemoryCacheBackend, RedisCacheBackend, StatusCache

CACHE_TTL_SECONDS = 60
QUERY_TIMEOUT_SECONDS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    cache_backend = os.getenv("CACHE_BACKEND", "redis")
    redis = None

    if cache_backend == "memory":
        backend = MemoryCacheBackend()
    elif cache_backend == "redis":
        redis_url = os.getenv("REDIS_URL", "redis://localhost")
        redis = aioredis.from_url(redis_url)
        backend = RedisCacheBackend(redis)
    else:
        raise ValueError("CACHE_BACKEND must be 'memory' or 'redis'")

    try:
        application.state.status_cache = StatusCache(backend, expire=CACHE_TTL_SECONDS)
        yield
    finally:
        if redis is not None:
            await redis.close(close_connection_pool=True)


app = FastAPI(lifespan=lifespan)


@dataclass
class JavaServerResponse:
    result: bool
    timestamp: datetime | None = None
    status: Any = None
    error: str | None = None


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unexpected API error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        headers={"Cache-Control": "no-store"},
    )


def normalize_latency(latency: float | None) -> float | None:
    # Preserve measured zero latency; only unknown or invalid values become null.
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        return None
    if not math.isfinite(latency) or latency < 0:
        return None

    return latency


async def query_status(address: str):
    server = await JavaServer.async_lookup(address, timeout=QUERY_TIMEOUT_SECONDS)

    try:
        return await server.async_status()
    except socket.gaierror as exc:
        missing_name_errors = {socket.EAI_NONAME}
        if hasattr(socket, "EAI_NODATA"):
            missing_name_errors.add(socket.EAI_NODATA)

        if exc.errno in missing_name_errors:
            raise ConnectionError(str(exc)) from exc
        raise
    except OSError as exc:
        # mcstatus 11.1.1 reports an orderly EOF as a generic OSError.
        # Match the exact error to avoid treating protocol errors as offline results.
        if exc.args == ("Socket did not respond with any information!",):
            raise ConnectionError(str(exc)) from exc
        raise
    except (KeyError, TypeError, ValueError) as exc:
        # Treat status parsing failures as upstream errors.
        # Keep address validation and API response serialization outside this scope.
        raise OSError("Received invalid Minecraft status response") from exc


@app.get(
    "/java/{address}",
    response_model=JavaServerResponse,
    responses={
        400: {"description": "Invalid target address (INVALID_ADDRESS)"},
        502: {"description": "Protocol or lookup infrastructure error (UPSTREAM_ERROR)"},
        500: {"description": "Internal API error (INTERNAL_ERROR)"},
    },
)
async def get_java_server(address: str, request: Request) -> Response:
    status_cache = request.app.state.status_cache
    return await status_cache.respond(address, request, build_java_server_response)


async def build_java_server_response(address: str) -> JavaServerResponse:
    try:
        Address.parse_address(address, default_port=JavaServer.DEFAULT_PORT)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_ADDRESS", "message": "Invalid Minecraft server address"},
            headers={"Cache-Control": "no-store"},
        ) from exc

    try:
        logger.info("Looking up server at address: %s", address)
        # Apply one timeout to the entire DNS lookup and status request.
        status = await asyncio.wait_for(query_status(address), timeout=QUERY_TIMEOUT_SECONDS)
    except (
        asyncio.TimeoutError,
        TimeoutError,
        ConnectionError,
        NXDOMAIN,
        NoAnswer,
    ) as exc:
        logger.info("Minecraft server unavailable address=%s error=%s", address, exc)
        return JavaServerResponse(
            result=False,
            status=None,
            error=str(exc),
            timestamp=datetime.now(timezone.utc),
        )
    except (OSError, DNSException) as exc:
        # Do not cache protocol or lookup infrastructure errors as offline results.
        logger.warning("Minecraft status query failed address=%s error=%s", address, exc)
        raise HTTPException(
            status_code=502,
            detail={"code": "UPSTREAM_ERROR", "message": "Failed to query Minecraft server status"},
            headers={"Cache-Control": "no-store"},
        ) from exc

    # Preserve the status structure and serialize invalid latency as null.
    status_data = jsonable_encoder(status)
    status_data["latency"] = normalize_latency(status.latency)
    logger.info("Successfully retrieved status for server: %s", address)
    return JavaServerResponse(
        result=True,
        status=status_data,
        error=None,
        timestamp=datetime.now(timezone.utc),
    )
