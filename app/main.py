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
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.backends.redis import RedisBackend
from fastapi_cache.decorator import cache
from mcstatus import JavaServer
from mcstatus.address import Address
from redis import asyncio as aioredis

CACHE_TTL_SECONDS = 60
QUERY_TIMEOUT_SECONDS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    cache_backend = os.getenv("CACHE_BACKEND", "redis")
    redis = None

    if cache_backend == "memory":
        backend = InMemoryBackend()
    elif cache_backend == "redis":
        redis_url = os.getenv("REDIS_URL", "redis://localhost")
        redis = aioredis.from_url(redis_url)
        backend = RedisBackend(redis)
    else:
        raise ValueError("CACHE_BACKEND must be 'memory' or 'redis'")

    try:
        FastAPICache.init(backend, prefix="mcstatus-api-cache")
        yield
    finally:
        if redis is not None:
            await redis.close()


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
    # 측정된 0은 유지하고, 알 수 없는 값만 null로 변환한다.
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
        # mcstatus 11.1.1은 정상적인 EOF도 일반 OSError로 보고한다.
        # 다른 프로토콜 오류까지 오프라인으로 처리하지 않도록 정확히 구분한다.
        if exc.args == ("Socket did not respond with any information!",):
            raise ConnectionError(str(exc)) from exc
        raise
    except (KeyError, TypeError, ValueError) as exc:
        # status 응답 파싱 오류는 외부 서버 오류로 변환한다.
        # 주소 검증과 API 응답 직렬화에서 발생하는 오류는 이 범위에 포함하지 않는다.
        raise OSError("Received invalid Minecraft status response") from exc


@app.get(
    "/java/{address}",
    responses={
        400: {"description": "Invalid target address (INVALID_ADDRESS)"},
        502: {"description": "Protocol or lookup infrastructure error (UPSTREAM_ERROR)"},
        500: {"description": "Internal API error (INTERNAL_ERROR)"},
    },
)
@cache(expire=CACHE_TTL_SECONDS, namespace="java-status")
async def get_java_server(address: str) -> JavaServerResponse:
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
        # DNS 조회와 status 요청 전체에 하나의 제한 시간을 적용한다.
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
        # 프로토콜 및 조회 환경 오류를 오프라인 결과로 캐싱하지 않는다.
        logger.warning("Minecraft status query failed address=%s error=%s", address, exc)
        raise HTTPException(
            status_code=502,
            detail={"code": "UPSTREAM_ERROR", "message": "Failed to query Minecraft server status"},
            headers={"Cache-Control": "no-store"},
        ) from exc

    # 기존 status 구조를 유지하고 유효하지 않은 지연 시간만 null로 변환한다.
    status_data = jsonable_encoder(status)
    status_data["latency"] = normalize_latency(status.latency)
    logger.info("Successfully retrieved status for server: %s", address)
    return JavaServerResponse(
        result=True,
        status=status_data,
        error=None,
        timestamp=datetime.now(timezone.utc),
    )
