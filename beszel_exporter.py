"""Prometheus/OpenMetrics-style exporter for Beszel Hub metrics.

The exporter reads Beszel's PocketBase API and exposes current values under
/metrics so Prometheus-compatible scrapers such as VictoriaMetrics vmagent can
forward them to a remote_write backend (for example OpenObserve).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import defaultdict
from typing import Any

import requests
from flask import Flask, Response

BESZEL_URL = os.getenv("BESZEL_URL", "http://beszel:8090").rstrip("/")
BESZEL_USER = os.getenv("BESZEL_USER", "")
BESZEL_PASSWORD = os.getenv("BESZEL_PASSWORD", "")
BESZEL_TOKEN = os.getenv("BESZEL_TOKEN", "")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
CACHE_TTL = float(os.getenv("CACHE_TTL", "15"))
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "9105"))

app = Flask(__name__)


def numeric(value: Any) -> float | int | None:
    """Return a finite numeric value or None."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def decoded(value: Any, default: Any) -> Any:
    """Decode PocketBase JSON fields that may arrive as strings."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def escape_label(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def gib_to_bytes(value: Any) -> float | None:
    value = numeric(value)
    return None if value is None else float(value) * 1024**3


def mib_to_bytes(value: Any) -> float | None:
    value = numeric(value)
    return None if value is None else float(value) * 1024**2


def microseconds_to_seconds(value: Any) -> float | None:
    value = numeric(value)
    return None if value is None else float(value) / 1_000_000


def milliseconds_to_seconds(value: Any) -> float | None:
    value = numeric(value)
    return None if value is None else float(value) / 1000


class PrometheusText:
    """Minimal Prometheus text exposition builder."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.declared: set[str] = set()

    def declare(self, name: str, metric_type: str = "gauge") -> None:
        if name not in self.declared:
            self.lines.append(f"# TYPE {name} {metric_type}")
            self.declared.add(name)

    def add(
        self,
        name: str,
        value: Any,
        labels: dict[str, Any] | None = None,
        metric_type: str = "gauge",
    ) -> None:
        value = numeric(value)
        if value is None:
            return

        self.declare(name, metric_type)

        if labels:
            encoded = ",".join(
                f'{key}="{escape_label(label_value)}"'
                for key, label_value in sorted(labels.items())
                if label_value is not None
            )
            self.lines.append(f"{name}{{{encoded}}} {value}")
        else:
            self.lines.append(f"{name} {value}")

    def info(self, name: str, labels: dict[str, Any]) -> None:
        self.add(name, 1, labels)

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


class BeszelAPI:
    """Small PocketBase client with automatic password re-authentication."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.token = BESZEL_TOKEN
        if self.token:
            self.session.headers["Authorization"] = self.token

    def authenticate(self) -> None:
        if self.token:
            return
        if not BESZEL_USER or not BESZEL_PASSWORD:
            raise RuntimeError(
                "Configure BESZEL_TOKEN or BESZEL_USER and BESZEL_PASSWORD"
            )

        response = self.session.post(
            f"{BESZEL_URL}/api/collections/users/auth-with-password",
            json={"identity": BESZEL_USER, "password": BESZEL_PASSWORD},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        self.token = response.json()["token"]
        self.session.headers["Authorization"] = self.token

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.authenticate()
        response = self.session.get(
            f"{BESZEL_URL}{path}",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 401 and not BESZEL_TOKEN:
            self.token = ""
            self.session.headers.pop("Authorization", None)
            self.authenticate()
            response = self.session.get(
                f"{BESZEL_URL}{path}",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

        response.raise_for_status()
        return response.json()

    def records(
        self,
        collection: str,
        *,
        fields: str | None = None,
        filter_expr: str | None = None,
        sort: str | None = None,
        per_page: int = 500,
    ) -> list[dict[str, Any]]:
        page = 1
        result: list[dict[str, Any]] = []

        while True:
            params: dict[str, Any] = {"page": page, "perPage": per_page}
            if fields:
                params["fields"] = fields
            if filter_expr:
                params["filter"] = filter_expr
            if sort:
                params["sort"] = sort

            payload = self.get(
                f"/api/collections/{collection}/records",
                params=params,
            )
            result.extend(payload.get("items", []))

            if page >= int(payload.get("totalPages", 1)):
                break
            page += 1

        return result

    def latest(
        self,
        collection: str,
        filter_expr: str,
        fields: str | None = None,
    ) -> dict[str, Any] | None:
        rows = self.records(
            collection,
            fields=fields,
            filter_expr=filter_expr,
            sort="-created",
            per_page=1,
        )
        return rows[0] if rows else None


class BeszelCollector:
    def __init__(self) -> None:
        self.api = BeszelAPI()
        self.lock = threading.Lock()
        self.cache = ""
        self.cache_time = 0.0
        self.last_success = 0.0
        self.collection_errors: defaultdict[str, int] = defaultdict(int)

    def optional_records(
        self,
        collection: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        try:
            return self.api.records(collection, **kwargs)
        except Exception:
            self.collection_errors[collection] += 1
            return []

    def optional_latest(
        self,
        collection: str,
        filter_expr: str,
        fields: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            return self.api.latest(collection, filter_expr, fields)
        except Exception:
            self.collection_errors[collection] += 1
            return None

    def collect(self) -> str:
        with self.lock:
            now = time.time()
            if self.cache and now - self.cache_time < CACHE_TTL:
                return self.cache

            metrics = PrometheusText()
            started = time.time()

            try:
                systems = self.api.records("systems")
                details = {
                    row.get("system", row.get("id")): row
                    for row in self.optional_records("system_details")
                }

                related = {
                    "containers": self.optional_records("containers"),
                    "smart_devices": self.optional_records("smart_devices"),
                    "systemd_services": self.optional_records("systemd_services"),
                    "network_monitors": self.optional_records("network_monitors"),
                    "zfs_pools": self.optional_records("zfs_pools"),
                }

                grouped: dict[str, defaultdict[str, list[dict[str, Any]]]] = {
                    name: defaultdict(list) for name in related
                }
                for name, rows in related.items():
                    for row in rows:
                        grouped[name][row.get("system", "")].append(row)

                for system in systems:
                    system_id = system.get("id")
                    if not system_id:
                        continue

                    labels = {
                        "system": system.get("name", system_id),
                        "system_id": system_id,
                    }

                    self.emit_system_record(
                        metrics,
                        labels,
                        system,
                        details.get(system_id),
                    )

                    system_stats = self.optional_latest(
                        "system_stats",
                        f'system="{system_id}" && type="1m"',
                        "stats,created,type",
                    )
                    if system_stats:
                        self.emit_system_stats(
                            metrics,
                            labels,
                            decoded(system_stats.get("stats"), {}),
                        )

                    container_stats = self.optional_latest(
                        "container_stats",
                        f'system="{system_id}" && type="1m"',
                        "stats,created,type",
                    )
                    history = (
                        decoded(container_stats.get("stats"), [])
                        if container_stats
                        else []
                    )

                    self.emit_containers(
                        metrics,
                        labels,
                        grouped["containers"][system_id],
                        history,
                    )
                    self.emit_smart(
                        metrics,
                        labels,
                        grouped["smart_devices"][system_id],
                    )
                    self.emit_systemd(
                        metrics,
                        labels,
                        grouped["systemd_services"][system_id],
                    )
                    self.emit_storage_pools(
                        metrics,
                        labels,
                        grouped["zfs_pools"][system_id],
                    )
                    self.emit_network_monitors(
                        metrics,
                        labels,
                        grouped["network_monitors"][system_id],
                    )

                self.last_success = time.time()
                metrics.add("beszel_exporter_up", 1)

            except Exception:
                metrics.add("beszel_exporter_up", 0)
                raise

            finally:
                metrics.add(
                    "beszel_exporter_scrape_duration_seconds",
                    time.time() - started,
                )
                if self.last_success:
                    metrics.add(
                        "beszel_exporter_last_success_timestamp_seconds",
                        self.last_success,
                    )
                for collection, count in self.collection_errors.items():
                    metrics.add(
                        "beszel_exporter_collection_errors_total",
                        count,
                        {"collection": collection},
                        metric_type="counter",
                    )

            self.cache = metrics.render()
            self.cache_time = time.time()
            return self.cache

    @staticmethod
    def emit_system_record(
        metrics: PrometheusText,
        labels: dict[str, Any],
        system: dict[str, Any],
        details: dict[str, Any] | None,
    ) -> None:
        info = decoded(system.get("info"), {})
        details = details or {}

        metrics.info(
            "beszel_system_info",
            {
                **labels,
                "status": system.get("status", ""),
                "agent_version": info.get("v", system.get("v", "")),
                "hostname": details.get("hostname", info.get("h", "")),
                "kernel": details.get("kernel", info.get("k", "")),
                "os_name": details.get("os_name", ""),
                "cpu_model": details.get("cpu", info.get("m", "")),
                "root_disk_name": info.get("rdn", ""),
            },
        )
        metrics.add(
            "beszel_system_up",
            1 if system.get("status") == "up" else 0,
            labels,
        )
        metrics.add("beszel_system_uptime_seconds", info.get("u"), labels)
        metrics.add(
            "beszel_system_dashboard_temperature_celsius",
            info.get("dt"),
            labels,
        )
        metrics.add("beszel_system_connection_type", info.get("ct"), labels)
        metrics.add("beszel_system_journal_available", info.get("jl"), labels)
        metrics.add("beszel_system_cpu_cores", details.get("cores"), labels)
        metrics.add("beszel_system_cpu_threads", details.get("threads"), labels)
        metrics.add("beszel_system_podman", details.get("podman"), labels)
        metrics.add(
            "beszel_system_detected_memory_bytes",
            details.get("memory"),
            labels,
        )

        services = info.get("sv")
        if isinstance(services, list):
            if len(services) >= 1:
                metrics.add("beszel_systemd_services_total", services[0], labels)
            if len(services) >= 2:
                metrics.add("beszel_systemd_services_failed", services[1], labels)

        updates = info.get("pu")
        if isinstance(updates, list):
            if len(updates) >= 1:
                metrics.add(
                    "beszel_package_updates_pending",
                    updates[0],
                    {**labels, "type": "all"},
                )
            if len(updates) >= 2:
                metrics.add(
                    "beszel_package_updates_pending",
                    updates[1],
                    {**labels, "type": "security"},
                )

        extra_fs = decoded(info.get("efs"), {})
        for filesystem, percent in extra_fs.items():
            metrics.add(
                "beszel_filesystem_usage_percent",
                percent,
                {**labels, "filesystem": filesystem},
            )

        wifi = decoded(info.get("wf"), {})
        for interface, station in wifi.items():
            if not isinstance(station, dict):
                continue
            station_labels = {
                **labels,
                "interface": interface,
                "ssid": station.get("s", ""),
            }
            metrics.info("beszel_system_wifi_info", station_labels)
            metrics.add(
                "beszel_system_wifi_rssi_dbm",
                station.get("r"),
                station_labels,
            )

    @staticmethod
    def emit_system_stats(
        metrics: PrometheusText,
        labels: dict[str, Any],
        stats: dict[str, Any],
    ) -> None:
        gauges: dict[str, tuple[str, float]] = {
            "beszel_system_cpu_usage_percent": ("cpu", 1),
            "beszel_system_cpu_usage_peak_percent": ("cpum", 1),
            "beszel_system_memory_total_bytes": ("m", 1024**3),
            "beszel_system_memory_used_bytes": ("mu", 1024**3),
            "beszel_system_memory_usage_percent": ("mp", 1),
            "beszel_system_memory_used_peak_bytes": ("mm", 1024**3),
            "beszel_system_memory_buffer_cache_bytes": ("mb", 1024**3),
            "beszel_system_zfs_arc_bytes": ("mz", 1024**3),
            "beszel_system_swap_total_bytes": ("s", 1024**3),
            "beszel_system_swap_used_bytes": ("su", 1024**3),
            "beszel_system_disk_total_bytes": ("d", 1024**3),
            "beszel_system_disk_used_bytes": ("du", 1024**3),
            "beszel_system_disk_usage_percent": ("dp", 1),
            "beszel_system_disk_read_mib_per_second": ("dr", 1),
            "beszel_system_disk_write_mib_per_second": ("dw", 1),
            "beszel_system_disk_read_peak_mib_per_second": ("drm", 1),
            "beszel_system_disk_write_peak_mib_per_second": ("dwm", 1),
            "beszel_system_network_sent_mib_per_second": ("ns", 1),
            "beszel_system_network_received_mib_per_second": ("nr", 1),
            "beszel_system_network_sent_peak_mib_per_second": ("nsm", 1),
            "beszel_system_network_received_peak_mib_per_second": ("nrm", 1),
        }
        for name, (key, factor) in gauges.items():
            value = numeric(stats.get(key))
            if value is not None:
                metrics.add(name, float(value) * factor, labels)

        load_average = stats.get("la")
        if isinstance(load_average, list):
            for index, window in enumerate(("1m", "5m", "15m")):
                if index < len(load_average):
                    metrics.add(
                        "beszel_system_load_average",
                        load_average[index],
                        {**labels, "window": window},
                    )

        bandwidth = stats.get("b")
        if isinstance(bandwidth, list):
            for index, direction in enumerate(("transmit", "receive")):
                if index < len(bandwidth):
                    metrics.add(
                        "beszel_system_network_bytes_per_second",
                        bandwidth[index],
                        {**labels, "direction": direction},
                    )

        max_bandwidth = stats.get("bm")
        if isinstance(max_bandwidth, list):
            for index, direction in enumerate(("transmit", "receive")):
                if index < len(max_bandwidth):
                    metrics.add(
                        "beszel_system_network_peak_bytes_per_second",
                        max_bandwidth[index],
                        {**labels, "direction": direction},
                    )

        cpu_breakdown = stats.get("cpub")
        if isinstance(cpu_breakdown, list):
            for index, mode in enumerate(
                ("user", "system", "iowait", "steal", "idle")
            ):
                if index < len(cpu_breakdown):
                    metrics.add(
                        "beszel_system_cpu_time_percent",
                        cpu_breakdown[index],
                        {**labels, "mode": mode},
                    )

        cpu_cores = stats.get("cpus")
        if isinstance(cpu_cores, list):
            for index, value in enumerate(cpu_cores):
                metrics.add(
                    "beszel_system_cpu_core_usage_percent",
                    value,
                    {**labels, "core": str(index)},
                )

        for sensor, value in decoded(stats.get("t"), {}).items():
            metrics.add(
                "beszel_system_temperature_celsius",
                value,
                {**labels, "sensor": sensor},
            )

        for fan, value in decoded(stats.get("f"), {}).items():
            metrics.add(
                "beszel_system_fan_rpm",
                value,
                {**labels, "fan": fan},
            )

        battery = stats.get("bat")
        if isinstance(battery, list):
            if len(battery) >= 1:
                metrics.add(
                    "beszel_system_battery_percent",
                    battery[0],
                    {**labels, "battery": "primary"},
                )
            if len(battery) >= 2:
                metrics.add(
                    "beszel_system_battery_state",
                    battery[1],
                    {**labels, "battery": "primary"},
                )

        for battery_name, value in decoded(stats.get("bats"), {}).items():
            metrics.add(
                "beszel_system_battery_percent",
                value,
                {**labels, "battery": battery_name},
            )

        disk_io = stats.get("dio")
        if isinstance(disk_io, list):
            for index, direction in enumerate(("read", "write")):
                if index < len(disk_io):
                    metrics.add(
                        "beszel_system_disk_io_bytes_per_second",
                        disk_io[index],
                        {**labels, "direction": direction},
                    )

        disk_io_peak = stats.get("diom")
        if isinstance(disk_io_peak, list):
            for index, direction in enumerate(("read", "write")):
                if index < len(disk_io_peak):
                    metrics.add(
                        "beszel_system_disk_io_peak_bytes_per_second",
                        disk_io_peak[index],
                        {**labels, "direction": direction},
                    )

        disk_io_total = stats.get("diot")
        if isinstance(disk_io_total, list):
            for index, direction in enumerate(("read", "write")):
                if index < len(disk_io_total):
                    metrics.add(
                        "beszel_system_disk_io_bytes_total",
                        disk_io_total[index],
                        {**labels, "direction": direction},
                        metric_type="counter",
                    )

        BeszelCollector.emit_disk_io_stats(
            metrics,
            labels,
            stats.get("dios"),
            prefix="beszel_system_disk",
        )
        BeszelCollector.emit_disk_io_stats(
            metrics,
            labels,
            stats.get("diosm"),
            prefix="beszel_system_disk_peak",
        )

        for interface, values in decoded(stats.get("ni"), {}).items():
            if not isinstance(values, list):
                continue
            nic_labels = {**labels, "interface": interface}
            for index, direction in enumerate(("transmit", "receive")):
                if index < len(values):
                    metrics.add(
                        "beszel_system_network_interface_bytes_per_second",
                        values[index],
                        {**nic_labels, "direction": direction},
                    )
                if index + 2 < len(values):
                    metrics.add(
                        "beszel_system_network_interface_bytes_total",
                        values[index + 2],
                        {**nic_labels, "direction": direction},
                        metric_type="counter",
                    )

        for interface, rssi in decoded(stats.get("wf"), {}).items():
            metrics.add(
                "beszel_system_wifi_rssi_dbm",
                rssi,
                {**labels, "interface": interface},
            )

        for filesystem, fs in decoded(stats.get("efs"), {}).items():
            if not isinstance(fs, dict):
                continue
            fs_labels = {**labels, "filesystem": filesystem}

            total = numeric(fs.get("d"))
            used = numeric(fs.get("du"))
            metrics.add(
                "beszel_filesystem_total_bytes",
                gib_to_bytes(total),
                fs_labels,
            )
            metrics.add(
                "beszel_filesystem_used_bytes",
                gib_to_bytes(used),
                fs_labels,
            )
            if total and used is not None:
                metrics.add(
                    "beszel_filesystem_usage_percent",
                    float(used) / float(total) * 100,
                    fs_labels,
                )

            legacy_fs = {
                "beszel_filesystem_read_mib_per_second": "r",
                "beszel_filesystem_write_mib_per_second": "w",
                "beszel_filesystem_read_peak_mib_per_second": "rm",
                "beszel_filesystem_write_peak_mib_per_second": "wm",
            }
            for metric_name, key in legacy_fs.items():
                metrics.add(metric_name, fs.get(key), fs_labels)

            metrics.add(
                "beszel_filesystem_read_bytes_per_second",
                fs.get("rb"),
                fs_labels,
            )
            metrics.add(
                "beszel_filesystem_write_bytes_per_second",
                fs.get("wb"),
                fs_labels,
            )
            metrics.add(
                "beszel_filesystem_read_peak_bytes_per_second",
                fs.get("rbm"),
                fs_labels,
            )
            metrics.add(
                "beszel_filesystem_write_peak_bytes_per_second",
                fs.get("wbm"),
                fs_labels,
            )
            metrics.add(
                "beszel_filesystem_read_bytes_total",
                fs.get("tr"),
                fs_labels,
                metric_type="counter",
            )
            metrics.add(
                "beszel_filesystem_write_bytes_total",
                fs.get("tw"),
                fs_labels,
                metric_type="counter",
            )
            BeszelCollector.emit_disk_io_stats(
                metrics,
                fs_labels,
                fs.get("dios"),
                prefix="beszel_filesystem",
            )
            BeszelCollector.emit_disk_io_stats(
                metrics,
                fs_labels,
                fs.get("diosm"),
                prefix="beszel_filesystem_peak",
            )

        for gpu_id, gpu in decoded(stats.get("g"), {}).items():
            if not isinstance(gpu, dict):
                continue
            gpu_labels = {
                **labels,
                "gpu": gpu_id,
                "name": gpu.get("n", ""),
            }
            metrics.add("beszel_gpu_usage_percent", gpu.get("u"), gpu_labels)
            metrics.add(
                "beszel_gpu_memory_used_bytes",
                mib_to_bytes(gpu.get("mu")),
                gpu_labels,
            )
            metrics.add(
                "beszel_gpu_memory_total_bytes",
                mib_to_bytes(gpu.get("mt")),
                gpu_labels,
            )
            metrics.add("beszel_gpu_power_watts", gpu.get("p"), gpu_labels)
            metrics.add(
                "beszel_gpu_package_power_watts",
                gpu.get("pp"),
                gpu_labels,
            )
            for engine, value in decoded(gpu.get("e"), {}).items():
                metrics.add(
                    "beszel_gpu_engine_usage_percent",
                    value,
                    {**gpu_labels, "engine": engine},
                )

        for pool_name, pool in decoded(stats.get("z"), {}).items():
            if not isinstance(pool, dict):
                continue
            pool_labels = {
                **labels,
                "pool": pool_name,
                "display_name": pool.get("n", pool_name),
                "raw": str(bool(pool.get("raw", False))).lower(),
            }
            metrics.add(
                "beszel_storage_pool_total_bytes",
                gib_to_bytes(pool.get("d")),
                pool_labels,
            )
            metrics.add(
                "beszel_storage_pool_used_bytes",
                gib_to_bytes(pool.get("du")),
                pool_labels,
            )
            metrics.add(
                "beszel_storage_pool_read_bytes_per_second",
                pool.get("rb"),
                pool_labels,
            )
            metrics.add(
                "beszel_storage_pool_write_bytes_per_second",
                pool.get("wb"),
                pool_labels,
            )
            if pool.get("h"):
                metrics.info(
                    "beszel_storage_pool_health_info",
                    {**pool_labels, "health": pool.get("h", "")},
                )

    @staticmethod
    def emit_disk_io_stats(
        metrics: PrometheusText,
        labels: dict[str, Any],
        values: Any,
        *,
        prefix: str,
    ) -> None:
        if not isinstance(values, list):
            return
        names = (
            "read_time_percent",
            "write_time_percent",
            "utilization_percent",
            "read_await_milliseconds",
            "write_await_milliseconds",
            "weighted_io_percent",
        )
        for index, suffix in enumerate(names):
            if index < len(values):
                metrics.add(f"{prefix}_{suffix}", values[index], labels)

    @staticmethod
    def emit_containers(
        metrics: PrometheusText,
        labels: dict[str, Any],
        containers: list[dict[str, Any]],
        historical_stats: list[dict[str, Any]],
    ) -> None:
        history_by_name = {
            row.get("n"): row
            for row in historical_stats
            if isinstance(row, dict) and row.get("n")
        }

        for container in containers:
            name = container.get("name", "")
            container_labels = {**labels, "container": name}

            metrics.info(
                "beszel_container_info",
                {
                    **container_labels,
                    "container_id": container.get("id", ""),
                    "image": container.get("image", ""),
                    "status": container.get("status", ""),
                    "ports": container.get("ports", ""),
                },
            )
            metrics.add(
                "beszel_container_cpu_usage_percent",
                container.get("cpu"),
                container_labels,
            )
            metrics.add(
                "beszel_container_memory_used_bytes",
                mib_to_bytes(container.get("memory")),
                container_labels,
            )
            metrics.add(
                "beszel_container_network_bytes_per_second",
                container.get("net"),
                {**container_labels, "direction": "total"},
            )
            metrics.add(
                "beszel_container_health_state",
                container.get("health"),
                container_labels,
            )
            metrics.add(
                "beszel_container_update_available",
                container.get("updatable"),
                container_labels,
            )

            history = history_by_name.get(name, {})
            bandwidth = history.get("b")
            if isinstance(bandwidth, list):
                for index, direction in enumerate(("transmit", "receive")):
                    if index < len(bandwidth):
                        metrics.add(
                            "beszel_container_network_bytes_per_second",
                            bandwidth[index],
                            {**container_labels, "direction": direction},
                        )

    @staticmethod
    def emit_smart(
        metrics: PrometheusText,
        labels: dict[str, Any],
        devices: list[dict[str, Any]],
    ) -> None:
        for device in devices:
            device_labels = {
                **labels,
                "device": device.get("name", ""),
                "model": device.get("model", ""),
                "serial": device.get("serial", ""),
                "firmware": device.get("firmware", ""),
                "device_type": device.get("type", ""),
            }

            metrics.info(
                "beszel_smart_device_info",
                {
                    **device_labels,
                    "state": device.get("state", ""),
                },
            )
            metrics.add(
                "beszel_smart_capacity_bytes",
                device.get("capacity"),
                device_labels,
            )
            metrics.add(
                "beszel_smart_temperature_celsius",
                device.get("temp"),
                device_labels,
            )
            metrics.add(
                "beszel_smart_power_on_hours_total",
                device.get("hours"),
                device_labels,
                metric_type="counter",
            )
            metrics.add(
                "beszel_smart_power_cycles_total",
                device.get("cycles"),
                device_labels,
                metric_type="counter",
            )

            state = str(device.get("state", "")).upper()
            if state:
                metrics.add(
                    "beszel_smart_passed",
                    1 if state == "PASSED" else 0,
                    device_labels,
                )

            for attribute in decoded(device.get("attributes"), []):
                if not isinstance(attribute, dict):
                    continue
                attribute_labels = {
                    **device_labels,
                    "attribute": attribute.get("n", ""),
                    "attribute_id": str(attribute.get("id", "")),
                }
                metrics.add(
                    "beszel_smart_attribute_value",
                    attribute.get("v"),
                    attribute_labels,
                )
                metrics.add(
                    "beszel_smart_attribute_worst",
                    attribute.get("w"),
                    attribute_labels,
                )
                metrics.add(
                    "beszel_smart_attribute_threshold",
                    attribute.get("t"),
                    attribute_labels,
                )
                metrics.add(
                    "beszel_smart_attribute_raw",
                    attribute.get("rv"),
                    attribute_labels,
                )

    @staticmethod
    def emit_systemd(
        metrics: PrometheusText,
        labels: dict[str, Any],
        services: list[dict[str, Any]],
    ) -> None:
        states = (
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "reloading",
        )
        substates = ("dead", "running", "exited", "failed", "unknown")

        for service in services:
            service_labels = {
                **labels,
                "service": service.get("name", ""),
            }
            state_value = numeric(service.get("state"))
            substate_value = numeric(service.get("sub"))
            state = int(state_value) if state_value is not None else 1
            substate = int(substate_value) if substate_value is not None else 4
            state_text = states[state] if 0 <= state < len(states) else str(state)
            substate_text = (
                substates[substate]
                if 0 <= substate < len(substates)
                else str(substate)
            )

            metrics.info(
                "beszel_systemd_service_info",
                {
                    **service_labels,
                    "state": state_text,
                    "substate": substate_text,
                },
            )
            metrics.add(
                "beszel_systemd_service_active",
                1 if state == 0 else 0,
                service_labels,
            )
            metrics.add(
                "beszel_systemd_service_failed",
                1 if state == 2 else 0,
                service_labels,
            )
            metrics.add(
                "beszel_systemd_service_cpu_usage_percent",
                service.get("cpu"),
                service_labels,
            )
            metrics.add(
                "beszel_systemd_service_cpu_peak_percent",
                service.get("cpuPeak"),
                service_labels,
            )
            metrics.add(
                "beszel_systemd_service_memory_bytes",
                service.get("memory"),
                service_labels,
            )
            metrics.add(
                "beszel_systemd_service_memory_peak_bytes",
                service.get("memPeak"),
                service_labels,
            )

    @staticmethod
    def emit_storage_pools(
        metrics: PrometheusText,
        labels: dict[str, Any],
        pools: list[dict[str, Any]],
    ) -> None:
        for pool in pools:
            raw_name = pool.get("name", "")
            pool_labels = {
                **labels,
                "pool": raw_name,
                "display_name": pool.get("display_name", raw_name),
                "pool_type": "btrfs" if str(raw_name).startswith("b:") else "zfs",
            }

            metrics.info(
                "beszel_storage_pool_info",
                {**pool_labels, "health": pool.get("health", "")},
            )
            metrics.add(
                "beszel_storage_pool_size_bytes",
                pool.get("size"),
                pool_labels,
            )
            metrics.add(
                "beszel_storage_pool_allocated_bytes",
                pool.get("alloc"),
                pool_labels,
            )
            metrics.add(
                "beszel_storage_pool_free_bytes",
                pool.get("free"),
                pool_labels,
            )

            scrub = decoded(pool.get("scrub"), {})
            if scrub:
                metrics.info(
                    "beszel_storage_pool_scrub_info",
                    {**pool_labels, "state": scrub.get("state", "")},
                )
                progress = str(scrub.get("progress", "")).strip().rstrip("%")
                metrics.add(
                    "beszel_storage_pool_scrub_progress_percent",
                    progress,
                    pool_labels,
                )
                metrics.add(
                    "beszel_storage_pool_scrub_errors",
                    scrub.get("errors"),
                    pool_labels,
                )

            for vdev in decoded(pool.get("vdevs"), []):
                if not isinstance(vdev, dict):
                    continue
                vdev_labels = {
                    **pool_labels,
                    "vdev": vdev.get("name", ""),
                    "state": vdev.get("state", ""),
                }
                metrics.info("beszel_storage_pool_vdev_info", vdev_labels)
                metrics.add(
                    "beszel_storage_pool_vdev_read_errors",
                    vdev.get("readErrs"),
                    vdev_labels,
                )
                metrics.add(
                    "beszel_storage_pool_vdev_write_errors",
                    vdev.get("writeErrs"),
                    vdev_labels,
                )
                metrics.add(
                    "beszel_storage_pool_vdev_checksum_errors",
                    vdev.get("checksumErrs"),
                    vdev_labels,
                )

            for dataset in decoded(pool.get("datasets"), []):
                if not isinstance(dataset, dict):
                    continue
                dataset_labels = {
                    **pool_labels,
                    "dataset": dataset.get("name", ""),
                    "mount": dataset.get("mount", ""),
                }
                metrics.add(
                    "beszel_storage_pool_dataset_used_bytes",
                    dataset.get("used"),
                    dataset_labels,
                )
                metrics.add(
                    "beszel_storage_pool_dataset_available_bytes",
                    dataset.get("avail"),
                    dataset_labels,
                )

    def emit_network_monitors(
        self,
        metrics: PrometheusText,
        labels: dict[str, Any],
        monitors: list[dict[str, Any]],
    ) -> None:
        for monitor in monitors:
            monitor_labels = {
                **labels,
                "monitor_id": monitor.get("id", ""),
                "target": monitor.get("target", ""),
                "protocol": monitor.get("protocol", ""),
                "port": str(monitor.get("port", "")),
                "server": monitor.get("server", ""),
            }

            metrics.add(
                "beszel_network_monitor_enabled",
                monitor.get("enabled"),
                monitor_labels,
            )
            metrics.add(
                "beszel_network_monitor_interval_seconds",
                monitor.get("interval"),
                monitor_labels,
            )

            response_values = {
                "current": monitor.get("res"),
                "1h_avg": monitor.get("resAvg1h"),
                "1h_min": monitor.get("resMin1h"),
                "1h_max": monitor.get("resMax1h"),
            }
            for window, value in response_values.items():
                metrics.add(
                    "beszel_network_monitor_response_seconds",
                    microseconds_to_seconds(value),
                    {**monitor_labels, "window": window},
                )

            metrics.add(
                "beszel_network_monitor_packet_loss_percent",
                monitor.get("loss"),
                {**monitor_labels, "window": "current"},
            )
            metrics.add(
                "beszel_network_monitor_packet_loss_percent",
                monitor.get("loss1h"),
                {**monitor_labels, "window": "1h"},
            )

            cert = decoded(monitor.get("certInfo"), {})
            if cert:
                expiry = milliseconds_to_seconds(cert.get("expires"))
                metrics.add(
                    "beszel_network_monitor_tls_cert_expiry_timestamp_seconds",
                    expiry,
                    monitor_labels,
                )
                if expiry is not None:
                    metrics.add(
                        "beszel_network_monitor_tls_cert_days_remaining",
                        (expiry - time.time()) / 86400,
                        monitor_labels,
                    )
                if cert.get("issuer"):
                    metrics.info(
                        "beszel_network_monitor_tls_cert_info",
                        {**monitor_labels, "issuer": cert.get("issuer", "")},
                    )

            monitor_id = monitor.get("id")
            if not monitor_id:
                continue

            record = self.optional_latest(
                "network_monitor_stats",
                f'monitor="{monitor_id}" && type="1m"',
                (
                    "monitor,res_min,res_max,total_count,success_count,"
                    "res_sum,created,type"
                ),
            )
            if not record:
                continue

            total = numeric(record.get("total_count")) or 0
            success = numeric(record.get("success_count")) or 0
            response_sum = numeric(record.get("res_sum")) or 0

            metrics.add(
                "beszel_network_monitor_probe_count",
                total,
                {**monitor_labels, "result": "total"},
            )
            metrics.add(
                "beszel_network_monitor_probe_count",
                success,
                {**monitor_labels, "result": "success"},
            )
            metrics.add(
                "beszel_network_monitor_response_seconds",
                microseconds_to_seconds(record.get("res_min")),
                {**monitor_labels, "window": "1m_min"},
            )
            metrics.add(
                "beszel_network_monitor_response_seconds",
                microseconds_to_seconds(record.get("res_max")),
                {**monitor_labels, "window": "1m_max"},
            )
            if success:
                metrics.add(
                    "beszel_network_monitor_response_seconds",
                    float(response_sum) / float(success) / 1_000_000,
                    {**monitor_labels, "window": "1m_avg"},
                )
            if total:
                metrics.add(
                    "beszel_network_monitor_packet_loss_percent",
                    (float(total) - float(success)) * 100 / float(total),
                    {**monitor_labels, "window": "1m"},
                )


collector = BeszelCollector()


@app.get("/metrics")
def metrics_endpoint() -> Response:
    try:
        return Response(
            collector.collect(),
            mimetype="text/plain; version=0.0.4; charset=utf-8",
        )
    except Exception as exc:
        body = (
            "# TYPE beszel_exporter_up gauge\n"
            "beszel_exporter_up 0\n"
            f'# exporter_error "{escape_label(type(exc).__name__)}: '
            f'{escape_label(exc)}"\n'
        )
        return Response(
            body,
            status=500,
            mimetype="text/plain; version=0.0.4; charset=utf-8",
        )


@app.get("/healthz")
def health_endpoint() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
