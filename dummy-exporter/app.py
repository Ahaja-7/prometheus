import os
import time

from prometheus_client import Counter, Gauge, start_http_server


PORT = int(os.getenv("DUMMY_EXPORTER_PORT", "9100"))

cpu_seconds_total = Counter(
    "node_cpu_seconds_total",
    "Dummy cumulative CPU seconds in node_exporter format.",
    ["cpu", "mode"],
)
memory_available_bytes = Gauge(
    "node_memory_MemAvailable_bytes",
    "Dummy available memory bytes in node_exporter format.",
)
memory_total_bytes = Gauge(
    "node_memory_MemTotal_bytes",
    "Dummy total memory bytes in node_exporter format.",
)
filesystem_avail_bytes = Gauge(
    "node_filesystem_avail_bytes",
    "Dummy available filesystem bytes in node_exporter format.",
    ["device", "fstype", "mountpoint"],
)
filesystem_size_bytes = Gauge(
    "node_filesystem_size_bytes",
    "Dummy filesystem size bytes in node_exporter format.",
    ["device", "fstype", "mountpoint"],
)


def set_dummy_metrics(tick):
    idle_increment = 11.25
    busy_increment = 3.75
    cpu_seconds_total.labels(cpu="0", mode="idle").inc(idle_increment)
    cpu_seconds_total.labels(cpu="0", mode="user").inc(busy_increment)

    total_memory = 16 * 1024 * 1024 * 1024
    used_ratio = 0.48 + ((tick % 12) * 0.01)
    memory_total_bytes.set(total_memory)
    memory_available_bytes.set(total_memory * (1 - used_ratio))

    total_disk = 200 * 1024 * 1024 * 1024
    used_disk_ratio = 0.62
    filesystem_size_bytes.labels(device="/dev/dummy0", fstype="ext4", mountpoint="/").set(total_disk)
    filesystem_avail_bytes.labels(device="/dev/dummy0", fstype="ext4", mountpoint="/").set(
        total_disk * (1 - used_disk_ratio)
    )


def main():
    start_http_server(PORT)
    tick = 0
    while True:
        set_dummy_metrics(tick)
        tick += 1
        time.sleep(15)


if __name__ == "__main__":
    main()
