import os
from http.server import ThreadingHTTPServer

from controller import create_handler
from natural_language import OpenAINormalizedQueryTranslator
from prometheus_client import PrometheusClient
from service import MetricQueryService


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
AGENT_PORT = int(os.getenv("AGENT_PORT", "8080"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
DEFAULT_QUERY_SERVER = os.getenv("DEFAULT_QUERY_SERVER", "host.docker.internal:9100")
DEFAULT_QUERY_WINDOW_SECONDS = int(os.getenv("DEFAULT_QUERY_WINDOW_SECONDS", "3600"))


def create_app():
    prometheus_client = PrometheusClient(PROMETHEUS_URL)
    metric_query_service = MetricQueryService(prometheus_client)
    natural_language_translator = OpenAINormalizedQueryTranslator(
        OPENAI_API_KEY,
        OPENAI_MODEL,
        DEFAULT_QUERY_SERVER,
        OPENAI_TIMEOUT_SECONDS,
        DEFAULT_QUERY_WINDOW_SECONDS,
    )
    return create_handler(metric_query_service, natural_language_translator)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", AGENT_PORT), create_app())
    print(f"metric-agent listening on :{AGENT_PORT}", flush=True)
    server.serve_forever()
