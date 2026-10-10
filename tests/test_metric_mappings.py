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


@pytest.mark.parametrize(
    ("updates", "total", "security"),
    [
        ([12, 3], 12, 3),
        ([4], 4, None),  # Upstream security omitted when unknown.
        ([5, 0], 5, 0),
        ([2, 3], 2, None),  # Cannot be a subset, suppress bad security data.
        ([], None, None),
    ],
)
def test_package_updates_are_distinct_with_optional_security(updates, total, security):
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_system_record(
        metrics,
        SYSTEM,
        {"status": "up", "info": {"pu": updates}},
        {},
        True,
    )
    if total is None:
        assert "beszel_package_updates_pending" not in metrics.families
    else:
        assert one(metrics, "beszel_package_updates_pending") == (total, SYSTEM)
    if security is None:
        assert "beszel_package_security_updates_pending" not in metrics.families
    else:
        assert one(metrics, "beszel_package_security_updates_pending") == (security, SYSTEM)
    assert all(
        "type" not in labels
        for name in ("beszel_package_updates_pending", "beszel_package_security_updates_pending")
        for _, labels in metrics.families.get(name, {"samples": []})["samples"]
    )


def test_network_monitor_one_minute_aggregates_and_age():
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    monitor = {
        "id": "mon1",
        "target": "1.1.1.1",
        "protocol": "icmp",
        "port": 0,
        "enabled": True,
        "interval": 60,
        "updated": "2026-10-09T11:59:30Z",
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
    probe_series = samples(metrics, "beszel_network_monitor_probes")
    assert all(labels["window"] == "1m" for _, labels in probe_series)
    probes = {labels["result"]: value for value, labels in probe_series}
    assert probes == {"success": 7, "failure": 3}
    assert sum(probes.values()) == 10  # No overlapping total/success series.
    assert metrics.families["beszel_network_monitor_probes"]["type"] == "gauge"
    exposition = metrics.render()
    assert "# TYPE beszel_network_monitor_probes gauge" in exposition
    assert "# HELP beszel_network_monitor_probes Number of successful or failed probes in the latest 1-minute Beszel monitor history aggregate." in exposition
    assert 'result="total"' not in exposition
    assert "beszel_network_monitor_probe_count" not in exposition
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


def test_network_monitor_total_packet_loss_omits_all_response_windows():
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    monitor = {
        "id": "mon1",
        "enabled": True,
        "updated": "2026-10-09T11:59:30Z",
        "res": 0,
        "loss": 100,
        "loss1h": 100,
        "resAvg1h": 0,
        "resMin1h": 0,
        "resMax1h": 0,
    }
    record = {
        "created": "2026-10-09T11:59:30Z",
        "total_count": 5,
        "success_count": 0,
        "res_sum": 0,
        "res_min": 0,
        "res_max": 0,
    }
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [monitor], {"mon1": record}, True, now
    )
    assert "beszel_network_monitor_response_seconds" not in metrics.families
    probes = {
        (labels["result"], labels["window"]): value
        for value, labels in samples(metrics, "beszel_network_monitor_probes")
    }
    assert probes == {("success", "1m"): 0, ("failure", "1m"): 5}
    losses = {
        labels["window"]: value
        for value, labels in samples(metrics, "beszel_network_monitor_packet_loss_percent")
    }
    assert losses == {"current": 100, "1h": 100, "1m": 100}


@pytest.mark.parametrize(
    ("loss1h", "values", "expected"),
    [
        (100, (5000, 2000, 8000), {}),
        (99, (5000, 2000, 8000), {
            "1h_avg": 0.005, "1h_min": 0.002, "1h_max": 0.008
        }),
        (10, (0, 2000, 0), {"1h_min": 0.002}),
        (None, (5000, 2000, 8000), {}),
    ],
)
def test_monitor_hourly_latency_requires_successful_positive_response(
    loss1h, values, expected
):
    monitor = {
        "id": "mon1",
        "enabled": True,
        "updated": "2026-10-09T12:00:00Z",
        "res": 0,
        "loss1h": loss1h,
        "resAvg1h": values[0],
        "resMin1h": values[1],
        "resMax1h": values[2],
    }
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [monitor], {}, True,
        datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    )
    observed = {
        labels["window"]: value
        for value, labels in metrics.families.get(
            "beszel_network_monitor_response_seconds", {"samples": []}
        )["samples"]
    }
    assert observed == expected
    assert "current" not in observed


def test_network_monitor_current_latency_requires_positive_response():
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM,
        [{
            "id": "mon1", "enabled": True,
            "updated": "2026-10-09T12:00:00Z",
            "res": 1200, "loss1h": 100, "resAvg1h": 0,
        }],
        {}, True, datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp(),
    )
    response = samples(metrics, "beszel_network_monitor_response_seconds")
    assert len(response) == 1
    assert response[0][1]["window"] == "current"
    assert response[0][0] == pytest.approx(0.0012)


@pytest.mark.parametrize(
    ("enabled", "updated_age", "interval", "expected_current"),
    [
        (False, 30, 30, False),
        (True, 3600, 30, False),
        (True, None, 30, False),
        (True, 180, 30, True),  # Default threshold, inclusive boundary.
        (True, 181, 30, False),
        (True, 599, 300, True),  # Slow probes: 2 * interval exceeds 180s.
        (True, 600, 300, True),
        (True, 601, 300, False),
    ],
)
def test_network_monitor_current_and_hourly_require_own_fresh_update(
    enabled, updated_age, interval, expected_current
):
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    monitor = {
        "id": "mon1",
        "enabled": enabled,
        "interval": interval,
        "res": 9000,
        "resAvg1h": 7000,
        "resMin1h": 2000,
        "resMax1h": 15000,
        "loss": 10,
        "loss1h": 20,
    }
    if updated_age is not None:
        monitor["updated"] = datetime.fromtimestamp(
            now - updated_age, tz=timezone.utc
        ).isoformat()
    record = {
        "created": datetime.fromtimestamp(now - 20, tz=timezone.utc).isoformat(),
        "total_count": 5,
        "success_count": 4,
        "res_sum": 16_000,
        "res_min": 1000,
        "res_max": 8000,
    }
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [monitor], {"mon1": record}, True, now
    )
    response = {
        labels["window"]: value
        for value, labels in samples(metrics, "beszel_network_monitor_response_seconds")
    }
    loss = {
        labels["window"]: value
        for value, labels in samples(metrics, "beszel_network_monitor_packet_loss_percent")
    }
    # Freshly timestamped history is independent of the cached monitor row.
    assert response["1m_min"] == pytest.approx(0.001)
    assert response["1m_avg"] == pytest.approx(0.004)
    assert response["1m_max"] == pytest.approx(0.008)
    assert loss["1m"] == pytest.approx(20)
    assert "beszel_network_monitor_interval_seconds" in metrics.families
    assert one(metrics, "beszel_network_monitor_enabled")[0] == int(enabled)
    current_keys = {"current", "1h_avg", "1h_min", "1h_max"}
    if expected_current:
        assert current_keys <= response.keys()
        assert {"current", "1h"} <= loss.keys()
    else:
        assert not (current_keys & response.keys())
        assert not ({"current", "1h"} & loss.keys())


def test_network_monitor_host_freshness_still_required_for_current_and_hourly():
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    monitor = {
        "id": "mon1", "enabled": True, "interval": 30,
        "updated": "2026-10-09T11:59:50Z",
        "res": 9000, "resMin1h": 2000, "loss1h": 50, "loss": 0,
    }
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [monitor], {}, False, now
    )
    assert "beszel_network_monitor_response_seconds" not in metrics.families
    assert "beszel_network_monitor_packet_loss_percent" not in metrics.families
    assert one(metrics, "beszel_network_monitor_enabled")[0] == 1


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
    assert "beszel_network_monitor_probes" in result.families
    assert {
        (labels["result"], labels["window"]): value
        for value, labels in samples(result, "beszel_network_monitor_probes")
    } == {("success", "1m"): 0, ("failure", "1m"): 0}
    assert not any(
        labels["window"] == "1m_avg"
        for _, labels in result.families["beszel_network_monitor_response_seconds"]["samples"]
    )
    assert "beszel_network_monitor_packet_loss_percent" not in result.families

    stale = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        stale, SYSTEM, [monitor], {"mon1": zero_record}, True, now + 601
    )
    assert "beszel_network_monitor_probes" not in stale.families
    assert one(stale, "beszel_network_monitor_stats_age_seconds")[0] == 631

    source_down = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        source_down, SYSTEM, [monitor], {"mon1": zero_record}, False, now
    )
    assert "beszel_network_monitor_probes" not in source_down.families
    assert "beszel_network_monitor_response_seconds" not in source_down.families


@pytest.mark.parametrize(
    ("total_count", "success_count", "expected"),
    [
        (10, 10, {"success": 10, "failure": 0}),
        (10, 0, {"success": 0, "failure": 10}),
        (10, 7, {"success": 7, "failure": 3}),
        (0, 0, {"success": 0, "failure": 0}),
        (2, 3, None),  # Upstream consistency violation: do not emit negatives.
    ],
)
def test_network_monitor_probe_buckets_are_disjoint(
    total_count, success_count, expected
):
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics,
        SYSTEM,
        [{"id": "mon1", "interval": 30}],
        {"mon1": {
            "created": "2026-10-09T11:59:30Z",
            "total_count": total_count,
            "success_count": success_count,
            "res_sum": 9000,
        }},
        True,
        now,
    )
    if expected is None:
        assert "beszel_network_monitor_probes" not in metrics.families
        assert not any(
            labels.get("window") == "1m"
            for _, labels in metrics.families.get(
                "beszel_network_monitor_packet_loss_percent", {"samples": []}
            )["samples"]
        )
    else:
        got = {
            labels["result"]: value
            for value, labels in samples(metrics, "beszel_network_monitor_probes")
            if labels["window"] == "1m"
        }
        assert got == expected
        assert sum(got.values()) == total_count

    averages = [
        value for value, labels in metrics.families.get(
            "beszel_network_monitor_response_seconds", {"samples": []}
        )["samples"] if labels.get("window") == "1m_avg"
    ]
    if success_count > 0:
        assert averages == [pytest.approx(9000 / success_count / 1_000_000)]
    else:
        assert averages == []


@pytest.mark.parametrize("missing_sum", ["absent", None, "", "not-a-number"])
def test_network_monitor_missing_response_sum_does_not_fabricate_zero_avg(missing_sum):
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    record = {
        "created": "2026-10-09T11:59:30Z",
        "total_count": 5,
        "success_count": 3,
        "res_min": 1200,
        "res_max": 8000,
    }
    if missing_sum != "absent":
        record["res_sum"] = missing_sum
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [{"id": "mon1"}], {"mon1": record}, True, now
    )
    probes = {
        labels["result"]: value
        for value, labels in samples(metrics, "beszel_network_monitor_probes")
    }
    assert probes == {"success": 3, "failure": 2}
    latency = {
        labels["window"]: value
        for value, labels in samples(metrics, "beszel_network_monitor_response_seconds")
    }
    assert latency == {"1m_min": pytest.approx(0.0012), "1m_max": pytest.approx(0.008)}
    loss = {
        labels["window"]: value
        for value, labels in samples(metrics, "beszel_network_monitor_packet_loss_percent")
    }
    assert loss == {"1m": 40}


@pytest.mark.parametrize("missing_key", ["total_count", "success_count"])
def test_network_monitor_missing_count_does_not_fabricate_probe_buckets(missing_key):
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    record = {
        "created": "2026-10-09T11:59:30Z",
        "total_count": 5,
        "success_count": 3,
        "res_sum": 12_000,
        "res_min": 1000,
        "res_max": 6000,
    }
    record.pop(missing_key)
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [{"id": "mon1"}], {"mon1": record}, True, now
    )
    assert "beszel_network_monitor_probes" not in metrics.families
    assert "beszel_network_monitor_packet_loss_percent" not in metrics.families
    observed = {
        labels["window"]: value
        for value, labels in metrics.families.get(
            "beszel_network_monitor_response_seconds", {"samples": []}
        )["samples"]
    }
    if missing_key == "total_count":
        assert observed == {
            "1m_min": pytest.approx(0.001),
            "1m_max": pytest.approx(0.006),
            "1m_avg": pytest.approx(0.004),
        }
    else:
        assert observed == {}


def test_network_monitor_average_survives_missing_total_count():
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc).timestamp()
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics,
        SYSTEM,
        [{"id": "mon1", "interval": 30}],
        {"mon1": {
            "created": "2026-10-09T11:59:30Z",
            "success_count": 3,
            "res_sum": 12_000,
        }},
        True,
        now,
    )
    assert "beszel_network_monitor_probes" not in metrics.families
    assert "beszel_network_monitor_packet_loss_percent" not in metrics.families
    avg = [
        value for value, labels in samples(
            metrics, "beszel_network_monitor_response_seconds"
        ) if labels["window"] == "1m_avg"
    ]
    assert avg == [pytest.approx(0.004)]


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


@pytest.mark.parametrize("interval_seconds", [1, 30, 60, 299, 300, 450, 539, 540, 600, 1200])
def test_monitor_interval_units_are_seconds(interval_seconds):
    # Upstream: Hub copies network_monitors.interval (integer) unchanged;
    # agent schedules probes with time.Duration(interval) * time.Second.
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics,
        SYSTEM,
        [{"id": "monitor-one", "interval": interval_seconds, "enabled": True}],
        {},
        source_fresh=False,
        now=0.0,
    )
    value, labels = one(metrics, "beszel_network_monitor_interval_seconds")
    assert value == interval_seconds
    assert labels["monitor_id"] == "monitor-one"



@pytest.mark.parametrize(
    ("raw_interval", "expected_seconds"),
    [
        (None, 30),
        ("", 30),
        (0, 30),
        ("0", 30),
        ("not-a-number", 30),
        (30, 30),
        (60, 60),
        (299, 299),
        (300, 300),
        (450, 450),
    ],
)
def test_monitor_interval_uses_agent_effective_default(raw_interval, expected_seconds):
    monitor = {"id": "mon1", "interval": raw_interval, "enabled": True}
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [monitor], {}, source_fresh=False, now=0.0
    )
    assert one(metrics, "beszel_network_monitor_interval_seconds")[0] == expected_seconds


def test_monitor_interval_missing_field_uses_30_seconds():
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics, SYSTEM, [{"id": "mon1"}], {}, source_fresh=False, now=0.0
    )
    assert one(metrics, "beszel_network_monitor_interval_seconds")[0] == 30
