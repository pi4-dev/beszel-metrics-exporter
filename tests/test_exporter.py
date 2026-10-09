from __future__ import annotations

from datetime import datetime, timezone

import pytest

import beszel_exporter as exporter


class LatestAPI(exporter.BeszelAPI):
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return {
            "items": [{"id": "newest", "created": "2026-10-05T12:00:00Z"}],
            "totalPages": 60,
            "totalItems": 60,
        }


def test_latest_is_exactly_one_request():
    api = LatestAPI()
    row = api.latest("system_stats", 'system="abc" && type="1m"', "stats,created,type")
    assert row["id"] == "newest"
    assert len(api.calls) == 1
    _, params = api.calls[0]
    assert params["page"] == 1
    assert params["perPage"] == 1
    assert params["skipTotal"] == 1
    assert params["sort"] == "-created"


class BulkAPI(exporter.BeszelAPI):
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return {
            "items": [
                {"system": "s1", "created": "2026-10-05T12:00:00Z", "stats": {}},
                {"system": "s2", "created": "2026-10-05T11:59:00Z", "stats": {}},
                {"system": "s1", "created": "2026-10-05T11:58:00Z", "stats": {}},
            ]
        }


def test_bulk_latest_groups_by_relation_and_stops_after_one_page(monkeypatch):
    monkeypatch.setattr(exporter, "BULK_PAGE_SIZE", 500)
    api = BulkAPI()
    rows = api.latest_by_relation(
        "system_stats",
        "system",
        {"s1", "s2"},
        fields="system,stats,created,type",
    )
    assert set(rows) == {"s1", "s2"}
    assert rows["s1"]["created"] == "2026-10-05T12:00:00Z"
    assert len(api.calls) == 1


class TimeFilteredAPI(exporter.BeszelAPI):
    """Mimic PocketBase's timestamp filter with many old records in history."""

    def __init__(self, records):
        self.rows = records
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        assert params["skipTotal"] == 1
        assert params["sort"] == "-created"
        cutoff = params["filter"].split('created >= "', 1)[1].split('"', 1)[0]
        cutoff_timestamp = exporter.parse_timestamp(cutoff)
        rows = [
            row for row in self.rows
            if exporter.parse_timestamp(row["created"]) >= cutoff_timestamp
        ]
        rows.sort(key=lambda row: row["created"], reverse=True)
        size = params["perPage"]
        start = (params["page"] - 1) * size
        return {"items": rows[start : start + size]}


def test_fresh_history_filter_skips_large_old_history_and_offline_ids(monkeypatch, caplog):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "BULK_PAGE_SIZE", 500)
    monkeypatch.setattr(exporter, "BULK_MAX_PAGES", 5)
    rows = [
        {"system": "offline", "created": datetime.fromtimestamp(now - 10_000 - i, tz=timezone.utc).isoformat()}
        for i in range(3_000)
    ]
    rows.extend([
        {"system": "online", "created": datetime.fromtimestamp(now - 20, tz=timezone.utc).isoformat(), "stats": {"cpu": 12}},
        {"system": "offline", "created": datetime.fromtimestamp(now - 180.5, tz=timezone.utc).isoformat()},
    ])
    api = TimeFilteredAPI(rows)
    found = api.latest_by_relation(
        "system_stats", "system", {"online", "offline"},
        fields="system,stats,created,type", min_created=now - 180,
    )
    assert set(found) == {"online"}
    assert len(api.calls) == 1
    assert 'type="1m"' in api.calls[0][1]["filter"]
    assert 'created >= "2026-10-05 11:57:00.000Z"' in api.calls[0][1]["filter"]
    assert not any(record.levelname == "WARNING" for record in caplog.records)


def test_bulk_history_cutoff_includes_boundary_and_excludes_older():
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    api = TimeFilteredAPI([
        {"system": "fresh", "created": "2026-10-05T11:57:00Z"},
        {"system": "old", "created": "2026-10-05T11:56:59Z"},
    ])
    found = api.latest_by_relation(
        "system_stats", "system", {"fresh", "old"}, fields="system,created,type",
        min_created=now - 180,
    )
    assert set(found) == {"fresh"}


def test_json_decoding_accepts_objects_and_strings():
    value = {"x": [1, 2]}
    assert exporter.decoded(value, {}) == value
    assert exporter.decoded('{"x":[1,2]}', {}) == value
    assert exporter.decoded("not-json", {"fallback": True}) == {"fallback": True}


def test_unit_conversions():
    assert exporter.gib_to_bytes(1) == 1024**3
    assert exporter.mib_to_bytes(2) == 2 * 1024**2
    assert exporter.microseconds_to_seconds(12_500) == pytest.approx(0.0125)
    assert exporter.milliseconds_to_seconds(25) == pytest.approx(0.025)


def test_timestamp_age():
    now = datetime(2026, 10, 5, 12, 3, tzinfo=timezone.utc).timestamp()
    assert exporter.record_age_seconds({"created": "2026-10-05T12:00:00Z"}, now) == 180
    assert exporter.record_age_seconds({"created": "2026-10-05 12:00:00+00:00"}, now) == 180


def test_prometheus_text_groups_families_and_emits_help():
    metrics = exporter.PrometheusText()
    metrics.add("foo_metric", 1, {"a": "1"})
    metrics.add("bar_metric", 2)
    metrics.add("foo_metric", 3, {"a": "2"})
    rendered = metrics.render().splitlines()
    foo_help = rendered.index("# HELP foo_metric Beszel exporter metric foo_metric.")
    foo_type = rendered.index("# TYPE foo_metric gauge")
    bar_help = rendered.index("# HELP bar_metric Beszel exporter metric bar_metric.")
    assert foo_type == foo_help + 1
    assert rendered[foo_type + 1].startswith('foo_metric{a="1"}')
    assert rendered[foo_type + 2].startswith('foo_metric{a="2"}')
    assert bar_help > foo_type + 2


def test_prometheus_text_drops_duplicate_series_but_preserves_first(caplog):
    duplicate_metrics = []
    metrics = exporter.PrometheusText(on_duplicate=duplicate_metrics.append)
    metrics.add("duplicate_metric", 1, {"a": "x"})
    metrics.add("duplicate_metric", 2, {"a": "x"})
    metrics.add("duplicate_metric", 3, {"a": "x"})
    metrics.add("other_metric", 4, {"a": "x"})
    lines = metrics.render().splitlines()
    assert [line for line in lines if line.startswith("duplicate_metric{")] == ['duplicate_metric{a="x"} 1']
    assert 'other_metric{a="x"} 4' in lines
    assert metrics.dropped_samples == 2
    assert duplicate_metrics == ["duplicate_metric", "duplicate_metric"]
    warnings = [record for record in caplog.records if "Dropping duplicate Prometheus sample" in record.message]
    assert len(warnings) == 1
    assert "duplicate_metric" in warnings[0].message


def test_prometheus_text_still_rejects_metric_type_conflicts():
    metrics = exporter.PrometheusText()
    metrics.add("my_metric", 1, metric_type="gauge")
    with pytest.raises(ValueError, match="Metric type conflict"):
        metrics.add("my_metric", 2, metric_type="counter")


def test_prometheus_text_info_only_keeps_well_formed_info_families():
    metrics = exporter.PrometheusText()
    metrics.info("source_info", {"name": "source-a"})
    metrics.add("source_cpu_percent", 42, {"name": "source-a"})
    metrics.add("source_up", 1, {"name": "source-a"})
    rendered = metrics.render(info_only=True)
    assert "# HELP source_info " in rendered
    assert "# TYPE source_info gauge" in rendered
    assert 'source_info{name="source-a"} 1' in rendered
    assert "source_cpu_percent" not in rendered
    assert "source_up" not in rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Up 2 minutes", "running"),
        ("Up About an hour", "running"),
        ("Up 3 hours (healthy)", "running"),
        ("Up 3 hours (unhealthy)", "running"),
        ("Up 4 hours (Paused)", "paused"),
        ("Exited (0) 2 hours ago", "exited"),
        ("Exited (137) 20 seconds ago", "exited"),
        ("Restarting (1) 5 seconds ago", "restarting"),
        ("Created", "created"),
        ("Dead", "dead"),
        ("Removing", "removing"),
        ("Running", "running"),
        ("", "unknown"),
        (None, "unknown"),
        ("Up 2 minutes; unique-value=123", "running"),
        ("unexpected 123456", "unknown"),
    ],
)
def test_normalized_container_status_is_bounded(raw, expected):
    assert exporter.normalized_container_status(raw) == expected


@pytest.mark.parametrize("value", ["up", "down", "paused", "pending"])
def test_normalized_system_status_known_values(value):
    assert exporter.normalized_system_status(value) == value


@pytest.mark.parametrize("value", ["Up 3 minutes", "", None, "down 10s"])
def test_normalized_system_status_unknown_values(value):
    assert exporter.normalized_system_status(value) == "unknown"

class FakeAPI:
    def __init__(self, now: float, stale: bool = False, fail_systems: bool = False):
        self.now = now
        self.stale = stale
        self.fail_systems = fail_systems

    def records(self, collection, **kwargs):
        if collection == "systems":
            if self.fail_systems:
                raise RuntimeError("hub unavailable at http://secret-internal:8090")
            return [
                {
                    "id": "sys1",
                    "name": "source-a",
                    "status": "up",
                    "info": {
                        "u": 1234,
                        "wf": {"wlan0": {"s": "HomeSSID"}},
                    },
                }
            ]
        if collection == "system_details":
            return [{"id": "sys1", "system": "sys1", "hostname": "source-a", "cores": 4, "threads": 4}]
        if collection == "containers":
            return [{"id": "cid", "system": "sys1", "name": "app", "cpu": 3, "memory": 12, "net": 100}]
        if collection == "smart_devices":
            return [
                {
                    "id": "smart1",
                    "system": "sys1",
                    "name": "/dev/sda",
                    "model": "Disk",
                    "serial": "SERIAL",
                    "firmware": "1.0",
                    "state": "PASSED",
                    "temp": 35,
                    "capacity": 1000,
                    "attributes": [],
                }
            ]
        if collection == "systemd_services":
            return [{"id": "svc", "system": "sys1", "name": "docker.service", "state": 0, "sub": 1, "cpu": 1}]
        if collection == "network_monitors":
            return [
                {
                    "id": "mon1",
                    "system": "sys1",
                    "target": "1.1.1.1",
                    "protocol": "icmp",
                    "port": 0,
                    "enabled": True,
                    "interval": 60,
                    "res": 10_000,
                    "loss": 0,
                    "certInfo": {},
                }
            ]
        if collection == "zfs_pools":
            return []
        return []

    def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
        age = 600 if self.stale else 30
        created = datetime.fromtimestamp(self.now - age, tz=timezone.utc).isoformat()
        if collection == "system_stats":
            return {
                "sys1": {
                    "system": "sys1",
                    "created": created,
                    "stats": {
                        "cpu": 10,
                        "m": 2,
                        "mu": 1,
                        "mp": 50,
                        "d": 10,
                        "du": 5,
                        "dp": 50,
                        "efs": {"/data": {"d": 4, "du": 2}},
                        "wf": {"wlan0": -50},
                        "dios": [1, 2, 3, 25, 50, 6],
                    },
                }
            }
        if collection == "container_stats":
            return {"sys1": {"system": "sys1", "created": created, "stats": [{"n": "app", "b": [40, 60]}]}}
        if collection == "network_monitor_stats":
            return {
                "mon1": {
                    "monitor": "mon1",
                    "created": created,
                    "res_min": 8_000,
                    "res_max": 12_000,
                    "res_sum": 20_000,
                    "total_count": 2,
                    "success_count": 2,
                }
            }
        return {}


def test_collector_passes_independent_freshness_cutoffs_to_all_history_queries(monkeypatch):
    class CutoffAPI(FakeAPI):
        def __init__(self, now):
            super().__init__(now)
            self.cutoffs = {}

        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            self.cutoffs[collection] = kwargs["min_created"]
            return super().latest_by_relation(collection, relation_field, wanted_ids, **kwargs)

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    monkeypatch.setattr(exporter, "MAX_STATS_AGE_SECONDS", 180)
    monkeypatch.setattr(exporter, "MAX_MONITOR_STATS_AGE_SECONDS", 600)
    api = CutoffAPI(now)
    result = exporter.BeszelCollector(api=api, clock=lambda: now).collect()
    assert "beszel_exporter_up 1" in result
    assert api.cutoffs == {
        "system_stats": now - 180,
        "container_stats": now - 180,
        "network_monitor_stats": now - 600,
    }

def test_stale_stats_are_not_exported(monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    collector = exporter.BeszelCollector(api=FakeAPI(now, stale=True), clock=lambda: now)
    text = collector.collect()
    assert "beszel_system_stats_age_seconds" in text
    assert "beszel_system_cpu_usage_percent" not in text
    assert "beszel_container_cpu_usage_percent" not in text
    assert "beszel_smart_temperature_celsius" not in text
    assert 'beszel_network_monitor_response_seconds{' not in text
    assert 'beszel_system_up{system="source-a",system_id="sys1"} 1' in text


def test_fresh_stats_have_no_duplicate_fs_or_wifi_samples(monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    collector = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now)
    text = collector.collect()
    fs_samples = [line for line in text.splitlines() if line.startswith("beszel_filesystem_usage_percent{")]
    wifi_samples = [line for line in text.splitlines() if line.startswith("beszel_system_wifi_rssi_dbm{")]
    assert len(fs_samples) == 1
    assert len(wifi_samples) == 1
    assert "ssid=" not in wifi_samples[0]
    assert "beszel_system_disk_read_await_seconds" in text
    assert "beszel_system_disk_read_await_milliseconds" not in text


def test_container_info_status_does_not_change_with_uptime(monkeypatch):
    class StatusAPI(FakeAPI):
        status = "Up 2 minutes"

        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "containers":
                rows[0]["status"] = self.status
            return rows

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    api = StatusAPI(now)
    collector = exporter.BeszelCollector(api=api, clock=lambda: now)
    first = collector.collect()
    api.status = "Up 3 hours (healthy)"
    second = collector.collect()
    def info_line(output):
        return next(line for line in output.splitlines() if line.startswith("beszel_container_info{"))
    assert info_line(first) == info_line(second)
    assert 'status="running"' in info_line(second)
    assert "3 hours" not in second


def test_systemd_info_unknown_numeric_codes_are_bounded():
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_systemd(
        metrics,
        {"system": "source-a", "system_id": "sys1"},
        [{"name": "x.service", "state": 12345, "sub": 98765}],
        emit_dynamic=False,
    )
    result = metrics.render()
    assert 'state="unknown"' in result
    assert 'substate="unknown"' in result
    assert "12345" not in result and "98765" not in result

def test_container_network_total_is_separate_metric(monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    collector = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now)
    text = collector.collect()
    assert "beszel_container_network_combined_bytes_per_second" in text
    lines = [line for line in text.splitlines() if line.startswith("beszel_container_network_bytes_per_second{")]
    assert len(lines) == 2
    assert all('direction="total"' not in line for line in lines)


def test_smart_metadata_only_on_info(monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    collector = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now)
    text = collector.collect()
    temp = next(line for line in text.splitlines() if line.startswith("beszel_smart_temperature_celsius{"))
    info = next(line for line in text.splitlines() if line.startswith("beszel_smart_device_info{"))
    assert "model=" not in temp and "firmware=" not in temp
    assert 'serial="SERIAL"' in temp
    assert 'model="Disk"' in info and 'firmware="1.0"' in info


def test_duplicate_source_samples_are_isolated_and_counted(monkeypatch):
    class DuplicateAPI(FakeAPI):
        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "containers":
                # Different IDs, but the numeric metrics use the same container name.
                rows.append({"id": "cid-other", "system": "sys1", "name": "app", "cpu": 99, "memory": 20})
            if collection == "zfs_pools":
                return [{"system": "sys1", "name": "pool", "datasets": [
                    {"name": "ds", "mount": "/mnt/ds", "used": 12, "avail": 20},
                    {"name": "ds", "mount": "/mnt/ds", "used": 99, "avail": 90},
                ]}]
            return rows

        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            records = super().latest_by_relation(collection, relation_field, wanted_ids, **kwargs)
            if collection == "system_stats":
                stats = records["sys1"]["stats"]
                stats["bats"] = {"primary": 80}
                stats["bat"] = [40, 1]
            return records

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    api = DuplicateAPI(now)
    collector = exporter.BeszelCollector(api=api, clock=lambda: now)
    first = collector.collect()
    assert "beszel_exporter_up 1" in first
    assert "beszel_system_info" in first
    assert "beszel_container_info" in first
    assert "beszel_systemd_service_info" in first
    assert 'beszel_system_battery_percent{battery="primary",system="source-a",system_id="sys1"} 80' in first
    assert 'beszel_container_cpu_usage_percent{container="app",system="source-a",system_id="sys1"} 3' in first
    assert 'beszel_storage_pool_dataset_used_bytes{dataset="ds",mount="/mnt/ds",pool="pool",system="source-a",system_id="sys1"} 12' in first
    dropped_first = collector.dropped_samples_total
    assert dropped_first >= 4
    assert f"beszel_exporter_dropped_samples_total {dropped_first}" in first
    assert "# TYPE beszel_exporter_dropped_samples_total counter" in first
    second = collector.collect()
    assert collector.dropped_samples_total == 2 * dropped_first
    assert f"beszel_exporter_dropped_samples_total {2 * dropped_first}" in second
    assert "beszel_exporter_up 1" in second


def test_duplicate_count_survives_unrelated_hub_outage(monkeypatch):
    class DuplicateContainerAPI(FakeAPI):
        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "containers":
                rows.append({"id": "other", "system": "sys1", "name": "app", "cpu": 123})
            return rows

    now = [datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    api = DuplicateContainerAPI(now[0])
    collector = exporter.BeszelCollector(api=api, clock=lambda: now[0])
    assert "beszel_exporter_up 1" in collector.collect()
    prior = collector.dropped_samples_total
    assert prior > 0
    api.fail_systems = True
    now[0] += 1
    failed = collector.collect()
    assert "beszel_exporter_up 0" in failed
    assert f"beszel_exporter_dropped_samples_total {prior}" in failed

def test_hub_failure_returns_only_metadata_and_exporter_metrics(monkeypatch):
    now = [datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    monkeypatch.setattr(exporter, "FAILURE_CACHE_TTL", 5)
    api = FakeAPI(now[0])
    collector = exporter.BeszelCollector(api=api, clock=lambda: now[0])
    good = collector.collect()
    assert "beszel_exporter_up 1" in good
    assert "beszel_system_cpu_usage_percent" in good
    assert "beszel_container_cpu_usage_percent" in good
    assert "beszel_system_up" in good
    last_success = collector.last_success

    api.fail_systems = True
    now[0] += 1
    failed = collector.collect()
    assert "beszel_exporter_up 0" in failed
    assert "beszel_system_info" in failed
    assert "beszel_container_info" in failed
    assert "# TYPE beszel_system_info gauge" in failed
    assert "beszel_exporter_last_success_timestamp_seconds" in failed
    assert collector.last_success == last_success
    assert "secret-internal" not in failed

    # Only descriptive metadata may be replayed. Host up=1 is omitted too:
    # with the Hub unavailable, exporter cannot verify current host availability.
    for line in failed.splitlines():
        if line.startswith("#") or not line:
            continue
        metric_name = line.split("{", 1)[0].split(" ", 1)[0]
        assert metric_name.endswith("_info") or metric_name.startswith("beszel_exporter_")
    assert "beszel_system_cpu_usage_percent" not in failed
    assert "beszel_system_temperature_celsius" not in failed
    assert "beszel_system_up" not in failed
    assert "beszel_system_stats_age_seconds" not in failed
    assert "beszel_container_cpu_usage_percent" not in failed
    assert "beszel_network_monitor_response_seconds" not in failed

    now[0] += 1
    assert collector.collect() == failed  # FAILURE_CACHE_TTL protects Hub from retries

    # Repeated scrapes hours into the outage must not reintroduce CPU, disk,
    # temperature or availability values with new timestamps.
    now[0] += 3600
    still_failed = collector.collect()
    assert "beszel_exporter_up 0" in still_failed
    assert "beszel_system_cpu_usage_percent" not in still_failed
    assert "beszel_system_up" not in still_failed
    assert "beszel_system_info" in still_failed
    assert collector.last_success == last_success

    api.fail_systems = False
    api.now = now[0]  # New upstream records on recovery
    now[0] += 6
    recovered = collector.collect()
    assert "beszel_exporter_up 1" in recovered
    assert "beszel_system_cpu_usage_percent" in recovered
    assert "beszel_system_up" in recovered
    assert collector.last_success > last_success


def test_hub_failure_before_initial_success_has_only_self_metrics(monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    api = FakeAPI(now, fail_systems=True)
    collector = exporter.BeszelCollector(api=api, clock=lambda: now)
    failed = collector.collect()
    assert "beszel_exporter_up 0" in failed
    assert "beszel_system_info" not in failed
    assert "beszel_system_up" not in failed
    assert "beszel_system_cpu_usage_percent" not in failed
