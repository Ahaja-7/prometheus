import unittest

from natural_language import is_natural_language_payload
from promql import build_cpu_usage_query, build_disk_usage_query, build_memory_usage_query
from service import MetricQueryService, RequestError, validate_payload


VALID_RANGE = {
    "start": "2026-05-06T00:00:00+09:00",
    "end": "2026-05-06T01:00:00+09:00",
    "step": "60s",
}


def query_payload(server="server-a:9100", targets=None, query_range=None, filters=None):
    payload = {
        "server": server,
        "targets": targets or ["cpu"],
        "range": query_range or VALID_RANGE,
    }
    if filters is not None:
        payload["filters"] = filters
    return {"queries": [payload]}


class ValidatePayloadTest(unittest.TestCase):
    def test_accepts_valid_cpu_usage_request(self):
        payload = query_payload(targets=["cpu"], filters={"job": "node"})

        validated = validate_payload(payload)

        self.assertEqual(len(validated["requests"]), 1)
        self.assertEqual(validated["requests"][0]["server"], "server-a:9100")
        self.assertEqual(validated["requests"][0]["target"], "cpu")
        self.assertEqual(validated["requests"][0]["operation"], "usage")
        self.assertEqual(validated["requests"][0]["filters"], {"job": "node", "instance": "server-a:9100"})

    def test_rejects_operation(self):
        payload = query_payload()
        payload["queries"][0]["operation"] = "usage"

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_OPERATION")

    def test_rejects_unknown_target(self):
        payload = query_payload(targets=["network"])

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_TARGET")

    def test_rejects_start_after_end(self):
        payload = query_payload(
            query_range={
                "start": "2026-05-06T02:00:00+09:00",
                "end": "2026-05-06T01:00:00+09:00",
                "step": "60s",
            }
        )

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_RANGE")

    def test_rejects_filter_not_allowed_for_target(self):
        payload = query_payload(targets=["cpu"], filters={"mountpoint": "/"})

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_FILTER")

    def test_accepts_multiple_targets(self):
        payload = query_payload(
            targets=["cpu", "memory", "disk"],
            filters={"job": "node", "mountpoint": "/data"},
        )

        validated = validate_payload(payload)

        self.assertEqual(validated["queries"][0]["targets"], ["cpu", "memory", "disk"])
        self.assertEqual(len(validated["requests"]), 3)
        self.assertEqual(validated["requests"][0]["filters"], {"job": "node", "instance": "server-a:9100"})
        self.assertEqual(validated["requests"][1]["filters"], {"job": "node", "instance": "server-a:9100"})
        self.assertEqual(
            validated["requests"][2]["filters"],
            {"job": "node", "mountpoint": "/data", "instance": "server-a:9100"},
        )

    def test_accepts_multiple_queries_with_different_servers_and_ranges(self):
        payload = {
            "queries": [
                {
                    "server": "server-a:9100",
                    "targets": ["cpu"],
                    "range": VALID_RANGE,
                },
                {
                    "server": "server-b:9100",
                    "targets": ["cpu", "memory"],
                    "range": {
                        "start": "2026-05-06T02:00:00+09:00",
                        "end": "2026-05-06T03:00:00+09:00",
                        "step": "60s",
                    },
                },
            ]
        }

        validated = validate_payload(payload)

        self.assertEqual(len(validated["queries"]), 2)
        self.assertEqual(len(validated["requests"]), 3)
        self.assertEqual(validated["requests"][0]["server"], "server-a:9100")
        self.assertEqual(validated["requests"][1]["server"], "server-b:9100")
        self.assertEqual(validated["requests"][2]["target"], "memory")

    def test_rejects_top_level_targets(self):
        payload = {
            "targets": ["cpu"],
            "range": VALID_RANGE,
            "server": "server-a:9100",
        }

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_TARGET")

    def test_rejects_target_alias(self):
        payload = query_payload()
        payload["queries"][0].pop("targets")
        payload["queries"][0]["target"] = "cpu"

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_TARGET")

    def test_rejects_servers_alias(self):
        payload = query_payload()
        payload["queries"][0].pop("server")
        payload["queries"][0]["servers"] = ["server-a:9100"]

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_SERVER")

    def test_rejects_instance_filter(self):
        payload = query_payload(filters={"instance": "server-b:9100"})

        with self.assertRaises(RequestError) as context:
            validate_payload(payload)

        self.assertEqual(context.exception.code, "INVALID_FILTER")


class NaturalLanguagePayloadTest(unittest.TestCase):
    def test_detects_natural_language_query_payload(self):
        self.assertTrue(is_natural_language_payload({"query": "현재 CPU 사용량 알려줘"}))

    def test_does_not_treat_normalized_query_as_natural_language(self):
        payload = query_payload()

        self.assertFalse(is_natural_language_payload(payload))

    def test_empty_query_is_not_natural_language(self):
        self.assertFalse(is_natural_language_payload({"query": "   "}))


class QueryBuilderTest(unittest.TestCase):
    def test_cpu_query_uses_regex_for_multiple_instances(self):
        query = build_cpu_usage_query({"instance": ["server-a:9100", "server-b:9100"]})

        self.assertIn('instance=~"server-a:9100|server-b:9100"', query)

    def test_memory_query_uses_filters(self):
        query = build_memory_usage_query({"job": "node"})

        self.assertIn('node_memory_MemAvailable_bytes{job="node"}', query)
        self.assertIn('node_memory_MemTotal_bytes{job="node"}', query)

    def test_disk_query_defaults_to_root_mountpoint_and_excludes_virtual_filesystems(self):
        query = build_disk_usage_query({"job": "node"})

        self.assertIn('mountpoint="/"', query)
        self.assertIn('fstype!~"tmpfs|overlay|squashfs|aufs"', query)


class FakePrometheusClient:
    prometheus_url = "http://prometheus:9090"

    def __init__(self):
        self.queries = []

    def query_range(self, query, query_range):
        self.queries.append((query, query_range))
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [],
            },
        }


class MetricQueryServiceTest(unittest.TestCase):
    def test_query_list_returns_result_per_target(self):
        client = FakePrometheusClient()
        service = MetricQueryService(client)
        payload = {
            "queries": [
                {
                    "server": "server-a:9100",
                    "targets": ["cpu"],
                    "range": VALID_RANGE,
                },
                {
                    "server": "server-b:9100",
                    "targets": ["cpu", "memory"],
                    "range": VALID_RANGE,
                },
            ]
        }

        response = service.handle_query(payload)

        self.assertEqual(len(response["results"]), 3)
        self.assertEqual(response["results"][0]["request"]["server"], "server-a:9100")
        self.assertEqual(response["results"][1]["request"]["target"], "cpu")
        self.assertEqual(response["results"][2]["request"]["target"], "memory")
        self.assertEqual(len(client.queries), 3)


if __name__ == "__main__":
    unittest.main()
