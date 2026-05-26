import os
from http.server import ThreadingHTTPServer

from controller import create_handler
from prometheus_client import PrometheusClient
from service import MetricQueryService


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
AGENT_PORT = int(os.getenv("AGENT_PORT", "8080"))


def create_app():
    prometheus_client = PrometheusClient(PROMETHEUS_URL)
    metric_query_service = MetricQueryService(prometheus_client)
    return create_handler(metric_query_service)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), create_app())
    print(f"metric-agent listening on :{AGENT_PORT}", flush=True)
    server.serve_forever()
