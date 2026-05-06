import json
import os
import re
import time
import math

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from uvicorn import run as uvicorn_run

#환경변수
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))
AGENT_PORT = int(os.getenv("AGENT_PORT", "8080"))

#자원 사용률 계산
QUERIES = {
    "cpu_percent": '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
    "memory_percent": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
    "disk_percent": (
        '100 - ((node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs|aufs"} '
        '/ node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs|aufs"}) * 100)'
    ),
}


app = FastAPI(title="Metric Agent", version="1.0.0")

#바디정의
class AnalysisRequest(BaseModel):
    query: str


def strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_json_object(text: str) -> dict:
    cleaned = strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def query_ollama(prompt: str) -> str:
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
        return result.get("response", "").strip()
    except Exception as e:
        raise RuntimeError(f"Ollama 쿼리 실패: {str(e)}")


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

#계획생성프롬프트
def build_planner_prompt(user_query: str) -> str:
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


def parse_duration_to_seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)([smhdw])", duration.strip())
    if not match:
        raise ValueError(f"지원하지 않는 범위 형식입니다: {duration}")
    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 60 * 60, "d": 60 * 60 * 24, "w": 60 * 60 * 24 * 7}
    return amount * multipliers[unit]


def query_prometheus(query: str):
    response = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload["data"]["result"]


def query_prometheus_range(query: str, start: int, end: int, step_seconds: int):
    response = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step_seconds},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus range query failed: {payload}")
    return payload["data"]["result"]

#값 조회
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
        values.append({"labels": metric, "value": round(parsed, 2)})
    return values

#범위 조회
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
            points.append({"timestamp": int(timestamp), "value": round(parsed, 2)})
        if points:
            series.append({"labels": metric, "points": points})
    return series

#계획 보정
def normalize_query_plan(raw_plan, user_query: str) -> dict:
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


def build_fallback_plan(user_query: str) -> dict:
    lowered = user_query.lower()
    metric_name = "cpu_percent"
    is_range_question = any(word in lowered for word in ["지난", "최근", "어제", "주간", "일주일", "한달", "최대", "가장", "언제", "추세"])
    lookback = "1h" if not is_range_question else ("7d" if "일주일" in lowered else "1d")
    return {"query_type": "range" if is_range_question else "instant", "promql": QUERIES[metric_name], "range": lookback, "step_seconds": 300, "metric_name": metric_name, "reasoning": "fallback"}

#질문 -> promql 계획
def generate_query_plan(user_query: str) -> dict:
    prompt = build_planner_prompt(user_query)
    try:
        raw_plan = parse_json_object(query_ollama(prompt))
        return normalize_query_plan(raw_plan, user_query)
    except Exception:
        return build_fallback_plan(user_query)

#프로메테우스 조회
def execute_query_plan(plan: dict) -> dict:
    query_type = plan["query_type"]
    query = plan["promql"]
    if query_type == "instant":
        result = query_prometheus(query)
        values = extract_series_values(result)
        return {"mode": "instant", "query": query, "result_count": len(values), "series": values}

    duration_seconds = parse_duration_to_seconds(plan.get("range", "1h"))
    end = int(time.time())
    start = end - duration_seconds
    step_seconds = min(plan.get("step_seconds", 300), duration_seconds)
    step_seconds = max(15, step_seconds)
    result = query_prometheus_range(query, start, end, step_seconds)
    series = extract_range_series_values(result)
    return {"mode": "range", "query": query, "range": plan.get("range"), "step_seconds": step_seconds, "series_count": len(series), "series": series}

#쿼리생성, 실행
def analyze_metrics_with_llm(user_query: str) -> dict:
    plan = generate_query_plan(user_query)
    raw = execute_query_plan(plan)
    return {"user_query": user_query, "query_plan": plan, "raw_result": raw, "timestamp": int(time.time())}


@app.get("/")
async def root():
    return {"service": "metric-agent", "version": "1.0.0", "endpoints": {"analyze": "POST /analyze"}}

#사용자 입력
@app.post("/analyze")
async def analyze(request: AnalysisRequest):
    if not request.query or len(request.query.strip()) == 0:
        raise HTTPException(status_code=400, detail="query는 비워둘 수 없습니다.")
    try:
        return analyze_metrics_with_llm(request.query)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    print(f"metric-agent starting on :{AGENT_PORT}", flush=True)
    print(f"Prometheus: {PROMETHEUS_URL}", flush=True)
    print(f"Ollama: {OLLAMA_URL} (model: {OLLAMA_MODEL})", flush=True)
    uvicorn_run(app, host="0.0.0.0", port=AGENT_PORT, log_level="info")
