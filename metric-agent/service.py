import math
import re
from datetime import datetime, timezone

from promql import build_query


SUPPORTED_OPERATIONS = {
    "cpu": {"usage"},
    "memory": {"usage"},
    "disk": {"usage"},
}
DEFAULT_OPERATION = "usage"

FILTER_ALLOWLIST = {
    "cpu": {"instance", "job"},
    "memory": {"instance", "job"},
    "disk": {"device", "fstype", "instance", "job", "mountpoint"},
}

STEP_RE = re.compile(r"^(\d+)(s|m|h|d)$")


class RequestError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def parse_rfc3339(value, field_name):
    if not isinstance(value, str) or not value:
        raise RequestError("INVALID_DATE", f"{field_name} must be a non-empty RFC3339 string")

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RequestError("INVALID_DATE", f"{field_name} must be a valid RFC3339 datetime") from exc

    if parsed.tzinfo is None:
        raise RequestError("INVALID_DATE", f"{field_name} must include a timezone")

    return parsed.astimezone(timezone.utc)


def parse_step_seconds(step):
    if not isinstance(step, str):
        raise RequestError("INVALID_STEP", "range.step must be a duration string like 60s, 5m, or 1h")

    match = STEP_RE.match(step)
    if not match:
        raise RequestError("INVALID_STEP", "range.step must use one unit: s, m, h, or d")

    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * multiplier

    if seconds < 15:
        raise RequestError("INVALID_STEP", "range.step must be at least 15s")
    if seconds > 86400:
        raise RequestError("INVALID_STEP", "range.step must be at most 1d")

    return seconds


def parse_targets(payload, field_name="targets"):
    target_value = payload.get("targets")
    if isinstance(target_value, list):
        targets = target_value
    else:
        raise RequestError("INVALID_TARGET", f"{field_name} must be a non-empty array")

    if not targets:
        raise RequestError("INVALID_TARGET", f"{field_name} must contain at least one target")

    invalid_targets = [target for target in targets if target not in SUPPORTED_OPERATIONS]
    if invalid_targets:
        allowed = ", ".join(sorted(SUPPORTED_OPERATIONS))
        invalid = ", ".join(str(target) for target in invalid_targets)
        raise RequestError("INVALID_TARGET", f"unsupported target: {invalid}. allowed: {allowed}")

    deduped = []
    for target in targets:
        if target not in deduped:
            deduped.append(target)
    return deduped


def validate_scalar_or_list(value, field_name):
    if isinstance(value, list):
        if not value:
            raise RequestError("INVALID_FILTER", f"{field_name} must not be an empty array")
        for item in value:
            if not isinstance(item, (str, int, float, bool)):
                raise RequestError("INVALID_FILTER", f"{field_name} must be a scalar value or an array of scalar values")
        return

    if not isinstance(value, (str, int, float, bool)):
        raise RequestError("INVALID_FILTER", f"{field_name} must be a scalar value or an array of scalar values")


def validate_server(value, field_name):
    if not isinstance(value, str) or not value:
        raise RequestError("INVALID_SERVER", f"{field_name} must be a non-empty string")


def normalize_filters(payload, field_name="filters"):
    filters = payload.get("filters", {})
    if filters is None:
        filters = {}
    if not isinstance(filters, dict):
        raise RequestError("INVALID_FILTER", f"{field_name} must be an object")

    filters = dict(filters)
    for label, value in filters.items():
        validate_scalar_or_list(value, f"{field_name}.{label}")

    return filters


def validate_range(query_range, field_name):
    if not isinstance(query_range, dict):
        raise RequestError("INVALID_RANGE", f"{field_name} must be an object with start, end, and step")

    start_raw = query_range.get("start")
    end_raw = query_range.get("end")
    step = query_range.get("step")
    start = parse_rfc3339(start_raw, f"{field_name}.start")
    end = parse_rfc3339(end_raw, f"{field_name}.end")
    parse_step_seconds(step)

    if start >= end:
        raise RequestError("INVALID_RANGE", f"{field_name}.start must be before {field_name}.end")

    return {
        "start": start_raw,
        "end": end_raw,
        "step": step,
    }


def validate_query(payload, index):
    if not isinstance(payload, dict):
        raise RequestError("INVALID_QUERY", f"queries[{index}] must be an object")
    if "operation" in payload:
        raise RequestError("INVALID_OPERATION", "operation is fixed to usage and must not be provided")
    if "target" in payload:
        raise RequestError("INVALID_TARGET", "use targets instead of target")
    if "servers" in payload:
        raise RequestError("INVALID_SERVER", "use server instead of servers")

    server = payload.get("server")
    validate_server(server, f"queries[{index}].server")
    targets = parse_targets(payload, f"queries[{index}].targets")
    query_range_spec = validate_range(payload.get("range"), f"queries[{index}].range")
    filters = normalize_filters(payload, f"queries[{index}].filters")

    if "instance" in filters:
        raise RequestError("INVALID_FILTER", "server cannot be used together with filters.instance")
    filters["instance"] = server

    allowed_any = set().union(*(FILTER_ALLOWLIST[target] for target in targets))
    unknown_filters = sorted(set(filters) - allowed_any)
    if unknown_filters:
        allowed = ", ".join(sorted(allowed_any))
        unknown = ", ".join(unknown_filters)
        raise RequestError("INVALID_FILTER", f"unsupported filters for requested targets: {unknown}. allowed: {allowed}")

    normalized_query = {
        "server": server,
        "targets": targets,
        "range": query_range_spec,
        "filters": filters,
    }
    requests = []
    for target in targets:
        target_filters = {
            label: value
            for label, value in filters.items()
            if label in FILTER_ALLOWLIST[target]
        }
        requests.append(
            {
                "query_index": index,
                "server": server,
                "target": target,
                "operation": DEFAULT_OPERATION,
                "range": query_range_spec,
                "filters": target_filters,
            }
        )

    return normalized_query, requests


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise RequestError("INVALID_JSON", "request body must be a JSON object")
    if "operation" in payload:
        raise RequestError("INVALID_OPERATION", "operation is fixed to usage and must not be provided")
    if "target" in payload or "targets" in payload:
        raise RequestError("INVALID_TARGET", "targets must be provided inside queries[]")
    if "server" in payload or "servers" in payload:
        raise RequestError("INVALID_SERVER", "server must be provided inside queries[]")
    if "range" in payload:
        raise RequestError("INVALID_RANGE", "range must be provided inside queries[]")

    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise RequestError("INVALID_QUERY", "queries must be a non-empty array")

    normalized_queries = []
    requests = []
    for index, query_payload in enumerate(queries):
        normalized_query, query_requests = validate_query(query_payload, index)
        normalized_queries.append(normalized_query)
        requests.extend(query_requests)

    return {
        "queries": normalized_queries,
        "requests": requests,
    }


def normalize_prometheus_values(payload):
    data = payload.get("data", {})
    for result in data.get("result", []):
        values = result.get("values", [])
        normalized_values = []
        for timestamp, value in values:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                normalized_values.append([timestamp, value])
                continue
            normalized_values.append([timestamp, round(parsed, 4) if math.isfinite(parsed) else value])
        result["values"] = normalized_values
    return payload


class MetricQueryService:
    def __init__(self, prometheus_client):
        self.prometheus_client = prometheus_client

    def handle_query(self, payload):
        request_spec = validate_payload(payload)
        requests = request_spec.get("requests", [request_spec])

        results = []
        for single_request in requests:
            query = build_query(
                single_request["target"],
                single_request["operation"],
                single_request["filters"],
            )
            prometheus_response = self.prometheus_client.query_range(query, single_request["range"])
            results.append(
                {
                    "request": single_request,
                    "query": query,
                    "result": normalize_prometheus_values(prometheus_response),
                }
            )

        return {
            "request": request_spec,
            "prometheus_url": self.prometheus_client.prometheus_url,
            "results": results,
        }
