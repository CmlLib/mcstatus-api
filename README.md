# MCStatus API

Minecraft Java 서버의 접속 가능 여부와 상태를 조회하는 API입니다. Python 3.10을 사용하며, Redis 또는 인메모리 캐시를 선택할 수 있습니다.

## 실행·배포

Docker와 Docker Compose가 필요합니다. 저장소 루트에서 실행합니다.

```bash
docker build -t ksi12345ab/mcstatus-api .
docker compose up -d
```

API는 `8000` 포트에서 실행됩니다. 코드 변경 후에는 이미지를 다시 빌드하고 같은 명령으로 컨테이너를 갱신합니다.

| 환경 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `CACHE_BACKEND` | `redis` | `redis` 또는 `memory`. 다른 값이면 시작 실패 |
| `REDIS_URL` | `redis://localhost` | Redis 선택 시 연결 주소. Compose에서는 `redis://redis:6379` |

Redis 없이 인메모리 캐시로 API만 실행하려면 다음 명령을 사용합니다.

```bash
CACHE_BACKEND=memory docker compose up -d mcstatus-api
```

인메모리 캐시는 프로세스별로 독립적이며 재시작하면 초기화됩니다. 최대 10,000개를 보관하고, 저장 시 만료 항목과 가장 오래 사용하지 않은 항목을 정리합니다.

```bash
# Check status and logs
docker compose ps
docker compose logs --tail=100 -f mcstatus-api redis

# Stop services
docker compose down
```

## API

- 운영 주소: `https://mcstatus.snowfrost.kr`
- 상태 조회: `GET /java/{address}` (예: `/java/example.com:25565`)
- 포트 생략 시 SRV 레코드를 조회하고, 레코드가 없으면 `25565`를 사용합니다.
- API 문서: 실행 중인 서버의 `/docs` 또는 `/openapi.json`

| HTTP | 의미 |
| --- | --- |
| 200, `result: true` | 접속 가능. `status`에 서버 상태 포함 |
| 200, `result: false` | 접속 불가. `status: null`, `error`에 진단 메시지 포함 |
| 400 | 잘못된 대상 주소 |
| 502 | DNS 환경 또는 서버 응답 오류 |
| 500 | API 내부 오류 |

`status.latency`는 API 서버에서 Minecraft 서버까지의 status 응답 시간(ms)이며, 측정값이 없으면 `null`입니다. `timestamp`는 조회 완료 시각(UTC)입니다. HTTP 오류를 Minecraft 서버의 오프라인 상태로 판단하지 마세요.

## 조회·캐시 동작

- DNS 조회와 status 요청을 합쳐 3초로 제한합니다.
- 접속 가능·불가 결과를 선택한 백엔드에 60초 캐싱합니다. 캐시 응답은 기존 측정값과 시각을 유지합니다.
- 같은 프로세스에서 같은 주소의 동시 요청은 하나의 조회 결과를 공유하며, 신규 조회를 공유한 응답은 모두 `MISS`로 표시합니다. 다른 주소는 병렬 조회하며, 워커·컨테이너 간 중복 조회는 막지 않습니다.
- 4xx/5xx 오류는 캐싱하지 않습니다.
- `X-Cache: HIT/MISS/BYPASS`로 캐시 재사용·신규 저장·우회를 구분합니다.
- `X-Cache-Created-At`은 캐시 저장 시각(UTC)이며 HIT에서도 유지됩니다. 저장하지 않은 응답에는 포함하지 않습니다.
- 요청의 캐시 지시어와 조건부 요청 헤더는 무시하며, 항상 서버의 60초 TTL을 따릅니다.
- Redis 읽기·쓰기 실패는 로그에 기록하며, 캐시 없이 실제 조회 결과를 반환합니다.
