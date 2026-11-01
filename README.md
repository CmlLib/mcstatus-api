# MCStatus API

Minecraft Java 서버 상태 API. Snowfrost 런처 연동 계약은 아래와 같습니다.

## 운영 호스트와 버전

- 담당 저장소: [CmlLib/mcstatus-api](https://github.com/CmlLib/mcstatus-api)
- 운영 endpoint: `GET https://mcstatus.snowfrost.kr/java/{address}`
- API 문서: [운영 OpenAPI](https://mcstatus.snowfrost.kr/openapi.json), [Swagger UI](https://mcstatus.snowfrost.kr/docs)
- `address`는 Minecraft 주소이며, 포트를 생략하면 SRV 조회 후 기본 포트 25565를 사용합니다. 예: `mc.hypixel.net`, `example.com:25565`. URL의 경로 매개변수로 인코딩해서 전달합니다.
- 2026-10-18 KST에 운영 OpenAPI의 `/java/{address}`와 실제 온라인 응답을 확인했습니다. OpenAPI 버전 표시는 `0.1.0`이며, 운영 호스트의 `/api/v1/servers/{target}`는 404입니다.
- 이 저장소는 `mcstatus-v2`와 별개입니다. 이번 변경은 기존 호스트·경로를 유지하며 v2 전환이 필요하지 않습니다. PR 병합·배포 전까지 아래 오류 구분과 nullable 보완은 운영에 적용되지 않습니다.

## 서버 응답 속도

이미 제공되는 **`status.latency`**를 사용합니다. 같은 의미의 추가 필드는 만들지 않습니다.

| 항목 | 계약 |
| --- | --- |
| 필드 | `status.latency` |
| 단위·타입 | 밀리초(ms), 소수점이 있는 JSON number 또는 `null` |
| 측정 구간 | API 서버 → Minecraft 서버의 Java status 요청·응답 |
| 측정 방식 | `mcstatus==11.1.1`의 `async_status()`가 status 요청을 보낸 직후부터 응답 패킷을 읽을 때까지 `perf_counter()`로 잰 시간 × 1000 |
| 포함하지 않는 시간 | DNS 조회, TCP 연결 수립, 사용자 PC → Minecraft 서버 ping, 런처 → HTTP API 왕복 시간 |
| 별도 ping 패킷 | 보내지 않습니다. status 응답의 기존 지연 시간을 그대로 사용합니다. |
| 온라인이지만 측정값 없음·유효하지 않음 | `result: true`, `status.latency: null`. 상태 데이터는 유지합니다. |
| 접속 불가·status 요청 타임아웃 | `result: false`, `status: null`. `status?.latency`를 nullable로 읽습니다. |
| API·조회 오류 | HTTP 4xx/5xx. 측정값과 온라인 여부를 갱신하지 않습니다. |

[mcstatus 11.1.1 측정 구현](https://github.com/py-mine/mcstatus/blob/v11.1.1/mcstatus/pinger.py)을 기준으로 합니다. `null`, 음수, NaN, Infinity 등의 유효하지 않은 값은 `null`로 반환합니다. 정상적으로 측정된 `0.0`은 유효하며, 알 수 없는 값을 0ms로 보정하지 않습니다. status 조회 자체가 실패하면 독립적인 ping 측정 결과는 없습니다.

## 온라인·오프라인·오류 구분

먼저 HTTP 상태를 확인한 뒤, 성공 응답에서만 `result`를 읽습니다.

| HTTP | 응답 | 소비자 동작 |
| --- | --- | --- |
| 200 | `result: true` | 온라인 상태와 nullable 지연 시간 갱신 |
| 200 | `result: false`, `status: null` | 접속 불가 관측 결과. 연결 거부·연결 종료·타임아웃·대상 DNS 레코드 없음 등에 해당 |
| 400 | `detail.code: INVALID_ADDRESS` | 주소 설정 오류. 온라인 여부 갱신 금지 |
| 502 | `detail.code: UPSTREAM_ERROR` | 잘못된 status 패킷·DNS 조회 환경 오류·기타 통신 조회 오류. 온라인 여부 갱신 금지 |
| 500 | `detail.code: INTERNAL_ERROR` | 예상하지 못한 API 내부 오류. 온라인 여부 갱신 금지 |
| 304 | 본문 없음 | 조건부 요청의 캐시 검증 성공. 클라이언트가 보관한 이전 정상 응답 재사용 |

시스템 DNS의 이름 없음(`EAI_NONAME`, 지원하는 환경에서는 `EAI_NODATA`)과 응답 없이 정상적으로 종료된 TCP 연결은 접속 불가로 처리합니다. 일시적인 DNS 장애(`EAI_AGAIN`)는 502로 처리합니다. status JSON의 필수 필드 누락이나 잘못된 필드 타입도 502이며 API 내부 오류로 분류하지 않습니다.

접속 불가는 API 서버에서 해당 Minecraft 서버에 유효한 status 연결을 할 수 없다는 관측입니다. 서버 프로세스 종료 자체를 보증하지 않으며 네트워크 단절도 포함합니다. DNS 조회와 status 요청을 합쳐 최대 3초를 적용합니다.

4xx/5xx 오류 응답에는 `result: false`를 넣지 않으며, 캐시에 저장하지 않습니다. `Cache-Control: no-store`를 반환합니다. API 오류·HTTP 요청 실패를 오프라인으로 치환하면 서버 열림 알림에 잘못된 전환이 생길 수 있습니다. 오류 동안 마지막으로 확인한 상태를 보존하거나 상태를 알 수 없음으로 표시해야 합니다.

### 온라인 예시 — HTTP 200

```json
{
  "result": true,
  "timestamp": "2026-10-17T19:40:00+00:00",
  "status": {
    "players": {"online": 3, "max": 20, "sample": null},
    "version": {"name": "1.21.1", "protocol": 767},
    "motd": {"parsed": ["Snowfrost", "r"], "raw": {"text": "Snowfrost"}, "bedrock": false},
    "latency": 12.75,
    "raw": {
      "version": {"name": "1.21.1", "protocol": 767},
      "players": {"online": 3, "max": 20},
      "description": {"text": "Snowfrost"}
    },
    "enforces_secure_chat": null,
    "icon": null
  },
  "error": null
}
```

온라인이지만 지연 시간만 알 수 없다면 위 응답의 `status.latency`가 `null`입니다.

### 접속 불가 예시 — HTTP 200

```json
{
  "result": false,
  "timestamp": "2026-10-17T19:40:00+00:00",
  "status": null,
  "error": "[Errno 111] Connection refused"
}
```

`error`는 운영체제와 실패 유형에 따라 달라지는 진단용 문자열입니다. 문자열을 파싱해서 상태를 판단하지 마세요.

### 조회 오류 예시 — HTTP 502

```json
{
  "detail": {
    "code": "UPSTREAM_ERROR",
    "message": "Failed to query Minecraft server status"
  }
}
```

### API 내부 오류 예시 — HTTP 500

```json
{
  "detail": {
    "code": "INTERNAL_ERROR",
    "message": "Internal server error"
  }
}
```

## 측정 시각과 캐시 정책

- **`timestamp`**는 조회가 완료되어 결과를 만든 UTC 시각(ISO 8601)입니다. HTTP 요청을 수신한 시각이나 캐시 응답을 반환한 시각이 아닙니다. 기존 필드를 그대로 사용합니다.
- 온라인·접속 불가 결과 모두 Redis에 **60초** 저장합니다. 캐시 hit는 TTL을 연장하거나 재측정하지 않으며, `timestamp`와 `status.latency`도 원래 값을 유지합니다.
- 만료 후 처음 들어온 요청이 다시 측정합니다. 주기적인 백그라운드 측정, 이력 저장, 동시 요청 병합은 없습니다.
- 기본 권장 polling 주기는 **60초**입니다. 여러 클라이언트는 요청 시각을 분산하세요. 60초 캐시에서는 polling을 더 자주 해도 새 측정이 보장되지 않습니다. 실제 서버 전환 감지는 캐시 TTL과 polling 간격을 합쳐 약 120초에 조회 시간이 더 걸릴 수 있습니다.
- 성공 응답의 `X-FastAPI-Cache: HIT/MISS`와 `Cache-Control: max-age=<남은 초>`로 캐시 재사용을 확인할 수 있습니다. `ETag`를 이용한 조건부 요청은 304를 반환할 수 있습니다. ETag는 프로세스 재시작·워커에 걸쳐 동일하다고 가정하지 마세요.
- 기존 캐시 라이브러리는 요청의 `Cache-Control: no-cache`로 강제 재측정·캐시 갱신, `no-store`로 캐시 조회·저장 생략을 지원합니다. 일반 polling에서는 사용하지 않는 것을 권장합니다.
- Redis 읽기·쓰기 실패는 캐시 라이브러리에서 기록하고 실제 조회로 대체합니다. 실제 조회가 성공하면 그 결과를 반환하므로 Redis 장애만으로 오프라인을 반환하지 않습니다.
- 배포 시 `java-status` 캐시 namespace를 사용해 이전 구현이 API 내부 오류를 오프라인으로 저장했던 캐시를 재사용하지 않습니다. 기존 키는 원래 TTL에 따라 만료됩니다.

## 런처 호환성과 적용

1. 기존 URL `https://mcstatus.snowfrost.kr/java/{address}`를 유지합니다.
2. 기존 `result`, `status.players.online`, `status.players.max`와 전체 `status` 구조를 유지합니다. `status.latency`를 nullable 실수로 읽고 ms로 표시하면 됩니다.
3. `EnsureSuccessStatusCode()` 등 HTTP 오류 확인을 유지합니다. 기존에 HTTP 200의 오프라인으로 위장했던 조회·API 오류가 이제 4xx/5xx가 되는 것은 의도적인 수정입니다.
4. 운영 응답과 현재 `mcstatus==11.1.1` 직렬화에는 `status.motd.clean`이 없습니다. `motd.raw`, `motd.parsed`, `motd.bedrock`이 제공됩니다. 런처가 `clean`을 읽으면 기존에도 null이 될 수 있으므로 MOTD 처리는 별도 소비 코드 작업에서 확인해야 합니다. 이번 변경은 MOTD 필드를 수정하지 않습니다.
5. 알림 판단에는 HTTP 200의 상태 관측만 사용하고, 오류·304 응답을 새로운 오프라인 관측으로 저장하지 마세요. 같은 `timestamp`는 같은 캐시 측정 결과입니다.

## 로컬 실행과 검증

Docker 배포와 `requirements.txt`의 고정 버전에 맞춰 **Python 3.10**을 사용합니다.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

테스트는 운영 의존성 버전에서 실행하며 추가 테스트 패키지가 필요하지 않습니다. 실제 캐시 데코레이터와 메모리 backend로 캐시 재사용·만료·304·오프라인 후 온라인 전환을 검증합니다. 로컬 TCP 서버로 실제 mcstatus status 요청·응답의 지연 시간과 연결 거부도 검증합니다. 지연 측정 불가, HTTP 오류, Redis 읽기·쓰기 실패는 주입해서 검증합니다. 운영 Redis 연결 자체와 운영 배포는 이 테스트의 대상이 아닙니다.

실행 시 Redis를 준비하고 `REDIS_URL`을 설정합니다(기본값 `redis://localhost`).

```bash
fastapi run app/main.py --port 8000
```
