from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import AsyncIterator
from fastapi import FastAPI
from mcstatus import JavaServer
import logging
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from fastapi_cache.decorator import cache

from redis import asyncio as aioredis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    redis_url = os.getenv("REDIS_URL", "redis://localhost")
    redis = aioredis.from_url(redis_url)
    FastAPICache.init(RedisBackend(redis), prefix="mcstatus-api-cache")
    yield

app = FastAPI(lifespan=lifespan)

@dataclass
class JavaServerResponse:
    result: bool
    timestamp: datetime = None
    status: any = None
    error: any = None

@app.get("/java/{address}")
@cache(expire=60)
async def get_java_server(address: str) -> JavaServerResponse:
    try:
        logging.info(f"Looking up server at address: {address}")
        server = await JavaServer.async_lookup(address)
        status = await server.async_status()
        logging.info(f"Successfully retrieved status for server: {address}")
        return JavaServerResponse(
            result = True,
            status = status,
            error = None,
            timestamp = datetime.now(timezone.utc)
        )
    except Exception as e:
        logging.error(f"Error retrieving server status for {address}: {e}")
        return JavaServerResponse(
            result = False,
            status = None,
            error = str(e),
            timestamp = datetime.now(timezone.utc)
        )