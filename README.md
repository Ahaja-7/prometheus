# Prometheus Node Metrics Stack

Docker Compose로 `node_exporter`, Prometheus, metric-agent를 실행하는 구성입니다.

## 실행

```bash
docker compose up -d --build
```

## 확인

```bash
docker compose ps
curl http://localhost:9091/-/ready
curl http://localhost:8080/health
curl http://localhost:8080/metrics-report
```

Prometheus UI는 `http://localhost:9091`에서 확인할 수 있습니다.

## 구성

- `node-exporter`: 호스트 머신 CPU, 메모리, 디스크, 네트워크 메트릭 노출
- `prometheus`: `host.docker.internal:9100`의 node_exporter 메트릭 수집
- `metric-agent`: Prometheus API를 조회해서 자원 사용률과 임계치 초과 알림 생성

Linux Docker에서 `host.docker.internal`을 사용할 수 있도록 Prometheus 서비스에 `host-gateway` 설정을 포함했습니다.
