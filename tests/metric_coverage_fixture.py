"""Full-mapping synthetic Beszel data for promtool and metric coverage checks.

The normal FakeAPI remains deliberately small for focused collector regression
tests. This richer subclass drives all standard (non-legacy) metric families.
"""

from datetime import datetime, timezone

import beszel_exporter as exporter
from test_exporter import FakeAPI


class FullMetricAPI(FakeAPI):
    def __init__(self, now):
        super().__init__(now)
        self.fail_details_once = True

    def records(self, collection, **kwargs):
        if collection == "system_details" and self.fail_details_once:
            # Exercise the optional collection-error counter in a real scrape.
            self.fail_details_once = False
            raise RuntimeError("synthetic optional collection failure")

        rows = super().records(collection, **kwargs)
        if collection == "systems":
            for row in rows:
                row["info"].update({
                    "v": "0.21.0", "u": 1234, "dt": 42, "ct": 1,
                    "jl": True, "pu": [12, 3], "sv": [42, 1],
                })
        elif collection == "system_details":
            for row in rows:
                row.update({
                    "cpu": "Example CPU", "kernel": "6.1", "os_name": "Linux",
                    "memory": 16_000_000_000, "podman": False,
                })
        elif collection == "containers":
            for row in rows:
                row.update({
                    "image": "example/app:v1", "ports": "8080/tcp",
                    "status": "Up 2 minutes", "health": 1,
                    "updatable": True,
                })
        elif collection == "smart_devices":
            for row in rows:
                row.update({
                    "hours": 15, "cycles": 5, "type": "hdd",
                    "attributes": [{
                        "id": 194, "n": "Temperature", "v": 98,
                        "w": 95, "t": 30, "rv": 35,
                    }],
                })
        elif collection == "systemd_services":
            for row in rows:
                row.update({
                    "cpu": 1.5, "cpuPeak": 2.5,
                    "memory": 5000, "memPeak": 7000,
                })
        elif collection == "network_monitors":
            for row in rows:
                row.update({
                    "resAvg1h": 9000, "resMin1h": 7000,
                    "resMax1h": 15_000, "loss1h": 12,
                    "certInfo": {
                        "expires": 2_000_000_000_000, "issuer": "Synthetic CA",
                    },
                })
        elif collection == "zfs_pools":
            rows = [{
                "id": "pool1", "system": "sys1", "name": "tank",
                "display_name": "Tank", "health": "ONLINE",
                "size": 10_000, "alloc": 6000, "free": 4000,
                "scrub": {"state": "running", "progress": "40%", "errors": 1},
                "vdevs": [{
                    "name": "sda", "state": "ONLINE", "readErrs": 0,
                    "writeErrs": 1, "checksumErrs": 2,
                }],
                "datasets": [{
                    "name": "tank/ds", "mount": "/data",
                    "used": 300, "avail": 800,
                }],
            }]
        return rows

    def latest_by_relation(self, collection, relation_field, wanted_ids, **kwargs):
        rows = super().latest_by_relation(
            collection, relation_field, wanted_ids, **kwargs
        )
        if collection == "system_stats" and "sys1" in rows:
            stats = rows["sys1"]["stats"]
            stats.update({
                "cpum": 65, "mm": 1.8, "mb": 0.4, "mz": 0.3,
                "s": 4, "su": 0.1,
                "la": [0.5, 0.3, 0.1],
                "b": [500, 600], "bm": [900, 1000],
                "dio": [300, 400], "diom": [500, 600],
                "diot": [10000, 12000],
                "cpub": [20, 10, 1, 0, 69], "cpus": [11, 22],
                "t": {"cpu": 51}, "f": {"fan0": 1200},
                "bat": [80, 1],
                "ni": {"eth0": [100, 200, 5000, 6000]},
                "diosm": [2, 4, 5, 6, 7, 8],
                "g": {
                    "0": {
                        "n": "Example GPU", "u": 80, "mu": 100,
                        "mt": 200, "p": 70, "pp": 85,
                        "e": {"graphics": 50},
                    }
                },
                "z": {
                    "tank": {
                        "d": 10, "du": 7, "rb": 123,
                        "wb": 456, "h": "ONLINE",
                    }
                },
            })
            stats["efs"]["/data"].update({
                "rb": 10, "wb": 20, "rbm": 30, "wbm": 40,
                "tr": 10_000, "tw": 20_000,
                "dios": [1, 2, 3, 4, 5, 6],
                "diosm": [6, 5, 4, 3, 2, 1],
            })
        elif collection == "container_stats" and "sys1" in rows:
            rows["sys1"]["stats"][0]["b"] = [40, 60]
        elif collection == "network_monitor_stats" and "mon1" in rows:
            # Both buckets and packet loss must be present in rendered output.
            rows["mon1"]["total_count"] = 10
            rows["mon1"]["success_count"] = 7
            rows["mon1"]["res_sum"] = 70_000
        return rows


def render_full_mock():
    """Render a complete, fresh scrape with one earlier optional read failure."""
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    collector = exporter.BeszelCollector(api=FullMetricAPI(now), clock=lambda: now)
    collector.collect()  # Seed collection_errors_total with a realistic failure.
    collector.cache_ttl = 0
    return collector.collect()
