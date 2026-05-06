# Prometheus Node Metrics Stack with LLM-assisted PromQL

Docker Compose로 `node_exporter`, Prometheus, Ollama, `metric-agent`를 실행하는 구성입니다.

이 리포지토리의 `metric-agent`는 사용자의 자연어 질문을 받아 LLM으로 PromQL 실행 계획을 생성하고, 해당 PromQL을 Prometheus에 실행하여 원시 결과(JSON)를 그대로 반환합니다.

## 실행

```bash
docker compose up -d --build
```

Ollama가 모델을 다운로드해야 하는 경우 초기 기동에 몇 분 소요될 수 있습니다.

## 빠른 예시

```bash
# POST /analyze: 사용자 질문을 보내면 LLM이 PromQL을 생성하고 결과 JSON을 반환합니다.
curl -X POST http://localhost:8080/analyze \
  -H "Content-Type: application/json" \
  -d '{"query": "현재 cpu 사용량을 알려줘"}'
```

예시 응답(간소화):

```json
{
  "user_query": "현재 cpu 사용량을 알려줘",
  "query_plan": {
    "query_type": "instant",
    "promql": "100 - (avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100)",
    "range": "1h",
    "step_seconds": 300,
    "metric_name": "cpu_percent"
  },
  "raw_result": {
    "mode": "instant",
    "query": "100 - (avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100)",
    "result_count": 1,
    "series": [
      { "labels": { "instance": "node-exporter:9100" }, "value": 7.5 }
    ]
  },
  "timestamp": 1234567890
}
```

## 동작 원리

- 클라이언트는 `/analyze`에 자연어 질문을 보냅니다.
- 서버는 내부적으로 LLM(Ollama)에 질문을 전달해 PromQL 계획(JSON)을 생성합니다.
- 생성된 `promql`을 Prometheus에 실행하고, Prometheus가 반환한 원시 시계열 JSON을 그대로 응답으로 돌려줍니다.

LLM은 오직 PromQL을 제안하는 역할만 하며, 최종 수치는 Prometheus가 반환한 원시값을 신뢰해야 합니다.

## 요청/응답 스키마

- 요청 (`POST /analyze`)
  - Content-Type: `application/json`
  - Body: `{ "query": "사용자 자연어 질문" }`

- 응답
  - `user_query`: 원본 질문
  - `query_plan`: LLM이 생성한 PromQL 실행 계획 (필드: `query_type`, `promql`, `range`, `step_seconds`, `metric_name`)
  - `raw_result`: Prometheus에서 반환한 원시 실행 결과 (instant: `series` 배열, range: `series` 배열 각 항목에 `points` 포함)
  - `timestamp`: 서버 응답 타임스탬프

## 주의 및 운영 권장사항

- 성능: LLM 생성 PromQL이 `range` 쿼리(특히 짧은 `step_seconds`)를 요구하면 Prometheus 쿼리 비용이 크게 증가할 수 있습니다. 필요 시 `step_seconds`를 크게 하고 기간을 적절히 제한하세요.
- 안전: 임의 PromQL을 사용자에게 허용하면 Prometheus에 과부하를 줄 수 있습니다. 운영 환경에서는 쿼리 화이트리스트, 요청당 포인트/시리즈 제한, 또는 쿼리 실행 시간 제한을 권장합니다.
- 신뢰성: LLM이 제안한 PromQL은 검토가 필요합니다. 항상 `raw_result`의 값을 신뢰하고, LLM이 결과를 해석해 제공하는 자연어 응답을 신뢰하지 마세요.

## 환경 변수

- `PROMETHEUS_URL`: Prometheus URL (기본값: `http://prometheus:9090`)
- `OLLAMA_URL`: Ollama URL (기본값: `http://localhost:11434`)
- `OLLAMA_MODEL`: 사용할 Ollama 모델 (기본값: `qwen3:8b`)
- `OLLAMA_TIMEOUT_SECONDS`: Ollama 요청 타임아웃(초) (기본값: `120`)
- `AGENT_PORT`: metric-agent 포트 (기본값: `8080`)

