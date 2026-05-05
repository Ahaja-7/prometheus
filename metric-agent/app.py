import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.respond({"status": "ok"})
            return

        if self.path == "/metrics-report":
            self.respond(latest_report)
            return

        self.respond(
            {
                "service": "metric-agent",
                "endpoints": ["/health", "/metrics-report"],
            }
        )

    def respond(self, body, status=200):
        encoded = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), Handler)
    print(f"metric-agent listening on :{AGENT_PORT}", flush=True)
    server.serve_forever()
