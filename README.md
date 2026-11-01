# MCStatus API

An API for checking Minecraft Java server availability and status. Runs on Python 3.14 with a choice of Redis or in-memory caching.

## Run and deploy

Requires Docker and Docker Compose. Run these commands from the repository root:

```bash
docker build -t ksi12345ab/mcstatus-api .
docker compose up -d
```

The API listens on port `8000`. After changing the code, rebuild the image and run the same commands to update the container.

| Environment variable | Default | Description |
| --- | --- | --- |
| `CACHE_BACKEND` | `redis` | `redis` or `memory`. Other values prevent startup. |
| `CACHE_TTL_SECONDS` | `10` | Cache lifetime in seconds. Must be a positive integer. Invalid values prevent startup. |
| `REDIS_URL` | `redis://localhost` | Connection URL when using Redis. Compose sets this to `redis://redis:6379`. |

To run only the API with in-memory caching and no Redis:

```bash
CACHE_BACKEND=memory docker compose up -d mcstatus-api
```

To run locally with [uv](https://docs.astral.sh/uv/) and in-memory caching:

```bash
uv sync --frozen --no-dev
CACHE_BACKEND=memory uv run --frozen --no-dev fastapi run app/main.py --port 8000
```

The in-memory cache is isolated per process and cleared on restart. It holds up to 10,000 entries. Writes remove expired entries and evict the least recently used entries when needed.

```bash
# Check status and logs
docker compose ps
docker compose logs --tail=100 -f mcstatus-api redis

# Stop services
docker compose down
```

## API

- Production URL: `https://mcstatus.snowfrost.kr`
- Server status: `GET /java/{address}` (for example, `/java/example.com:25565`)
- If the port is omitted, an SRV lookup is performed, falling back to port `25565` when no record is found.
- API documentation: `/docs` or `/openapi.json` on the running server.

| HTTP | Meaning |
| --- | --- |
| 200, `result: true` | Server reachable. `status` contains the server details. |
| 200, `result: false` | Server unreachable. `status` is `null`; `error` contains a diagnostic message. |
| 400 | Invalid target address. |
| 502 | DNS infrastructure or upstream server response error. |
| 500 | Internal API error. |

`status.latency` is the status response time from the API server to the Minecraft server in milliseconds, or `null` if unavailable. `timestamp` is the query completion time in UTC. Do not interpret HTTP errors as a Minecraft server being offline.

## Queries and caching

- DNS lookup and the status request have a combined timeout of 3 seconds.
- Both reachable and unreachable results are cached in the selected backend for `CACHE_TTL_SECONDS` (10 seconds by default). Cached responses retain the original measurements and timestamp.
- Concurrent requests for the same address within one process share a single lookup. Responses sharing a new lookup all report `MISS`. Different addresses are queried in parallel. Requests are not coalesced across workers or containers.
- HTTP 4xx/5xx errors are not cached.
- `X-Cache: HIT/MISS/BYPASS` indicates a cached response, a newly stored result, or a cache bypass.
- `X-Cache-Created-At` is the cache creation time in UTC, preserved on hits. It is omitted when the result is not stored.
- Request cache directives and conditional request headers are ignored. The server always uses its configured TTL.
- Redis read/write failures are logged. The API returns the actual query result without caching.
