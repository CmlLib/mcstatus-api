import asyncio
import logging
import math
import os
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
    redis_url = os.getenv("REDIS_URL", "redis://localhost")
    redis = aioredis.from_url(redis_url)
    FastAPICache.init(RedisBackend(redis), prefix="mcstatus-api-cache")
    try:
        yield
    finally:
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
    # 알 수 없는 측정값은 0ms로 대체하지 않는다. 실제 측정된 0ms는 유효하다.
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        return None
    if not math.isfinite(latency) or latency < 0:
        return None

    return latency


async def query_status(address: str):
    server = await JavaServer.async_lookup(address, timeout=QUERY_TIMEOUT_SECONDS)
    return await server.async_status()


@app.get(
    "/java/{address}",
    description=(
        "Minecraft Java 서버 상태를 조회한다. status.latency는 API 서버에서 Minecraft status 응답까지의 "
        "지연 시간(밀리초)이며 측정 불가 시 null이다. timestamp는 조회 완료 UTC 시각이며 "
        "60초 캐시를 재사용해도 바뀌지 않는다. HTTP 200의 result=false는 접속 불가이고, "
        "HTTP 4xx/5xx는 조회 오류이므로 오프라인 전환으로 처리하지 않는다."
    ),
    responses={
        400: {"description": "대상 주소 형식 오류 (INVALID_ADDRESS)"},
        502: {"description": "프로토콜 응답 또는 조회 환경 오류 (UPSTREAM_ERROR)"},
        500: {"description": "API 내부 오류 (INTERNAL_ERROR)"},
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
        # DNS 조회와 status 요청 전체에 제한 시간을 적용한다.
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
        # 프로토콜 오류나 조회 환경 장애는 정상 오프라인 응답으로 캐싱하지 않는다.
        logger.warning("Minecraft status query failed address=%s error=%s", address, exc)
        raise HTTPException(
            status_code=502,
            detail={"code": "UPSTREAM_ERROR", "message": "Failed to query Minecraft server status"},
            headers={"Cache-Control": "no-store"},
        ) from exc

    # 기존 status 구조를 보존하면서 유효하지 않은 지연 값만 null로 직렬화한다.
    status_data = jsonable_encoder(status)
    status_data["latency"] = normalize_latency(status.latency)
    logger.info("Successfully retrieved status for server: %s", address)
    return JavaServerResponse(
        result=True,
        status=status_data,
        error=None,
        timestamp=datetime.now(timezone.utc),
    )
