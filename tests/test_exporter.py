from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

import pytest

import beszel_exporter as exporter


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


class NumericMonitorHistoryAPI(exporter.BeszelAPI):
    """Emulate PocketBase's numeric-ms timestamp field and server-side filter."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        assert path == "/api/collections/network_monitor_stats/records"
        assert params["sort"] == "-created"
        assert params["skipTotal"] == 1
        assert 'type="1m"' in params["filter"]
        # Date-string cutoff is incorrect for this Beszel collection.
        assert 'created >= "' not in params["filter"]
        cutoff = int(params["filter"].split("created >= ", 1)[1].split()[0].strip(")"))
        filtered = [
            row for row in self.rows
            if row["type"] == "1m" and row["created"] >= cutoff
        ]
        filtered.sort(key=lambda row: row["created"], reverse=True)
        size = params["perPage"]
        start = (params["page"] - 1) * size
        return {"items": filtered[start:start + size]}


def test_network_monitor_numeric_ms_history_filter_and_latency_windows():
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    cutoff_ms = int((now - 600) * 1000)
    api = NumericMonitorHistoryAPI([
        {"monitor": "old", "created": cutoff_ms - 1, "type": "1m"},
        {"monitor": "edge", "created": cutoff_ms, "type": "1m",
         "success_count": 1, "total_count": 1, "res_sum": 7000,
         "res_min": 7000, "res_max": 7000},
        {"monitor": "fresh", "created": int((now - 30) * 1000), "type": "1m",
         "success_count": 3, "total_count": 4, "res_sum": 15000,
         "res_min": 2000, "res_max": 9000},
        {"monitor": "wrong-tier", "created": int((now - 20) * 1000), "type": "10m"},
    ])
    records = api.latest_by_relation(
        "network_monitor_stats", "monitor",
        {"old", "edge", "fresh", "wrong-tier"},
        fields="monitor,created,type,total_count,success_count,res_sum,res_min,res_max",
        min_created=now - 600,
        created_format="unix_ms",
    )
    assert set(records) == {"edge", "fresh"}
    assert f"created >= {cutoff_ms}" in api.calls[0][1]["filter"]
    assert exporter.record_age_seconds(records["fresh"], now) == pytest.approx(30)
    assert exporter.record_age_seconds(records["edge"], now) == pytest.approx(600)

    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_network_monitors(
        metrics,
        {"system": "source-a", "system_id": "sys1"},
        [{"id": "fresh", "interval": 30, "enabled": True}],
        records,
        source_fresh=True,
        now=now,
    )
    latency = {
        labels["window"]: value
        for value, labels in metrics.families["beszel_network_monitor_response_seconds"]["samples"]
    }
    assert latency["1m_min"] == pytest.approx(0.002)
    assert latency["1m_max"] == pytest.approx(0.009)
    assert latency["1m_avg"] == pytest.approx(0.005)
    assert metrics.families["beszel_network_monitor_stats_age_seconds"]["samples"][0][0] == 30


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


def test_fresh_scan_page_limit_warns_only_if_unresolved_ids(monkeypatch, caplog):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "BULK_PAGE_SIZE", 2)
    monkeypatch.setattr(exporter, "BULK_MAX_PAGES", 1)
    api = TimeFilteredAPI([
        {"system": "a", "created": "2026-10-05T11:59:50Z"},
        {"system": "b", "created": "2026-10-05T11:59:40Z"},
    ])
    found = api.latest_by_relation(
        "system_stats", "system", {"a", "b"}, fields="system,created,type",
        min_created=now - 180,
    )
    assert set(found) == {"a", "b"}
    assert len(api.calls) == 1
    assert not any(record.levelname == "WARNING" for record in caplog.records)

    api.calls.clear()
    found = api.latest_by_relation(
        "system_stats", "system", {"a", "b", "c"}, fields="system,created,type",
        min_created=now - 180,
    )
    assert set(found) == {"a", "b"}
    assert len(api.calls) == 1
    assert any("reached page limit" in record.message for record in caplog.records)


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
    assert duplicate_metrics == ["duplicate_metric", "duplicate_metric"]
    warnings = [record for record in caplog.records if "Dropping duplicate Prometheus sample" in record.message]
    assert len(warnings) == 1
    assert "duplicate_metric" in warnings[0].message


def test_prometheus_text_still_rejects_metric_type_conflicts():
    metrics = exporter.PrometheusText()
    metrics.add("my_metric", 1, metric_type="gauge")
    with pytest.raises(ValueError, match="Metric type conflict"):
        metrics.add("my_metric", 2, metric_type="counter")


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
                        "sv": [42, 1],
                        "pu": [12, 3],
                        "wf": {"wlan0": {"s": "HomeSSID"}},
                    },
                }
            ]
        if collection == "system_details":
            return [{"id": "sys1", "system": "sys1", "hostname": "source-a", "cores": 4, "threads": 4}]
        if collection == "containers":
            return [{
                "id": "cid", "system": "sys1", "name": "app",
                "cpu": 3, "memory": 12, "net": 100,
                "updated": int((self.now - 30) * 1000),
            }]
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
            return [{
                "id": "svc", "system": "sys1", "name": "docker.service",
                "state": 0, "sub": 1, "cpu": 1,
                "updated": int((self.now - 60) * 1000),
            }]
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
                    "updated": datetime.fromtimestamp(
                        self.now - 30, tz=timezone.utc
                    ).isoformat(),
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

def history_coverage(output):
    """Return collection-level wanted/found metrics from text exposition."""
    observed = {}
    for line in output.splitlines():
        for metric in ("wanted", "found"):
            prefix = f'beszel_exporter_history_records_{metric}{{collection="'
            if line.startswith(prefix):
                collection, value = line[len(prefix):].split('"} ', 1)
                observed[(metric, collection)] = float(value)
    return observed


def test_history_coverage_reports_all_collections(monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    result = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now).collect()
    assert "# TYPE beszel_exporter_history_records_wanted gauge" in result
    assert "# TYPE beszel_exporter_history_records_found gauge" in result
    assert history_coverage(result) == {
        (metric, collection): 1.0
        for metric in ("wanted", "found")
        for collection in ("system_stats", "container_stats", "network_monitor_stats")
    }
    assert 'beszel_exporter_history_records_wanted{system=' not in result
    assert 'beszel_exporter_history_records_found{system=' not in result


def test_history_coverage_reports_missing_history(monkeypatch):
    class EmptyHistoryAPI(FakeAPI):
        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            return {}

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    result = exporter.BeszelCollector(
        api=EmptyHistoryAPI(now), clock=lambda: now
    ).collect()
    assert "beszel_exporter_up 1" in result
    assert history_coverage(result) == {
        (metric, collection): float(value)
        for collection in ("system_stats", "container_stats", "network_monitor_stats")
        for metric, value in (("wanted", 1), ("found", 0))
    }
    assert "beszel_network_monitor_stats_age_seconds" not in result


def test_history_coverage_handles_zero_sources_and_partial_results(monkeypatch):
    class PartialAPI(FakeAPI):
        def __init__(self, now, empty=False):
            super().__init__(now)
            self.empty = empty

        def records(self, collection, **kwargs):
            if self.empty and collection in ("systems", "network_monitors"):
                return []
            rows = super().records(collection, **kwargs)
            if collection == "systems":
                rows.append({"id": "offline", "name": "offline", "status": "down"})
            elif collection == "network_monitors":
                rows.append({
                    "id": "disabled", "system": "offline",
                    "enabled": False, "interval": 600,
                })
            return rows

        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            if self.empty:
                assert wanted_ids == set()
                return {}
            return super().latest_by_relation(
                collection, relation_field, wanted_ids, **kwargs
            )

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    partial = exporter.BeszelCollector(
        api=PartialAPI(now), clock=lambda: now
    ).collect()
    assert history_coverage(partial) == {
        (metric, collection): float(value)
        for collection in ("system_stats", "container_stats", "network_monitor_stats")
        for metric, value in (("wanted", 1), ("found", 1))
    }
    empty = exporter.BeszelCollector(
        api=PartialAPI(now, empty=True), clock=lambda: now
    ).collect()
    assert "beszel_exporter_up 1" in empty
    assert history_coverage(empty) == {
        (metric, collection): 0.0
        for collection in ("system_stats", "container_stats", "network_monitor_stats")
        for metric in ("wanted", "found")
    }


def test_history_coverage_ignores_ineligible_found_rows_but_keeps_bulk_queries(monkeypatch):
    class AllRowsAPI(FakeAPI):
        def __init__(self, now):
            super().__init__(now)
            self.wanted_queries = {}

        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "systems":
                rows.extend([
                    {"id": "offline", "name": "offline", "status": "down"},
                    {"id": "paused", "name": "paused", "status": "paused"},
                ])
            elif collection == "network_monitors":
                rows.extend([
                    {"id": "disabled-up", "system": "sys1", "enabled": False},
                    {"id": "enabled-offline", "system": "offline", "enabled": True},
                    {"id": "enabled-paused", "system": "paused", "enabled": True},
                    {"id": "orphan", "system": "missing", "enabled": True},
                ])
            return rows

        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            self.wanted_queries[collection] = set(wanted_ids)
            rows = super().latest_by_relation(
                collection, relation_field, wanted_ids, **kwargs
            )
            if collection in ("system_stats", "container_stats"):
                # Offline and paused hosts may still have fresh historic rows.
                rows["offline"] = {"created": "2026-10-05T11:59:40Z"}
                rows["paused"] = {"created": "2026-10-05T11:59:40Z"}
            elif collection == "network_monitor_stats":
                rows.update({
                    id_: {"created": "2026-10-05T11:59:40Z"}
                    for id_ in ("disabled-up", "enabled-offline",
                                "enabled-paused", "orphan")
                })
            return rows

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    api = AllRowsAPI(now)
    output = exporter.BeszelCollector(api=api, clock=lambda: now).collect()
    assert api.wanted_queries == {
        "system_stats": {"sys1", "offline", "paused"},
        "container_stats": {"sys1", "offline", "paused"},
        "network_monitor_stats": {
            "mon1", "disabled-up", "enabled-offline", "enabled-paused", "orphan"
        },
    }
    assert history_coverage(output) == {
        (metric, collection): 1.0
        for metric in ("wanted", "found")
        for collection in ("system_stats", "container_stats", "network_monitor_stats")
    }


def test_history_coverage_all_offline_or_disabled_sources_are_zero(monkeypatch):
    class IneligibleAPI(FakeAPI):
        def __init__(self, now, mode):
            super().__init__(now)
            self.mode = mode

        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "systems" and self.mode == "offline":
                rows[0]["status"] = "down"
            elif collection == "network_monitors":
                if self.mode == "disabled":
                    rows[0]["enabled"] = False
                elif self.mode == "offline":
                    rows[0]["enabled"] = True
            return rows

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)

    offline = exporter.BeszelCollector(
        api=IneligibleAPI(now, "offline"), clock=lambda: now
    ).collect()
    assert history_coverage(offline) == {
        (metric, collection): 0.0
        for metric in ("wanted", "found")
        for collection in ("system_stats", "container_stats", "network_monitor_stats")
    }

    disabled = exporter.BeszelCollector(
        api=IneligibleAPI(now, "disabled"), clock=lambda: now
    ).collect()
    coverage = history_coverage(disabled)
    assert coverage[("wanted", "system_stats")] == 1
    assert coverage[("found", "system_stats")] == 1
    assert coverage[("wanted", "container_stats")] == 1
    assert coverage[("found", "container_stats")] == 1
    assert coverage[("wanted", "network_monitor_stats")] == 0
    assert coverage[("found", "network_monitor_stats")] == 0


def test_history_coverage_excludes_missing_active_history_not_inactive_sources(monkeypatch):
    class MissingActiveAPI(FakeAPI):
        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "systems":
                rows.extend([
                    {"id": "second-up", "name": "second-up", "status": "up"},
                    {"id": "offline", "name": "offline", "status": "down"},
                ])
            elif collection == "network_monitors":
                rows.extend([
                    {"id": "second-mon", "system": "second-up", "enabled": True},
                    {"id": "disabled", "system": "sys1", "enabled": False},
                ])
            return rows

        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            # Only the original system/monitor gets a row; other active
            # sources have none, while an offline/disabled one has history.
            rows = super().latest_by_relation(
                collection, relation_field, wanted_ids, **kwargs
            )
            rows["offline" if collection != "network_monitor_stats" else "disabled"] = {
                "created": "2026-10-05T11:59:40Z"
            }
            return rows

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    output = exporter.BeszelCollector(
        api=MissingActiveAPI(now), clock=lambda: now
    ).collect()
    assert history_coverage(output) == {
        (metric, collection): float(value)
        for collection in ("system_stats", "container_stats", "network_monitor_stats")
        for metric, value in (("wanted", 2), ("found", 1))
    }


def test_history_coverage_zero_found_on_optional_read_failure(monkeypatch):
    class FailingHistoryAPI(FakeAPI):
        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            if collection == "network_monitor_stats":
                raise RuntimeError("synthetic monitor-history query failure")
            return super().latest_by_relation(
                collection, relation_field, wanted_ids, **kwargs
            )

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    result = exporter.BeszelCollector(
        api=FailingHistoryAPI(now), clock=lambda: now
    ).collect()
    assert "beszel_exporter_up 1" in result
    coverage = history_coverage(result)
    assert coverage[("wanted", "network_monitor_stats")] == 1
    assert coverage[("found", "network_monitor_stats")] == 0
    assert coverage[("found", "system_stats")] == 1
    assert coverage[("found", "container_stats")] == 1
    assert (
        'beszel_exporter_collection_errors_total{collection="network_monitor_stats"} 1'
        in result
    )


def test_stale_stats_are_not_exported(monkeypatch):
    class FilteredStaleAPI(FakeAPI):
        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            # Model the created >= cutoff query implemented by PocketBase.
            rows = super().latest_by_relation(collection, relation_field, wanted_ids, **kwargs)
            return {
                key: record for key, record in rows.items()
                if exporter.parse_timestamp(record["created"]) >= kwargs["min_created"]
            }

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    collector = exporter.BeszelCollector(api=FilteredStaleAPI(now, stale=True), clock=lambda: now)
    text = collector.collect()
    assert "beszel_system_stats_age_seconds" not in text
    assert "beszel_container_stats_age_seconds" not in text
    assert "beszel_system_cpu_usage_percent" not in text
    assert "beszel_container_cpu_usage_percent" not in text
    assert "beszel_smart_temperature_celsius" not in text
    assert 'beszel_network_monitor_response_seconds{' not in text
    assert 'beszel_system_up{system="source-a",system_id="sys1"} 1' in text
    system_info = next(line for line in text.splitlines() if line.startswith("beszel_system_info{"))
    assert "status=" not in system_info


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


def test_package_update_counts_have_distinct_metric_names(monkeypatch):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    text = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now).collect()
    assert 'beszel_package_updates_pending{system="source-a",system_id="sys1"} 12' in text
    assert 'beszel_package_security_updates_pending{system="source-a",system_id="sys1"} 3' in text
    assert 'beszel_package_updates_pending{system="source-a",system_id="sys1",type=' not in text


@pytest.mark.parametrize(
    ("age_seconds", "should_emit"),
    [
        (30, True),
        (600, True),     # Normal 10-minute snapshot interval.
        (1199, True),
        (1200, True),    # Two-cycle boundary is inclusive.
        (1201, False),
        (3600, False),
        (None, False),   # Older agent without a systemd snapshot.
    ],
)
def test_systemd_service_updated_controls_runtime_metrics_on_fresh_host(
    age_seconds, should_emit
):
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    service = {
        "name": "docker.service", "state": 0, "sub": 1,
        "cpu": 5, "cpuPeak": 10, "memory": 100, "memPeak": 200,
    }
    if age_seconds is not None:
        service["updated"] = int((now - age_seconds) * 1000)
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_systemd(
        metrics, {"system": "source-a", "system_id": "sys1"}, [service],
        emit_dynamic=True, now=now,
    )
    info = next(
        line for line in metrics.render().splitlines()
        if line.startswith("beszel_systemd_service_info{")
    )
    assert 'service="docker.service"' in info
    numeric_families = set(metrics.families) - {"beszel_systemd_service_info"}
    if should_emit:
        assert 'state="active"' in info
        assert 'substate="running"' in info
        assert numeric_families == {
            "beszel_systemd_service_active",
            "beszel_systemd_service_failed",
            "beszel_systemd_service_cpu_usage_percent",
            "beszel_systemd_service_cpu_peak_percent",
            "beszel_systemd_service_memory_bytes",
            "beszel_systemd_service_memory_peak_bytes",
        }
    else:
        assert "state=" not in info
        assert "substate=" not in info
        assert not numeric_families


def test_stale_systemd_row_hidden_with_fresh_host_and_fresh_stats(monkeypatch):
    class StaleSystemdAPI(FakeAPI):
        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "systemd_services":
                rows[0]["updated"] = int((self.now - 3600) * 1000)
            return rows

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    rendered = exporter.BeszelCollector(
        api=StaleSystemdAPI(now), clock=lambda: now
    ).collect()
    assert "beszel_exporter_up 1" in rendered
    assert "beszel_system_cpu_usage_percent" in rendered
    info = next(
        line for line in rendered.splitlines()
        if line.startswith("beszel_systemd_service_info{")
    )
    assert 'service="docker.service"' in info
    assert 'state="' not in info
    assert 'substate="' not in info
    assert 'beszel_systemd_service_active{' not in rendered
    assert 'beszel_systemd_service_cpu_usage_percent{' not in rendered


def test_mock_systemd_service_count_is_gauge_with_valid_name(monkeypatch):
    # The same FakeAPI also drives tests/render_mock_metrics.py and promtool CI.
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    text = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now).collect()
    assert "# TYPE beszel_systemd_services gauge" in text
    assert 'beszel_systemd_services{system="source-a",system_id="sys1"} 42' in text
    assert 'beszel_systemd_services_failed{system="source-a",system_id="sys1"} 1' in text
    assert "beszel_systemd_services_total" not in text


def test_systemd_info_unknown_numeric_codes_are_bounded():
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_systemd(
        metrics,
        {"system": "source-a", "system_id": "sys1"},
        [{
            "name": "x.service", "state": 12345, "sub": 98765,
            "updated": int((now - 60) * 1000),
        }],
        emit_dynamic=True,
        now=now,
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
                rows.append({
                    "id": "cid-other", "system": "sys1", "name": "app",
                    "cpu": 99, "memory": 20,
                    "updated": int((self.now - 30) * 1000),
                })
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
                rows.append({
                    "id": "other", "system": "sys1", "name": "app",
                    "cpu": 123, "updated": int((self.now - 30) * 1000),
                })
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


class BudgetResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.data


def make_jwt(exp):
    """Test-only unsigned JWT: only the unverified exp claim is consumed."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "sub": "example"}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("invalid", None),
        ("header.e30.signature", None),
        ("header.###.signature", None),
        (make_jwt(None), None),
        (make_jwt("1780000000"), None),
        (make_jwt(True), None),
        (make_jwt(1800000000), 1800000000.0),
    ],
)
def test_jwt_expiration_parsing_is_strict(token, expected):
    assert exporter.jwt_expires_at(token) == expected


def test_password_token_renews_before_expiry(monkeypatch):
    monkeypatch.setattr(exporter, "BESZEL_TOKEN", "")
    monkeypatch.setattr(exporter, "BESZEL_USER", "user")
    monkeypatch.setattr(exporter, "BESZEL_PASSWORD", "secret")
    session_token = make_jwt(time.time() + 3600)
    refreshed_token = make_jwt(time.time() + 7200)

    class MockSession:
        def __init__(self):
            self.headers = {}
            self.posts = 0
            self.gets = 0

        def post(self, url, json, timeout):
            self.posts += 1
            return BudgetResponse({"token": refreshed_token})

        def get(self, url, params, timeout):
            self.gets += 1
            return BudgetResponse({"items": [{"id": "host"}]})

    api = exporter.BeszelAPI()
    api.session = MockSession()
    api.token = session_token
    api.session.headers["Authorization"] = session_token
    # A valid token just outside the refresh window is reused.
    assert api.get("/test")["items"]
    assert api.session.posts == 0
    api.token = make_jwt(time.time() + 590)
    api.session.headers["Authorization"] = api.token
    assert api.get("/test")["items"]
    assert api.session.posts == 1
    assert api.token == refreshed_token
    assert api.get("/test")["items"]
    assert api.session.posts == 1
    assert api.session.gets == 3


@pytest.mark.parametrize("static_token", [
    "not-a-jwt",
    make_jwt(1000),
    make_jwt(time.time() + 590),
])
def test_invalid_or_near_expiry_static_token_fails_without_http(monkeypatch, static_token):
    monkeypatch.setattr(exporter, "BESZEL_TOKEN", static_token)

    class NoRequests:
        headers = {}

        def get(self, *args, **kwargs):
            raise AssertionError("must reject static token before HTTP")

        def post(self, *args, **kwargs):
            raise AssertionError("static token must never password-login")

    api = exporter.BeszelAPI()
    api.session = NoRequests()
    with pytest.raises(RuntimeError, match="BESZEL_TOKEN"):
        api.get("/api/collections/systems/records")

    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    out = exporter.BeszelCollector(api=api).collect()
    assert "beszel_exporter_up 0" in out
    assert "beszel_exporter_systems" not in out


def test_valid_static_token_does_not_password_login_or_retry_empty_systems(monkeypatch):
    monkeypatch.setattr(exporter, "BESZEL_TOKEN", make_jwt(time.time() + 3600))

    class EmptySession:
        def __init__(self):
            self.headers = {}
            self.gets = 0

        def post(self, *args, **kwargs):
            raise AssertionError("static token must not password-login")

        def get(self, url, params, timeout):
            self.gets += 1
            return BudgetResponse({"items": [], "totalPages": 1})

    api = exporter.BeszelAPI()
    api.session = EmptySession()
    assert api.records("systems") == []
    assert api.session.gets == 1


def test_empty_systems_relogin_retries_once_then_returns_hosts(monkeypatch):
    monkeypatch.setattr(exporter, "BESZEL_TOKEN", "")
    monkeypatch.setattr(exporter, "BESZEL_USER", "user")
    monkeypatch.setattr(exporter, "BESZEL_PASSWORD", "secret")

    class EmptyThenFound:
        def __init__(self):
            self.headers = {}
            self.posts = 0
            self.gets = 0

        def post(self, url, json, timeout):
            self.posts += 1
            return BudgetResponse({"token": make_jwt(time.time() + 3600)})

        def get(self, url, params, timeout):
            self.gets += 1
            if self.gets == 1:
                return BudgetResponse({"items": [], "totalPages": 1})
            return BudgetResponse({"items": [{"id": "sys1"}], "totalPages": 1})

    api = exporter.BeszelAPI()
    api.session = EmptyThenFound()
    rows = api.records("systems")
    assert [row["id"] for row in rows] == ["sys1"]
    assert api.session.posts == 2
    assert api.session.gets == 2


def test_empty_systems_after_password_relogin_visible_as_zero(monkeypatch, caplog):
    monkeypatch.setattr(exporter, "BESZEL_TOKEN", "")
    monkeypatch.setattr(exporter, "BESZEL_USER", "user")
    monkeypatch.setattr(exporter, "BESZEL_PASSWORD", "secret")
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)

    class AlwaysEmpty:
        def __init__(self):
            self.headers = {}
            self.posts = 0
            self.gets = 0
            self.systems_gets = 0

        def post(self, url, json, timeout):
            self.posts += 1
            return BudgetResponse({"token": make_jwt(time.time() + 3600)})

        def get(self, url, params, timeout):
            self.gets += 1
            if url.endswith("/api/collections/systems/records"):
                self.systems_gets += 1
            return BudgetResponse({"items": [], "totalPages": 1})

    api = exporter.BeszelAPI()
    api.session = AlwaysEmpty()
    out = exporter.BeszelCollector(api=api).collect()
    assert "beszel_exporter_systems 0" in out
    assert "beszel_exporter_up 1" in out
    assert "empty after a fresh password login" in caplog.text
    assert api.session.systems_gets == 2
    assert api.session.posts == 2


def test_full_collector_exports_visible_systems_count(monkeypatch):
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    out = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now).collect()
    assert "# TYPE beszel_exporter_systems gauge" in out
    assert "beszel_exporter_systems 1" in out


def test_http_requests_share_one_budget_across_auth_and_401_retry(monkeypatch):
    tick = [100.0]
    monkeypatch.setattr(exporter.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(exporter, "REQUEST_TIMEOUT", 10)
    monkeypatch.setattr(exporter, "BESZEL_USER", "user")
    monkeypatch.setattr(exporter, "BESZEL_PASSWORD", "secret")
    monkeypatch.setattr(exporter, "BESZEL_TOKEN", "")

    class MockSession:
        def __init__(self):
            self.headers = {}
            self.calls = []
            self.gets = 0

        def post(self, url, json, timeout):
            self.calls.append(("post", timeout))
            tick[0] += 1
            return BudgetResponse({"token": make_jwt(time.time() + 3600)})

        def get(self, url, params, timeout):
            self.calls.append(("get", timeout))
            self.gets += 1
            tick[0] += 1
            return BudgetResponse({"items": []}, 401 if self.gets == 1 else 200)

    api = exporter.BeszelAPI()
    api.session = MockSession()
    token = exporter._SCRAPE_DEADLINE.set(106.0)
    try:
        assert api.get("/test") == {"items": []}
        assert api.session.calls == [
            ("post", 6.0), ("get", 5.0), ("post", 4.0), ("get", 3.0),
        ]
        tick[0] = 106.0
        with pytest.raises(exporter.ScrapeDeadlineExceeded):
            api.get("/test")
        assert len(api.session.calls) == 4
    finally:
        exporter._SCRAPE_DEADLINE.reset(token)


@pytest.mark.parametrize("num_rows,expected_pages", [(5, 3), (4, 3), (1, 1)])
def test_records_pagination_uses_skip_total_and_short_page(num_rows, expected_pages):
    class Pages(exporter.BeszelAPI):
        def __init__(self):
            self.calls = []

        def get(self, path, params=None):
            self.calls.append((path, params))
            assert params["skipTotal"] == 1
            assert params["perPage"] == 2
            start = (params["page"] - 1) * 2
            return {
                "items": [{"id": str(n)} for n in range(start, min(start + 2, num_rows))],
                "totalPages": 9999,  # Must be ignored in skipTotal mode.
            }

    api = Pages()
    rows = api.records(
        "containers", per_page=2, fields="id", filter_expr='status="up"',
        sort="-created",
    )
    assert [r["id"] for r in rows] == [str(i) for i in range(num_rows)]
    assert len(api.calls) == expected_pages
    assert all(path == "/api/collections/containers/records" for path, _ in api.calls)
    assert all(p["fields"] == "id" for _, p in api.calls)
    assert all(p["filter"] == 'status="up"' for _, p in api.calls)
    assert all(p["sort"] == "-created" for _, p in api.calls)


@pytest.mark.parametrize(
    ("collection", "created_format", "numeric_expected"),
    [
        ("system_stats", "pocketbase", False),
        ("network_monitor_stats", "unix_ms", True),
        ("other_collection", "unix_ms", True),
        ("network_monitor_stats", "pocketbase", False),
    ],
)
def test_history_created_format_is_explicit_not_collection_specific(
    collection, created_format, numeric_expected
):
    class Captured(exporter.BeszelAPI):
        def __init__(self):
            self.params = None

        def get(self, path, params=None):
            self.params = params
            return {"items": [{"system": "id", "created": "2026-10-05T12:00:00Z"}]}

    api = Captured()
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    assert "id" in api.latest_by_relation(
        collection, "system", {"id"}, fields="system,created,type",
        min_created=now - 600, created_format=created_format,
    )
    predicate = api.params["filter"]
    assert ('created >= "' not in predicate) == numeric_expected
    if numeric_expected:
        assert f"created >= {int((now - 600) * 1000)}" in predicate


def test_history_rejects_unsupported_created_format():
    api = exporter.BeszelAPI()
    with pytest.raises(ValueError, match="created_format"):
        api.latest_by_relation(
            "system_stats", "system", {"id"}, fields="system,created",
            created_format="unknown",
        )


def test_records_and_bulk_pagination_stop_on_shared_deadline(monkeypatch):
    tick = [100.0]
    monkeypatch.setattr(exporter.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(exporter, "BULK_PAGE_SIZE", 1)

    class SlowPages(exporter.BeszelAPI):
        def __init__(self):
            self.calls = 0

        def get(self, path, params=None):
            self.calls += 1
            tick[0] += 3
            return {"items": [{"system": "another"}], "totalPages": 100}

    api = SlowPages()
    token = exporter._SCRAPE_DEADLINE.set(105.0)
    try:
        with pytest.raises(exporter.ScrapeDeadlineExceeded):
            api.records("systems", per_page=1)
        assert api.calls == 2
        api.calls = 0
        tick[0] = 100.0
        with pytest.raises(exporter.ScrapeDeadlineExceeded):
            api.latest_by_relation(
                "system_stats", "system", {"offline"},
                fields="system,stats,created,type",
            )
        assert api.calls == 2
    finally:
        exporter._SCRAPE_DEADLINE.reset(token)


def test_slow_optional_collection_aborts_without_partial_success(monkeypatch):
    tick = [100.0]
    monkeypatch.setattr(exporter.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(exporter, "SCRAPE_BUDGET_SECONDS", 5)
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)

    class SlowOptional(FakeAPI):
        def __init__(self, now):
            super().__init__(now)
            self.slow = False
            self.calls = []

        def records(self, collection, **kwargs):
            self.calls.append(collection)
            if self.slow and collection == "containers":
                tick[0] += 6
            return super().records(collection, **kwargs)

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    api = SlowOptional(now)
    collector = exporter.BeszelCollector(api=api, clock=lambda: now)
    good = collector.collect()
    assert "beszel_exporter_up 1" in good
    assert "beszel_system_cpu_usage_percent" in good
    api.slow = True
    api.calls.clear()
    failed = collector.collect()
    assert "beszel_exporter_up 0" in failed
    assert "beszel_system_info" in failed
    assert "beszel_system_cpu_usage_percent" not in failed
    assert "beszel_system_up" not in failed
    assert "smart_devices" not in api.calls
    assert exporter._SCRAPE_DEADLINE.get() is None


def test_lock_wait_is_bounded_by_total_scrape_budget(monkeypatch):
    monkeypatch.setattr(exporter, "SCRAPE_BUDGET_SECONDS", 0.01)
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    collector = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now)
    assert collector.lock.acquire(blocking=False)
    try:
        started = exporter.time.monotonic()
        response = collector.collect()
        assert exporter.time.monotonic() - started < 0.5
        assert "beszel_exporter_up 0" in response
        assert "beszel_system_info" not in response
        assert "beszel_exporter_scrape_duration_seconds" in response
    finally:
        collector.lock.release()



def test_identity_only_render_deduplicates_collapsed_dynamic_states():
    metrics = exporter.PrometheusText()
    base = {"system": "alpha", "system_id": "sys1", "service": "sshd"}
    metrics.info("beszel_systemd_service_info", {**base, "state": "active"})
    metrics.info("beszel_systemd_service_info", {**base, "state": "failed"})
    assert len(metrics.families["beszel_systemd_service_info"]["samples"]) == 2
    filtered = metrics.render(identity_only=True)
    assert len([
        line for line in filtered.splitlines()
        if line.startswith("beszel_systemd_service_info{")
    ]) == 1
    assert "state=" not in filtered


def test_identity_only_render_strips_operational_labels_and_state_only_families():
    metrics = exporter.PrometheusText()
    sys = {"system": "alpha", "system_id": "sys1"}
    metrics.info("beszel_system_info", {**sys, "status": "up", "hostname": "alpha", "kernel": "6.0"})
    metrics.info(
        "beszel_container_info",
        {**sys, "container": "app", "container_id": "cid", "status": "running",
         "image": "image:v1", "ports": "8080"},
    )
    metrics.info(
        "beszel_smart_device_info",
        {**sys, "device": "/dev/sda", "serial": "disk1", "state": "FAILED", "model": "Disk"},
    )
    metrics.info(
        "beszel_systemd_service_info",
        {**sys, "service": "sshd", "state": "failed", "substate": "dead"},
    )
    metrics.info(
        "beszel_storage_pool_info",
        {**sys, "pool": "tank", "pool_type": "zfs", "health": "DEGRADED"},
    )
    metrics.info(
        "beszel_storage_pool_vdev_info",
        {**sys, "pool": "tank", "vdev": "sda", "state": "FAULTED"},
    )
    metrics.info("beszel_storage_pool_health_info", {**sys, "pool": "tank", "health": "DEGRADED"})
    metrics.info("beszel_storage_pool_scrub_info", {**sys, "pool": "tank", "state": "running"})
    metrics.info("beszel_unknown_future_info", {**sys, "state": "BAD"})
    metrics.add("beszel_system_cpu_usage_percent", 100, sys)

    full = metrics.render()
    assert 'state="failed"' in full and 'status="up"' in full
    assert 'health="DEGRADED"' in full
    fallback = metrics.render(identity_only=True)
    assert 'beszel_system_info{hostname="alpha",system="alpha",system_id="sys1"} 1' in fallback
    assert "beszel_container_info{" in fallback
    assert "beszel_smart_device_info{" in fallback
    assert "beszel_systemd_service_info{" in fallback
    assert "beszel_storage_pool_info{" in fallback
    assert "beszel_storage_pool_vdev_info{" in fallback
    for name in (
        "beszel_storage_pool_health_info",
        "beszel_storage_pool_scrub_info",
        "beszel_unknown_future_info",
        "beszel_system_cpu_usage_percent",
    ):
        assert name not in fallback
    for value in ('state="', 'status="', 'health="', 'substate="', 'ports="',
                  'image="', 'model="', 'kernel="'):
        assert value not in fallback


def test_older_operational_info_not_replayed_when_hub_fails(monkeypatch):
    class StateAPI(FakeAPI):
        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "containers":
                rows[0]["status"] = "Up 1 hour"
                rows[0]["ports"] = "443/tcp"
                rows[0]["image"] = "app:v1"
            if collection == "smart_devices":
                rows[0]["state"] = "PASSED"
            if collection == "zfs_pools":
                return [{
                    "system": "sys1", "name": "tank", "health": "ONLINE",
                    "scrub": {"state": "running", "progress": "45%"},
                    "vdevs": [{"name": "sda", "state": "ONLINE"}],
                }]
            return rows

        def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
            rows = super().latest_by_relation(collection, relation_field, wanted_ids, **kwargs)
            if collection == "system_stats":
                rows["sys1"]["stats"]["z"] = {
                    "tank": {"d": 10, "du": 5, "h": "ONLINE"}
                }
            return rows

    now = [datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    api = StateAPI(now[0])
    collector = exporter.BeszelCollector(api=api, clock=lambda: now[0])
    good = collector.collect()
    assert "beszel_exporter_up 1" in good
    assert 'state="running"' in good
    assert 'status="up"' in good
    assert "beszel_storage_pool_health_info" in good
    assert "beszel_storage_pool_scrub_info" in good

    api.fail_systems = True
    now[0] += 1
    failed = collector.collect()
    assert "beszel_exporter_up 0" in failed
    assert "beszel_system_info" in failed
    assert "beszel_container_info" in failed
    assert "beszel_smart_device_info" in failed
    assert "beszel_storage_pool_info" in failed
    assert "beszel_storage_pool_vdev_info" in failed
    assert "beszel_storage_pool_health_info" not in failed
    assert "beszel_storage_pool_scrub_info" not in failed
    for dynamic_label in ('state="', 'status="', 'substate="', 'health="', 'ports="'):
        assert dynamic_label not in failed
    assert "beszel_system_up" not in failed


def test_unfresh_system_does_not_expose_saved_runtime_states():
    m = exporter.PrometheusText()
    labels = {"system": "alpha", "system_id": "sys1"}
    exporter.BeszelCollector.emit_containers(
        m, labels, [{"name": "app", "status": "Up 4 hours"}], [],
        emit_dynamic=False, now=0.0
    )
    exporter.BeszelCollector.emit_smart(
        m, labels, [{"name": "/dev/sda", "serial": "d", "state": "FAILED"}],
        emit_dynamic=False,
    )
    exporter.BeszelCollector.emit_systemd(
        m, labels, [{"name": "x.service", "state": 2, "sub": 3}],
        emit_dynamic=False, now=0.0
    )
    exporter.BeszelCollector.emit_storage_pools(
        m, labels, [{"name": "tank", "health": "DEGRADED"}], emit_dynamic=False
    )
    rendered = m.render()
    for family in ("beszel_container_info", "beszel_smart_device_info",
                   "beszel_systemd_service_info", "beszel_storage_pool_info"):
        assert family in rendered
    for field in ('status="', 'state="', 'health="', 'substate="'):
        assert field not in rendered


def test_duplicate_warning_once_per_metric_process_not_per_scrape(monkeypatch, caplog):
    metric = "test_process_wide_warning_unique_family"
    # Isolate this metric family without disabling the shared warning registry.
    with exporter._LOGGED_DUPLICATE_METRICS_LOCK:
        exporter._LOGGED_DUPLICATE_METRICS.discard(metric)
    try:
        dropped = []
        for _ in range(3):
            metrics = exporter.PrometheusText(on_duplicate=dropped.append)
            metrics.add(metric, 1, {"system": "a"})
            metrics.add(metric, 2, {"system": "a"})
        assert dropped == [metric] * 3
        warnings = [
            rec for rec in caplog.records
            if "Dropping duplicate Prometheus sample" in rec.message and metric in rec.message
        ]
        assert len(warnings) == 1
    finally:
        with exporter._LOGGED_DUPLICATE_METRICS_LOCK:
            exporter._LOGGED_DUPLICATE_METRICS.discard(metric)


def test_stale_container_info_omits_ports_but_preserves_identity():
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    labels = {"system": "host-a", "system_id": "sys1"}
    container = {
        "id": "cid",
        "name": "app",
        "status": "Up 3 hours",
        "ports": "8080/tcp",
        "image": "example/app:v2",
        "cpu": 20,
        "updated": int((now - 30) * 1000),
    }
    fresh = exporter.PrometheusText()
    exporter.BeszelCollector.emit_containers(
        fresh, labels, [container], [], emit_dynamic=True, now=now
    )
    fresh_info = next(
        line for line in fresh.render().splitlines()
        if line.startswith("beszel_container_info{")
    )
    assert 'ports="8080/tcp"' in fresh_info
    assert 'status="running"' in fresh_info

    stale = exporter.PrometheusText()
    exporter.BeszelCollector.emit_containers(
        stale, labels, [container], [], emit_dynamic=False, now=now
    )
    stale_info = next(
        line for line in stale.render().splitlines()
        if line.startswith("beszel_container_info{")
    )
    assert 'container="app"' in stale_info
    assert 'container_id="cid"' in stale_info
    assert "ports=" not in stale_info
    assert "status=" not in stale_info
    assert "beszel_container_cpu_usage_percent" not in stale.render()


@pytest.mark.parametrize(
    ("updated_age", "expected_fresh"),
    [
        (30, True),
        (180, True),  # Inclusive freshness boundary
        (181, False),
        (600, False),  # Removed 10 minutes ago, host still fresh
        (None, False),  # Older agents missing per-container updated
    ],
)
def test_container_updated_controls_all_dynamic_series(updated_age, expected_fresh):
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    container = {
        "id": "cid", "name": "app", "image": "example/app:v2",
        "status": "Up 3 hours", "ports": "8080/tcp",
        "cpu": 20, "memory": 256, "net": 1000,
        "health": 1, "updatable": True,
    }
    if updated_age is not None:
        container["updated"] = int((now - updated_age) * 1000)
    metrics = exporter.PrometheusText()
    exporter.BeszelCollector.emit_containers(
        metrics,
        {"system": "source-a", "system_id": "sys1"},
        [container],
        [{"n": "app", "b": [100, 200]}],
        emit_dynamic=True,
        now=now,
    )
    info = next(
        line for line in metrics.render().splitlines()
        if line.startswith("beszel_container_info{")
    )
    assert 'container="app"' in info
    assert 'container_id="cid"' in info
    assert 'image="example/app:v2"' in info
    operational = set(metrics.families) - {"beszel_container_info"}
    if expected_fresh:
        assert 'status="running"' in info
        assert 'ports="8080/tcp"' in info
        assert operational == {
            "beszel_container_cpu_usage_percent",
            "beszel_container_memory_used_bytes",
            "beszel_container_network_combined_bytes_per_second",
            "beszel_container_health_state",
            "beszel_container_update_available",
            "beszel_container_network_bytes_per_second",
        }
    else:
        assert "status=" not in info
        assert "ports=" not in info
        assert operational == set()


def test_stale_container_row_on_fresh_host_exposes_only_identity_info(monkeypatch):
    class StaleContainerAPI(FakeAPI):
        def records(self, collection, **kwargs):
            rows = super().records(collection, **kwargs)
            if collection == "containers":
                rows[0].update({
                    "updated": int((self.now - 600) * 1000),
                    "status": "Up 3 hours",
                    "ports": "8080/tcp",
                    "health": 1,
                    "updatable": True,
                })
            return rows

    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(exporter, "CACHE_TTL", 0)
    output = exporter.BeszelCollector(
        api=StaleContainerAPI(now), clock=lambda: now
    ).collect()
    assert "beszel_exporter_up 1" in output
    assert "beszel_system_cpu_usage_percent" in output  # Host is fresh.
    info = next(
        line for line in output.splitlines()
        if line.startswith("beszel_container_info{")
    )
    assert 'container="app"' in info
    assert 'container_id="cid"' in info
    assert "status=" not in info
    assert "ports=" not in info
    assert not any(
        line.startswith("beszel_container_")
        and not line.startswith((
            "beszel_container_info",
            "beszel_container_stats_age_seconds",
        ))
        for line in output.splitlines() if not line.startswith("#")
    )


def test_duplicate_warning_includes_first_system_id_debug_identifies_other_hosts(caplog):
    import logging

    name = "test_duplicate_diagnostics_first_system_id"
    with exporter._LOGGED_DUPLICATE_METRICS_LOCK:
        exporter._LOGGED_DUPLICATE_METRICS.discard(name)
    try:
        with caplog.at_level(logging.DEBUG, logger="beszel_exporter"):
            dropped = []
            for host in ("sys1", "sys2"):
                metrics = exporter.PrometheusText(on_duplicate=dropped.append)
                labels = {
                    "system_id": host,
                    "system": "private-host",
                    "container": "secret-container",
                }
                metrics.add(name, 1, labels)
                metrics.add(name, 2, labels)
            assert dropped == [name, name]

        warnings = [
            record.message for record in caplog.records
            if record.levelno == logging.WARNING and name in record.message
        ]
        assert len(warnings) == 1
        assert "system_id=sys1" in warnings[0]
        debugs = [
            record.message for record in caplog.records
            if record.levelno == logging.DEBUG and name in record.message
        ]
        assert len(debugs) == 2
        assert any("system_id=sys1" in message for message in debugs)
        assert any("system_id=sys2" in message for message in debugs)
        assert "private-host" not in caplog.text
        assert "secret-container" not in caplog.text
    finally:
        with exporter._LOGGED_DUPLICATE_METRICS_LOCK:
            exporter._LOGGED_DUPLICATE_METRICS.discard(name)
