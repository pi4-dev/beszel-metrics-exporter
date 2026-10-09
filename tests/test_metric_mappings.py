"""Contract tests for Beszel -> Prometheus metric mappings and unit conversions."""

from datetime import datetime, timezone

import pytest

import beszel_exporter as exporter


SYSTEM = {"system": "host-a", "system_id": "sys1"}


def samples(metrics, name):
    return metrics.families[name]["samples"]


def one(metrics, name):
    values = samples(metrics, name)
    assert len(values) == 1
    return values[0]


@pytest.mark.parametrize(
    ("state", "substate", "expected_state", "expected_substate", "expected_active", "expected_failed"),
    [
        (None, None, "unknown", "unknown", None, None),
        (123, 77, "unknown", "unknown", None, None),
        ("garbage", "broken", "unknown", "unknown", None, None),
        (0.5, 1.5, "unknown", "unknown", None, None),
        (0, 1, "active", "running", 1, 0),
        (1, 0, "inactive", "dead", 0, 0),
        (2, 3, "failed", "failed", 0, 1),
        (0, None, "active", "unknown", 1, 0),
    ],
)
def test_systemd_state_mapping_never_defaults_missing_to_inactive(
    state, substate, expected_state, expected_substate, expected_active, expected_failed
):
    record = {"name": "sshd.service", "memory": 256, "cpu": 4}
    if state is not None:
        record["state"] = state
    if substate is not None:
        record["sub"] = substate
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_systemd(metrics, SYSTEM, [record], emit_dynamic=True)

    info_value, info_labels = one(metrics, "beszel_systemd_service_info")
    assert info_value == 1
    assert info_labels["state"] == expected_state
    assert info_labels["substate"] == expected_substate
    assert info_labels["service"] == "sshd.service"
    assert one(metrics, "beszel_systemd_service_memory_bytes")[0] == 256
    if expected_active is None:
        assert "beszel_systemd_service_active" not in metrics.families
        assert "beszel_systemd_service_failed" not in metrics.families
    else:
        assert one(metrics, "beszel_systemd_service_active")[0] == expected_active
        assert one(metrics, "beszel_systemd_service_failed")[0] == expected_failed


def test_smart_power_on_time_and_attributes_use_base_units(monkeypatch):
    monkeypatch.setattr(exporter, "LEGACY_UNITS", False)
    device = {
        "name": "/dev/sda",
        "serial": "disk-serial",
        "model": "NAS disk",
        "firmware": "1.0",
        "state": "PASSED",
        "hours": 1.5,
        "cycles": 7,
        "capacity": 4_000_000_000,
        "temp": 32,
        "attributes": [
            {"id": 194, "n": "Temperature", "v": 98, "w": 95, "t": 30, "rv": 32}
        ],
    }
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_smart(metrics, SYSTEM, [device], emit_dynamic=True)
    value, labels = one(metrics, "beszel_smart_power_on_seconds_total")
    assert value == 5400
    assert labels["serial"] == "disk-serial"
    assert metrics.families["beszel_smart_power_on_seconds_total"]["type"] == "counter"
    assert "beszel_smart_power_on_hours_total" not in metrics.families
    assert one(metrics, "beszel_smart_power_cycles_total")[0] == 7
    assert one(metrics, "beszel_smart_capacity_bytes")[0] == 4_000_000_000
    assert one(metrics, "beszel_smart_temperature_celsius")[0] == 32
    assert one(metrics, "beszel_smart_passed")[0] == 1
    info = one(metrics, "beszel_smart_device_info")[1]
    assert info["model"] == "NAS disk" and info["firmware"] == "1.0"
    assert "model" not in labels and "firmware" not in labels
    attribute_value, attr_labels = one(metrics, "beszel_smart_attribute_raw")
    assert attribute_value == 32
    assert attr_labels["attribute_id"] == "194"
    assert attr_labels["attribute"] == "Temperature"
    assert one(metrics, "beszel_smart_attribute_value")[0] == 98
    assert one(metrics, "beszel_smart_attribute_worst")[0] == 95
    assert one(metrics, "beszel_smart_attribute_threshold")[0] == 30

    monkeypatch.setattr(exporter, "LEGACY_UNITS", True)
    legacy = exporter.PrometheusText()
    exporter.BeszelCollector.emit_smart(legacy, SYSTEM, [device], emit_dynamic=True)
    assert one(legacy, "beszel_smart_power_on_hours_total")[0] == 1.5
    assert one(legacy, "beszel_smart_power_on_seconds_total")[0] == 5400


def test_gpu_model_name_only_on_info_not_numeric_series():
    stats = {
        "g": {
            "0": {
                "n": "NVIDIA RTX 4000",
                "u": 73,
                "mu": 512,
                "mt": 1024,
                "p": 120,
                "pp": 135,
                "e": {"graphics": 60, "video": 25},
            }
        }
    }
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_system_stats(metrics, SYSTEM, stats)
    value, labels = one(metrics, "beszel_gpu_usage_percent")
    assert value == 73
    assert labels == {**SYSTEM, "gpu": "0"}
    assert one(metrics, "beszel_gpu_memory_used_bytes")[0] == 512 * 1024**2
    assert one(metrics, "beszel_gpu_memory_total_bytes")[0] == 1024 * 1024**2
    assert one(metrics, "beszel_gpu_power_watts")[0] == 120
    assert one(metrics, "beszel_gpu_package_power_watts")[0] == 135
    engines = samples(metrics, "beszel_gpu_engine_usage_percent")
    assert {labels["engine"]: value for value, labels in engines} == {
        "graphics": 60,
        "video": 25,
    }
    assert all("name" not in labels for _, labels in engines)
    info = one(metrics, "beszel_gpu_info")[1]
    assert info == {**SYSTEM, "gpu": "0", "name": "NVIDIA RTX 4000"}

    # A driver-reported model-string change must not change numeric identities.
    stats["g"]["0"]["n"] = "NVIDIA RTX 4000 - revised"
    later = exporter.PrometheusText()
    exporter.BeszelCollector.emit_system_stats(later, SYSTEM, stats)
    assert one(later, "beszel_gpu_usage_percent") == one(
        metrics, "beszel_gpu_usage_percent"
    )
    assert one(later, "beszel_gpu_info")[1]["name"] != info["name"]


def test_zfs_stats_and_collection_have_distinct_mappings_and_units():
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_system_stats(
        metrics,
        SYSTEM,
        {"z": {"tank": {"d": 2, "du": 0.75, "rb": 1000, "wb": 3000, "h": "ONLINE"}}},
    )
    exporter.BeszelCollector.emit_storage_pools(
        metrics,
        SYSTEM,
        [
            {
                "name": "tank",
                "display_name": "Tank",
                "health": "ONLINE",
                "size": 10_000,
                "alloc": 7_000,
                "free": 3_000,
                "scrub": {"state": "running", "progress": "35.5%", "errors": 2},
                "vdevs": [
                    {
                        "name": "sda",
                        "state": "ONLINE",
                        "readErrs": 1,
                        "writeErrs": 2,
                        "checksumErrs": 3,
                    }
                ],
                "datasets": [
                    {"name": "tank/data", "mount": "/data", "used": 400, "avail": 800}
                ],
            }
        ],
        emit_dynamic=True,
    )
    assert one(metrics, "beszel_storage_pool_total_bytes")[0] == 2 * 1024**3
    assert one(metrics, "beszel_storage_pool_used_bytes")[0] == 0.75 * 1024**3
    assert one(metrics, "beszel_storage_pool_read_bytes_per_second")[0] == 1000
    assert one(metrics, "beszel_storage_pool_write_bytes_per_second")[0] == 3000
    assert one(metrics, "beszel_storage_pool_health_info")[1]["health"] == "ONLINE"
    assert one(metrics, "beszel_storage_pool_info")[1]["pool_type"] == "zfs"
    assert one(metrics, "beszel_storage_pool_size_bytes")[0] == 10_000
    assert one(metrics, "beszel_storage_pool_allocated_bytes")[0] == 7_000
    assert one(metrics, "beszel_storage_pool_free_bytes")[0] == 3_000
    assert one(metrics, "beszel_storage_pool_scrub_info")[1]["state"] == "running"
    assert one(metrics, "beszel_storage_pool_scrub_progress_percent")[0] == 35.5
    assert one(metrics, "beszel_storage_pool_scrub_errors")[0] == 2
    assert one(metrics, "beszel_storage_pool_vdev_info")[1]["state"] == "ONLINE"
    assert one(metrics, "beszel_storage_pool_vdev_read_errors")[0] == 1
    assert one(metrics, "beszel_storage_pool_vdev_write_errors")[0] == 2
    assert one(metrics, "beszel_storage_pool_vdev_checksum_errors")[0] == 3
    value, labels = one(metrics, "beszel_storage_pool_dataset_used_bytes")
    assert value == 400
    assert labels["dataset"] == "tank/data"
    assert labels["mount"] == "/data"
    assert one(metrics, "beszel_storage_pool_dataset_available_bytes")[0] == 800


def test_btrfs_pool_type_detected_from_prefix_and_static_info_survives():
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_storage_pools(
        metrics,
        SYSTEM,
        [{"name": "b:root", "display_name": "root", "size": 123}],
        emit_dynamic=False,
    )
    assert one(metrics, "beszel_storage_pool_info")[1]["pool_type"] == "btrfs"
    assert "beszel_storage_pool_size_bytes" not in metrics.families


def test_network_monitor_one_minute_aggregates_and_age():
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    monitor = {
        "id": "mon1",
        "target": "1.1.1.1",
        "protocol": "icmp",
        "port": 0,
        "enabled": True,
        "interval": 60,
        "res": 8000,
        "resAvg1h": 6000,
        "resMin1h": 3000,
        "resMax1h": 15000,
        "loss": 10,
        "loss1h": 15,
    }
    record = {
        "created": "2026-10-09T11:59:30Z",
        "total_count": 10,
        "success_count": 7,
        "res_sum": 35_000,
        "res_min": 1_000,
        "res_max": 14_000,
    }
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [monitor], {"mon1": record}, True, now
    )
    assert one(metrics, "beszel_network_monitor_enabled")[0] == 1
    assert one(metrics, "beszel_network_monitor_interval_seconds")[0] == 60
    assert one(metrics, "beszel_network_monitor_stats_age_seconds")[0] == 30
    probes = {labels["result"]: value for value, labels in samples(
        metrics, "beszel_network_monitor_probe_count"
    )}
    assert probes == {"total": 10, "success": 7}
    seconds = {labels["window"]: value for value, labels in samples(
        metrics, "beszel_network_monitor_response_seconds"
    )}
    assert seconds == {
        "current": pytest.approx(0.008),
        "1h_avg": pytest.approx(0.006),
        "1h_min": pytest.approx(0.003),
        "1h_max": pytest.approx(0.015),
        "1m_min": pytest.approx(0.001),
        "1m_max": pytest.approx(0.014),
        "1m_avg": pytest.approx(0.005),
    }
    loss = {labels["window"]: value for value, labels in samples(
        metrics, "beszel_network_monitor_packet_loss_percent"
    )}
    assert loss == {"current": 10, "1h": 15, "1m": 30}


def test_network_monitor_aggregates_skip_stale_data_and_zero_denominators():
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    monitor = {"id": "mon1", "res": 4500, "enabled": False}
    zero_record = {
        "created": "2026-10-09T11:59:30Z",
        "total_count": 0,
        "success_count": 0,
        "res_sum": 0,
    }
    result = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        result, SYSTEM, [monitor], {"mon1": zero_record}, True, now
    )
    assert one(result, "beszel_network_monitor_enabled")[0] == 0
    assert "beszel_network_monitor_probe_count" in result.families
    assert not any(
        labels["window"] == "1m_avg"
        for _, labels in result.families["beszel_network_monitor_response_seconds"]["samples"]
    )
    assert "beszel_network_monitor_packet_loss_percent" not in result.families

    stale = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        stale, SYSTEM, [monitor], {"mon1": zero_record}, True, now + 601
    )
    assert "beszel_network_monitor_probe_count" not in stale.families
    assert one(stale, "beszel_network_monitor_stats_age_seconds")[0] == 631

    source_down = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        source_down, SYSTEM, [monitor], {"mon1": zero_record}, False, now
    )
    assert "beszel_network_monitor_probe_count" not in source_down.families
    assert "beszel_network_monitor_response_seconds" not in source_down.families


def test_legacy_mib_rates_convert_to_bytes_and_new_bytes_take_precedence(monkeypatch):
    monkeypatch.setattr(exporter, "LEGACY_UNITS", False)
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_system_stats(
        metrics,
        SYSTEM,
        {
            "ns": 1.5, "nr": 0.25, "nsm": 3, "nrm": 4,
            "dr": 2, "dw": 0.125, "drm": 5, "dwm": 6,
            "efs": {"/data": {"r": 1, "w": 0.5, "rm": 2, "wm": 3}},
        },
    )
    net = {labels["direction"]: value for value, labels in samples(
        metrics, "beszel_system_network_bytes_per_second"
    )}
    assert net == {"transmit": 1.5 * 1024**2, "receive": 0.25 * 1024**2}
    disk = {labels["direction"]: value for value, labels in samples(
        metrics, "beszel_system_disk_io_bytes_per_second"
    )}
    assert disk == {"read": 2 * 1024**2, "write": 0.125 * 1024**2}
    peaks = {labels["direction"]: value for value, labels in samples(
        metrics, "beszel_system_network_peak_bytes_per_second"
    )}
    assert peaks == {"transmit": 3 * 1024**2, "receive": 4 * 1024**2}
    disk_peaks = {labels["direction"]: value for value, labels in samples(
        metrics, "beszel_system_disk_io_peak_bytes_per_second"
    )}
    assert disk_peaks == {"read": 5 * 1024**2, "write": 6 * 1024**2}
    assert one(metrics, "beszel_filesystem_read_bytes_per_second")[0] == 1024**2
    assert one(metrics, "beszel_filesystem_write_bytes_per_second")[0] == 0.5 * 1024**2
    assert one(metrics, "beszel_filesystem_read_peak_bytes_per_second")[0] == 2 * 1024**2
    assert one(metrics, "beszel_filesystem_write_peak_bytes_per_second")[0] == 3 * 1024**2
    assert "beszel_system_disk_read_mib_per_second" not in metrics.families

    preferred = exporter.PrometheusText()
    exporter.BeszelCollector.emit_system_stats(
        preferred, SYSTEM,
        {"b": [100, 200], "ns": 99, "nr": 99,
         "dio": [333, 444], "dr": 99, "dw": 99,
         "efs": {"/data": {"rb": 1234, "r": 99}}},
    )
    net_values = [value for value, _ in samples(
        preferred, "beszel_system_network_bytes_per_second"
    )]
    assert net_values == [100, 200]
    disk_values = [value for value, _ in samples(
        preferred, "beszel_system_disk_io_bytes_per_second"
    )]
    assert disk_values == [333, 444]
    assert one(preferred, "beszel_filesystem_read_bytes_per_second")[0] == 1234


def test_healthz_is_process_liveness_not_hub_readiness(monkeypatch):
    def unavailable_hub():
        raise RuntimeError("Hub is unavailable")

    monkeypatch.setattr(exporter.collector, "collect", unavailable_hub)
    # Tests use a minimal Flask stub; the HTTP route itself is covered by
    # the Docker CI smoke test. Here verify health_endpoint never polls Hub.
    assert exporter.health_endpoint() == {"status": "ok"}
