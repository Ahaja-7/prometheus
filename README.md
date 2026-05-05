# Prometheus Node Metrics Stack with LLM Analysis

Docker Compose로 `node_exporter`, Prometheus, Ollama, metric-agent를 실행하는 구성입니다.

Qwen 8B LLM을 사용하여 사용자 질문 기반의 메트릭 분석을 제공합니다.

## 실행

```bash
docker compose up -d --build
```

초기 실행 시 Ollama가 Qwen 8B 모델을 다운로드하는데 몇 분이 걸릴 수 있습니다.

## 확인

```bash
docker compose ps

# 기본 상태 확인
curl http://localhost:8080/health

# 현재 메트릭 리포트 조회
curl http://localhost:8080/metrics-report

# LLM을 사용한 메트릭 분석 (POST 요청)
curl -X POST http://localhost:8080/analyze \
  -H "Content-Type: application/json" \
  -d '{"query": "CPU 사용률이 높은지 확인해줘"}'
```

Prometheus UI는 `http://localhost:9091`에서 확인할 수 있습니다.

## 구성

- `node-exporter`: 호스트 머신 CPU, 메모리, 디스크, 네트워크 메트릭 노출
- `prometheus`: node_exporter 메트릭 수집
- `ollama`: Qwen 8B LLM 모델 서빙 (http://localhost:11434)
- `metric-agent`: 
  - Prometheus 메트릭 수집 및 분석
  - FastAPI 기반 REST API 제공
  - LLM 기반 자연어 메트릭 분석

## API 엔드포인트

### GET /
서비스 정보 및 사용 가능한 엔드포인트 조회

### GET /health
헬스 체크

### GET /metrics-report
최신 메트릭 리포트 조회

응답 예시:
```json
{
  "status": "ok",
  "metrics": {
    "cpu_percent": [...],
    "memory_percent": [...],
    "disk_percent": [...]
  },
  "alerts": [...]
}
```

### POST /analyze
사용자 질문을 기반으로 메트릭 분석

요청:
```json
{
  "query": "메모리 사용률이 높은가?"
}
```

응답:
```json
{
  "user_query": "메모리 사용률이 높은가?",
  "metrics": {...},
  "analysis": "LLM이 생성한 분석 결과...",
  "timestamp": 1234567890
}
```

## 환경 변수

- `PROMETHEUS_URL`: Prometheus URL (기본값: http://prometheus:9090)
- `OLLAMA_URL`: Ollama URL (기본값: http://ollama:11434)
- `OLLAMA_MODEL`: 사용할 Ollama 모델 (기본값: qwen:8b)
- `CPU_WARN_PERCENT`: CPU 경고 임계치 (기본값: 80)
- `MEMORY_WARN_PERCENT`: 메모리 경고 임계치 (기본값: 80)
- `DISK_WARN_PERCENT`: 디스크 경고 임계치 (기본값: 85)
