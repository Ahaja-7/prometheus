import json
import math
import os
import re
import threading
import time
from datetime import datetime

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from uvicorn import run as uvicorn_run


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))
AGENT_PORT = int(os.getenv("AGENT_PORT", "8080"))

THRESHOLDS = {
    "cpu_percent": float(os.getenv("CPU_WARN_PERCENT", "80")),
    "memory_percent": float(os.getenv("MEMORY_WARN_PERCENT", "80")),
    "disk_percent": float(os.getenv("DISK_WARN_PERCENT", "85")),
}

QUERIES = {
    "cpu_percent": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "memory_percent": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
    "disk_percent": (
        '100 - ((node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs|aufs"} '
        '/ node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs|aufs"}) * 100)'
    ),
}

latest_report = {
    "status": "starting",
    "prometheus_url": PROMETHEUS_URL,
    "checked_at": None,
    "metrics": {},
    "alerts": [],
    "error": None,
}

app = FastAPI(title="Metric Agent", version="1.0.0")
monitor_thread = None
monitor_thread_lock = threading.Lock()


class AnalysisRequest(BaseModel):
    """사용자의 메트릭 분석 요청"""
    query: str


class QueryPlan(BaseModel):
    """LLM이 생성하는 PromQL 실행 계획"""
    query_type: str
    promql: str
    range: str = "1h"
    step_seconds: int = 300
    metric_name: str | None = None
    reasoning: str | None = None


def query_prometheus(query):
    response = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": query},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload["data"]["result"]


def query_prometheus_range(query, start, end, step_seconds):
    response = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={
            "query": query,
            "start": start,
            "end": end,
            "step": step_seconds,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus range query failed: {payload}")
    return payload["data"]["result"]


def extract_series_values(result):
    values = []
    for item in result:
        metric = item.get("metric", {})
        value = item.get("value", [None, None])[1]
        if value is None:
            continue
        parsed = float(value)
        if not math.isfinite(parsed):
            continue

        values.append(
            {
                "labels": metric,
                "value": round(parsed, 2),
            }
        )
    return values


def extract_range_series_values(result):
    series = []
    for item in result:
        metric = item.get("metric", {})
        values = item.get("values", [])
        points = []

        for timestamp, value in values:
            parsed = float(value)
            if not math.isfinite(parsed):
                continue

            points.append(
                {
                    "timestamp": int(timestamp),
                    "value": round(parsed, 2),
                }
            )

        if points:
            series.append(
                {
                    "labels": metric,
                    "points": points,
                }
            )

    return series


def parse_duration_to_seconds(duration):
    match = re.fullmatch(r"(\d+)([smhdw])", duration.strip())
    if not match:
        raise ValueError(f"지원하지 않는 범위 형식입니다: {duration}")

    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 60 * 60 * 24,
        "w": 60 * 60 * 24 * 7,
    }
    return amount * multipliers[unit]


def strip_code_fences(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_json_object(text):
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def select_metric_hint(user_query):
    lowered = user_query.lower()
    if any(word in lowered for word in ["cpu", "프로세서", "cpu 사용", "cpu 사용량"]):
        return "cpu_percent"
    if any(word in lowered for word in ["memory", "mem", "메모리", "ram"]):
        return "memory_percent"
    if any(word in lowered for word in ["disk", "storage", "디스크", "용량", "저장"]):
        return "disk_percent"
    return None


def build_metric_catalog():
    return {
        "cpu_percent": {
            "description": "CPU 사용률(%)",
            "instant_query": QUERIES["cpu_percent"],
            "range_query": QUERIES["cpu_percent"],
        },
        "memory_percent": {
            "description": "메모리 사용률(%)",
            "instant_query": QUERIES["memory_percent"],
            "range_query": QUERIES["memory_percent"],
        },
        "disk_percent": {
            "description": "디스크 사용률(%)",
            "instant_query": QUERIES["disk_percent"],
            "range_query": QUERIES["disk_percent"],
        },
    }


def build_planner_prompt(user_query):
    metric_catalog = build_metric_catalog()
    return f"""당신은 Prometheus/PromQL 전문가입니다.

사용자의 질문을 보고 반드시 JSON만 출력하세요. 설명 문장, 코드 블록, 마크다운은 출력하지 마세요.

허용된 JSON 형식:
{{
  "query_type": "instant" 또는 "range",
  "promql": "실행할 PromQL",
  "range": "instant면 1h, range면 1h/1d/7d 같은 기간",
  "step_seconds": 숫자,
  "metric_name": "cpu_percent|memory_percent|disk_percent|custom",
  "reasoning": "짧은 한글 설명"
}}

규칙:
- "언제 최고였는지", "가장 높았던 때", "추세", "지난/최근/어제/주간" 같은 질문이면 query_type은 "range"로 하세요.
- range 질문에서는 query_range로 계산하기 좋은 시계열 PromQL을 작성하세요.
- 현재 시점만 묻는 질문이면 query_type은 "instant"로 하세요.
- 가능하면 아래 메트릭 카탈로그를 우선 활용하세요.
- 최고값과 시각은 코드가 계산하므로 PromQL은 시계열을 충분히 반환하도록 작성하세요.

메트릭 카탈로그:
{json.dumps(metric_catalog, ensure_ascii=False, indent=2)}

사용자 질문:
{user_query}
"""


def build_answer_prompt(user_query, plan, computed):
    return f"""당신은 시스템 모니터링 분석가입니다.

아래 계산 결과만 사용해서 사용자의 질문에 한글로 답하세요.
새로운 계산을 하지 말고, 숫자와 시각은 계산 결과에 나온 값만 그대로 사용하세요.
특히 숫자는 절대 추정하거나 바꾸지 말고, computed에 있는 값을 그대로 복사하세요.
가능하면 2문장 이내로 짧고 명확하게 답하세요.

사용자 질문:
{user_query}

실행 계획:
{json.dumps(plan, ensure_ascii=False, indent=2)}

계산 결과:
{json.dumps(computed, ensure_ascii=False, indent=2)}
"""


def build_report():
    metrics = {}
    alerts = []

    for name, query in QUERIES.items():
        values = extract_series_values(query_prometheus(query))
        metrics[name] = values

        threshold = THRESHOLDS[name]
        for series in values:
            if series["value"] >= threshold:
                alerts.append(
                    {
                        "metric": name,
                        "value": series["value"],
                        "threshold": threshold,
                        "labels": series["labels"],
                    }
                )

    return {
        "status": "warning" if alerts else "ok",
        "prometheus_url": PROMETHEUS_URL,
        "checked_at": int(time.time()),
        "metrics": metrics,
        "alerts": alerts,
        "error": None,
    }


def query_ollama(prompt: str) -> str:
    """Ollama LLM을 사용하여 프롬프트 처리"""
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
        return result.get("response", "").strip()
    except Exception as e:
        raise RuntimeError(f"Ollama 쿼리 실패: {str(e)}")


def build_fallback_plan(user_query):
    metric_name = select_metric_hint(user_query) or "cpu_percent"
    lowered = user_query.lower()
    is_range_question = any(
        word in lowered
        for word in ["지난", "최근", "어제", "주간", "일주일", "한달", "월간", "최대", "가장", "언제", "추세"]
    )
    lookback = "7d" if any(word in lowered for word in ["일주일", "주간", "지난 7일"]) else "1d"
    if "어제" in lowered:
        lookback = "1d"
    if not is_range_question:
        lookback = "1h"

    return {
        "query_type": "range" if is_range_question else "instant",
        "promql": QUERIES[metric_name],
        "range": lookback,
        "step_seconds": 300,
        "metric_name": metric_name,
        "reasoning": "fallback plan",
    }


def normalize_query_plan(raw_plan, user_query):
    if not isinstance(raw_plan, dict):
        raise ValueError("LLM 응답이 JSON 객체가 아닙니다.")

    plan = {
        "query_type": str(raw_plan.get("query_type", "range")).strip().lower(),
        "promql": str(raw_plan.get("promql", "")).strip(),
        "range": str(raw_plan.get("range", "1h")).strip(),
        "step_seconds": int(raw_plan.get("step_seconds", 300)),
        "metric_name": raw_plan.get("metric_name"),
        "reasoning": raw_plan.get("reasoning"),
    }

    if not plan["promql"]:
        raise ValueError("promql이 비어 있습니다.")

    if plan["query_type"] not in {"instant", "range"}:
        plan["query_type"] = "range"

    if plan["query_type"] == "range" and plan["range"] == "1h":
        lowered = user_query.lower()
        if any(word in lowered for word in ["일주일", "주간", "지난 7일"]):
            plan["range"] = "7d"
        elif "어제" in lowered:
            plan["range"] = "1d"
        elif any(word in lowered for word in ["최근", "지난"]):
            plan["range"] = "1d"

    plan["step_seconds"] = max(15, min(plan["step_seconds"], 3600))
    return plan


def generate_query_plan(user_query: str) -> dict:
    prompt = build_planner_prompt(user_query)
    try:
        raw_plan = parse_json_object(query_ollama(prompt))
        return normalize_query_plan(raw_plan, user_query)
    except Exception:
        return build_fallback_plan(user_query)


def find_peak_point(series):
    peak = None
    for item in series:
        labels = item.get("labels", {})
        for point in item.get("points", []):
            candidate = {
                "labels": labels,
                "timestamp": point["timestamp"],
                "value": point["value"],
            }
            if peak is None or candidate["value"] > peak["value"]:
                peak = candidate

    return peak


def format_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def execute_query_plan(plan):
    query_type = plan["query_type"]
    query = plan["promql"]

    if query_type == "instant":
        result = query_prometheus(query)
        values = extract_series_values(result)
        primary_value = values[0]["value"] if values else None
        return {
            "mode": "instant",
            "query": query,
            "result_count": len(values),
            "current_value": primary_value,
            "series": values,
            "peak": None,
        }

    duration_seconds = parse_duration_to_seconds(plan["range"])
    end = int(time.time())
    start = end - duration_seconds
    step_seconds = min(plan["step_seconds"], duration_seconds)
    if step_seconds <= 0:
        step_seconds = 300

    result = query_prometheus_range(query, start, end, step_seconds)
    series = extract_range_series_values(result)
    peak = find_peak_point(series)

    if peak is not None:
        peak = {
            **peak,
            "formatted_time": format_timestamp(peak["timestamp"]),
        }

    return {
        "mode": "range",
        "query": query,
        "range": plan["range"],
        "step_seconds": step_seconds,
        "series_count": len(series),
        "series": series,
        "peak": peak,
    }


def analyze_metrics_with_llm(user_query: str) -> dict:
    """사용자 질문을 바탕으로 PromQL을 만들고 결과를 후처리한다."""
    plan = generate_query_plan(user_query)
    computed = execute_query_plan(plan)

    if computed["peak"] is not None:
        computed_summary = {
            "peak_value": computed["peak"]["value"],
            "peak_time": computed["peak"]["formatted_time"],
            "peak_timestamp": computed["peak"]["timestamp"],
            "labels": computed["peak"]["labels"],
            "query_type": plan["query_type"],
            "range": plan.get("range"),
            "metric_name": plan.get("metric_name"),
        }
    else:
        computed_summary = {
            "query_type": plan["query_type"],
            "range": plan.get("range"),
            "metric_name": plan.get("metric_name"),
            "result_count": computed.get("result_count", computed.get("series_count", 0)),
            "current_value": computed.get("current_value"),
        }

    answer = query_ollama(build_answer_prompt(user_query, plan, computed_summary))

    return {
        "user_query": user_query,
        "query_plan": plan,
        "computed": computed_summary,
        "analysis": answer,
        "raw_result": computed,
        "timestamp": int(time.time()),
    }


def monitor_loop():
    global latest_report

    while True:
        try:
            latest_report = build_report()
            print(json.dumps(latest_report, ensure_ascii=False), flush=True)
        except Exception as exc:
            latest_report = {
                **latest_report,
                "status": "error",
                "checked_at": int(time.time()),
                "error": str(exc),
            }
            print(json.dumps(latest_report, ensure_ascii=False), flush=True)

        time.sleep(CHECK_INTERVAL_SECONDS)


def ensure_monitor_started():
    global monitor_thread

    with monitor_thread_lock:
        if monitor_thread is not None and monitor_thread.is_alive():
            return

        monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        monitor_thread.start()


@app.on_event("startup")
async def startup_event():
    ensure_monitor_started()


# FastAPI 엔드포인트


@app.get("/")
async def root():
    """루트 엔드포인트 - 이용 가능한 서비스 정보"""
    return {
        "service": "metric-agent",
        "version": "1.0.0",
        "endpoints": {
            "health": "GET /health",
            "metrics_report": "GET /metrics-report",
            "analyze": "POST /analyze",
        },
    }


@app.get("/health")
async def health():
    """헬스 체크 엔드포인트"""
    return {"status": "ok", "prometheus": PROMETHEUS_URL, "ollama": OLLAMA_URL}


@app.get("/metrics-report")
async def metrics_report():
    """최신 메트릭 리포트 조회"""
    return latest_report


@app.post("/analyze")
async def analyze(request: AnalysisRequest):
    """사용자 질문을 기반으로 메트릭 분석"""
    if not request.query or len(request.query.strip()) == 0:
        raise HTTPException(status_code=400, detail="query는 비워둘 수 없습니다.")

    try:
        result = analyze_metrics_with_llm(request.query)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    ensure_monitor_started()

    print(f"metric-agent starting on :{AGENT_PORT}", flush=True)
    print(f"Prometheus: {PROMETHEUS_URL}", flush=True)
    print(f"Ollama: {OLLAMA_URL} (model: {OLLAMA_MODEL})", flush=True)

    uvicorn_run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
