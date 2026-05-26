import re


LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def escape_label_value(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def escape_regex_value(value):
    return escape_label_value(re.sub(r"([.^$*+?{}\\[\]|()])", r"\\\1", str(value)))


def label_value_matcher(label, value):
    if isinstance(value, (list, tuple)):
        alternatives = "|".join(escape_regex_value(item) for item in value)
        return f'{label}=~"{alternatives}"'
    return f'{label}="{escape_label_value(value)}"'


def label_matchers(filters, defaults=None, negative_matchers=None):
    matchers = []
    merged = dict(defaults or {})
    merged.update(filters)

    for label, value in sorted(merged.items()):
        if not LABEL_RE.match(label):
            raise ValueError(f"invalid label name: {label}")
        matchers.append(label_value_matcher(label, value))

    for label, regex in sorted((negative_matchers or {}).items()):
        if label not in merged:
            matchers.append(f'{label}!~"{escape_label_value(regex)}"')

    return "{" + ",".join(matchers) + "}" if matchers else ""


def build_cpu_usage_query(filters):
    idle_matchers = label_matchers({"mode": "idle", **filters})
    return f"100 - (avg by (instance) (rate(node_cpu_seconds_total{idle_matchers}[5m])) * 100)"


def build_memory_usage_query(filters):
    matchers = label_matchers(filters)
    return f"(1 - (node_memory_MemAvailable_bytes{matchers} / node_memory_MemTotal_bytes{matchers})) * 100"


def build_disk_usage_query(filters):
    defaults = {"mountpoint": "/"}
    negative = {"fstype": "tmpfs|overlay|squashfs|aufs"}
    matchers = label_matchers(filters, defaults=defaults, negative_matchers=negative)
    return f"100 - ((node_filesystem_avail_bytes{matchers} / node_filesystem_size_bytes{matchers}) * 100)"


QUERY_BUILDERS = {
    ("cpu", "usage"): build_cpu_usage_query,
    ("memory", "usage"): build_memory_usage_query,
    ("disk", "usage"): build_disk_usage_query,
}


def build_query(target, operation, filters):
    return QUERY_BUILDERS[(target, operation)](filters)
