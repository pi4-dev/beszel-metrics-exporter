from datetime import datetime, timezone

import beszel_exporter as exporter
from test_exporter import FakeAPI

now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
exporter.CACHE_TTL = 0
collector = exporter.BeszelCollector(api=FakeAPI(now), clock=lambda: now)
print(collector.collect(), end="")
