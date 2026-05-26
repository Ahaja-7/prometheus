import requests


class PrometheusClient:
    def __init__(self, prometheus_url):
        self.prometheus_url = prometheus_url.rstrip("/")

    def query_range(self, query, query_range):
        response = requests.get(
            f"{self.prometheus_url}/api/v1/query_range",
            params={
                "query": query,
                "start": query_range["start"],
                "end": query_range["end"],
                "step": query_range["step"],
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Prometheus query_range failed: {payload}")
        return payload
