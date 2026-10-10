"""Keep promtool's synthetic exposition in sync with every exported metric.

A new family must be reachable with a non-legacy fixture, not merely mentioned
in a unit test. Discover literals in the implementation and expand the dynamic
disk I/O prefix+suffix names that a naive literal scan would otherwise miss.
"""

import re
from pathlib import Path

from metric_coverage_fixture import render_full_mock


# Intentionally excluded from normal-mode synthetic metrics. Explicit list
# rather than blanket suffix patterns, so new legacy names need a review.
LEGACY_ONLY_METRICS = {
    "beszel_filesystem_read_mib_per_second",
    "beszel_filesystem_read_peak_mib_per_second",
    "beszel_filesystem_write_mib_per_second",
    "beszel_filesystem_write_peak_mib_per_second",
    "beszel_smart_power_on_hours_total",
    "beszel_system_disk_read_mib_per_second",
    "beszel_system_disk_read_peak_mib_per_second",
    "beszel_system_disk_write_mib_per_second",
    "beszel_system_disk_write_peak_mib_per_second",
    "beszel_system_network_received_mib_per_second",
    "beszel_system_network_received_peak_mib_per_second",
    "beszel_system_network_sent_mib_per_second",
    "beszel_system_network_sent_peak_mib_per_second",
    "beszel_filesystem_peak_read_await_milliseconds",
    "beszel_filesystem_peak_write_await_milliseconds",
    "beszel_filesystem_read_await_milliseconds",
    "beszel_filesystem_write_await_milliseconds",
    "beszel_system_disk_peak_read_await_milliseconds",
    "beszel_system_disk_peak_write_await_milliseconds",
    "beszel_system_disk_read_await_milliseconds",
    "beszel_system_disk_write_await_milliseconds",
}

# Some literal beszel_ strings are logger names or dynamic family prefixes.
NON_METRIC_LITERALS = {"beszel_exporter", "beszel_scrape_deadline"}

DISK_IO_SUFFIXES = {
    "read_time_percent",
    "write_time_percent",
    "utilization_percent",
    "read_await_seconds",
    "write_await_seconds",
    "weighted_io_percent",
    "read_await_milliseconds",  # LEGACY_UNITS only
    "write_await_milliseconds",  # LEGACY_UNITS only
}


def expected_metric_names():
    source = (Path(__file__).resolve().parent.parent / "beszel_exporter.py").read_text()
    literal_names = set(re.findall(r'["\\'](beszel_[a-z0-9_]+)["\\']', source))
    prefixes = set(re.findall(r'prefix="(beszel_[a-z0-9_]+)"', source))
    assert prefixes == {
        "beszel_system_disk",
        "beszel_system_disk_peak",
        "beszel_filesystem",
        "beszel_filesystem_peak",
    }, "Disk I/O prefix mapping changed: update fixture coverage contract"
    discovered = (
        literal_names - NON_METRIC_LITERALS - prefixes
        | {f"{prefix}_{suffix}" for prefix in prefixes for suffix in DISK_IO_SUFFIXES}
    )
    assert LEGACY_ONLY_METRICS <= discovered, "Legacy allowlist has stale entries"
    return discovered - LEGACY_ONLY_METRICS


def test_promtool_fixture_covers_every_standard_exported_metric_family():
    exposition = render_full_mock()
    actual = set(re.findall(r"^# TYPE (beszel_[a-z0-9_]+) ", exposition, flags=re.M))
    expected = expected_metric_names()
    missing = sorted(expected - actual)
    assert not missing, (
        "Promtool fixture missing emitted families (update metric_coverage_fixture.py): "
        + ", ".join(missing)
    )
    assert not (actual & LEGACY_ONLY_METRICS), "Legacy metrics must not appear by default"
    assert "beszel_package_security_updates_pending" in actual
    assert "beszel_storage_pool_scrub_info" in actual
    assert "beszel_network_monitor_tls_cert_info" in actual
