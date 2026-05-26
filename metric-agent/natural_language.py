import json
from datetime import datetime, timedelta, timezone

import requests

from service import RequestError


NORMALIZED_QUERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["queries"],
    "properties": {
        "queries": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["server", "targets", "range"],
                "properties": {
                    "server": {"type": "string", "minLength": 1},
                    "targets": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "enum": ["cpu", "memory", "disk"]},
                    },
                    "range": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start", "end", "step"],
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "step": {"type": "string", "pattern": "^[0-9]+[smhd]$"},
                        },
                    },
                    "filters": {
                        "type": "object",
                        "additionalProperties": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "boolean"},
                                {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {
                                        "anyOf": [
                                            {"type": "string"},
                                            {"type": "number"},
                                            {"type": "boolean"},
                                        ]
                                    },
                                },
                            ]
                        },
                    },
                },
            },
        }
    },
}


def is_natural_language_payload(payload):
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("query"), str)
        and payload.get("query").strip()
        and "queries" not in payload
    )


def _extract_output_text(response_payload):
    if isinstance(response_payload.get("output_text"), str):
        return response_payload["output_text"]

    text_parts = []
    for item in response_payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                text_parts.append(content["text"])

    return "".join(text_parts)


class OpenAINormalizedQueryTranslator:
    def __init__(self, api_key, model, default_server, timeout_seconds, default_window_seconds):
        self.api_key = api_key
        self.model = model
        self.default_server = default_server
        self.timeout_seconds = timeout_seconds
        self.default_window_seconds = default_window_seconds

    def translate(self, natural_query):
        if not self.api_key:
            raise RequestError("OPENAI_NOT_CONFIGURED", "OPENAI_API_KEY is required for natural language queries", 500)

        now = datetime.now(timezone.utc)
        default_start = now - timedelta(seconds=self.default_window_seconds)
        defaults = {
            "server": self.default_server,
            "start": default_start.isoformat(),
            "end": now.isoformat(),
            "step": "60s",
        }

        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": [
                        {
                            "role": "developer",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": self._instructions(defaults),
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": natural_query}],
                        },
                    ],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "normalized_metric_query",
                            "strict": False,
                            "schema": NORMALIZED_QUERY_SCHEMA,
                        }
                    },
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RequestError("OPENAI_ERROR", str(exc), 502) from exc

        output_text = _extract_output_text(response.json())
        if not output_text:
            raise RequestError("OPENAI_EMPTY_RESPONSE", "OpenAI did not return a normalized query", 502)

        try:
            return json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise RequestError("OPENAI_INVALID_JSON", "OpenAI response was not valid JSON", 502) from exc

    def _instructions(self, defaults):
        return (
            "Convert the user's Korean or English metric request into the normalized metric query JSON schema. "
            "Return only JSON. Do not explain. "
            "Allowed targets are cpu, memory, and disk. The operation is always usage and must not be included. "
            "Use a queries array even for one query. "
            f"If the user does not specify a server, use {defaults['server']}. "
            f"If the user does not specify a time range, use start={defaults['start']} and end={defaults['end']}. "
            f"If the user does not specify step, use {defaults['step']}. "
            "For disk queries, use filters.mountpoint only when the user names a mount point. "
            "Never include filters.instance; the server field supplies the instance."
        )
