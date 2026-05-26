import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests

from natural_language import is_natural_language_payload
from service import RequestError


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_handler(metric_query_service, natural_language_translator=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path

            if path == "/health":
                self.respond({"status": "ok"})
                return

            if path in {"/", "/index.html"}:
                self.respond_html(STATIC_DIR / "index.html")
                return

            self.respond(
                {
                    "service": "metric-agent",
                    "endpoints": {
                        "GET": ["/health"],
                        "POST": ["/query"],
                    },
                    "query_formats": [
                        {"queries": "normalized metric query JSON"},
                        {"query": "natural language metric request"},
                    ],
                }
            )

        def do_POST(self):
            path = urlparse(self.path).path
            if path != "/query":
                self.respond_error("NOT_FOUND", "endpoint not found", 404)
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.respond_error("INVALID_REQUEST", "invalid Content-Length", 400)
                return

            if content_length <= 0:
                self.respond_error("INVALID_JSON", "request body is required", 400)
                return

            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body)
                if is_natural_language_payload(payload):
                    if natural_language_translator is None:
                        self.respond_error("NATURAL_LANGUAGE_DISABLED", "natural language queries are not configured", 500)
                        return
                    payload = natural_language_translator.translate(payload["query"])
                self.respond(metric_query_service.handle_query(payload))
            except json.JSONDecodeError:
                self.respond_error("INVALID_JSON", "request body must be valid JSON", 400)
            except RequestError as exc:
                self.respond_error(exc.code, exc.message, exc.status)
            except requests.RequestException as exc:
                self.respond_error("PROMETHEUS_ERROR", str(exc), 502)
            except Exception as exc:
                self.respond_error("INTERNAL_ERROR", str(exc), 500)

        def respond_error(self, code, message, status):
            self.respond({"error": {"code": code, "message": message}}, status)

        def respond(self, body, status=200):
            encoded = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def respond_html(self, path, status=200):
            try:
                encoded = path.read_bytes()
            except FileNotFoundError:
                self.respond_error("NOT_FOUND", "page not found", 404)
                return

            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt, *args):
            return

    return Handler
