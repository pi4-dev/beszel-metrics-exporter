"""Prometheus exporter for Beszel Hub/PocketBase metrics."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import threading
from contextvars import ContextVar
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import requests
from flask import Flask, Response


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


BESZEL_URL = os.getenv("BESZEL_URL", "http://beszel:8090").rstrip("/")
BESZEL_USER = os.getenv("BESZEL_USER", "")
BESZEL_PASSWORD = os.getenv("BESZEL_PASSWORD", "")
BESZEL_TOKEN = os.getenv("BESZEL_TOKEN", "")
TOKEN_REFRESH_MARGIN_SECONDS = 600
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
SCRAPE_BUDGET_SECONDS = float(os.getenv("SCRAPE_BUDGET_SECONDS", "15"))
if not math.isfinite(SCRAPE_BUDGET_SECONDS) or SCRAPE_BUDGET_SECONDS <= 0:
    raise ValueError("SCRAPE_BUDGET_SECONDS must be a finite positive number")
CACHE_TTL = float(os.getenv("CACHE_TTL", "15"))
FAILURE_CACHE_TTL = float(os.getenv("FAILURE_CACHE_TTL", "5"))
MAX_STATS_AGE_SECONDS = float(os.getenv("MAX_STATS_AGE_SECONDS", "180"))
# Full systemd snapshots are refreshed every 10m; tolerate two intervals.
MAX_SYSTEMD_AGE_SECONDS = float(os.getenv("MAX_SYSTEMD_AGE_SECONDS", "1200"))
MAX_MONITOR_STATS_AGE_SECONDS = float(os.getenv("MAX_MONITOR_STATS_AGE_SECONDS", "600"))
BULK_PAGE_SIZE = int(os.getenv("BULK_PAGE_SIZE", "500"))
BULK_MAX_PAGES = int(os.getenv("BULK_MAX_PAGES", "5"))
LEGACY_UNITS = env_bool("LEGACY_UNITS", False)
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "9105"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("beszel_exporter")

# Limit duplicate warnings to one per metric family per process while still
# counting every discarded sample. The set is deliberately not keyed by labels.
_LOGGED_DUPLICATE_METRICS: set[str] = set()
_LOGGED_DUPLICATE_METRICS_LOCK = threading.Lock()

# Explicit allowlist: an outage must not replay any previously observed
# operational state/health labels, even for families ending in _info.
INFO_IDENTITY_LABELS: dict[str, frozenset[str]] = {
    "beszel_system_info": frozenset({"system", "system_id", "hostname"}),
    "beszel_system_wifi_info": frozenset({"system", "system_id", "interface"}),
    "beszel_gpu_info": frozenset({"system", "system_id", "gpu"}),
    "beszel_container_info": frozenset({"system", "system_id", "container", "container_id"}),
    "beszel_smart_device_info": frozenset({"system", "system_id", "device", "serial"}),
    "beszel_systemd_service_info": frozenset({"system", "system_id", "service"}),
    "beszel_storage_pool_info": frozenset(
        {"system", "system_id", "pool", "display_name", "pool_type"}
    ),
    "beszel_storage_pool_vdev_info": frozenset({"system", "system_id", "pool", "vdev"}),
}

app = Flask(__name__)


class ScrapeDeadlineExceeded(TimeoutError):
    """The budget for one HTTP /metrics scrape was exhausted."""


# A context-local deadline avoids leakage across Gunicorn gthread requests.
_SCRAPE_DEADLINE: ContextVar[float | None] = ContextVar("beszel_scrape_deadline", default=None)


def check_scrape_deadline() -> None:
    deadline = _SCRAPE_DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise ScrapeDeadlineExceeded("Beszel collection time budget exhausted")


def bounded_request_timeout() -> float:
    """Limit each HTTP request to the remaining collection budget."""
    deadline = _SCRAPE_DEADLINE.get()
    if deadline is None:
        return REQUEST_TIMEOUT
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ScrapeDeadlineExceeded("Beszel collection time budget exhausted")
    return min(REQUEST_TIMEOUT, remaining)


def numeric(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def jwt_expires_at(token: str) -> float | None:
    """Read the unverified JWT exp claim for scheduling, not authentication."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or not parts[1] or len(parts[1]) > 16384:
            return None
        payload_bytes = base64.urlsafe_b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4)
        )
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        exp = float(exp)
        return exp if math.isfinite(exp) and exp > 0 else None
    except (ValueError, UnicodeDecodeError, TypeError, OverflowError):
        return None


def decoded(value: Any, default: Any) -> Any:
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


def normalized_container_status(value: Any) -> str:
    """Collapse Docker uptime text into a fixed set of lifecycle states."""
    if not isinstance(value, str):
        return "unknown"
    status = value.strip().lower()
    # Docker Status is display text: "Up 3 hours (healthy)", not a stable state.
    # Check paused first because Docker represents it as "Up ... (Paused)".
    if status.startswith("up ") or status == "up":
        return "paused" if "(paused)" in status else "running"
    for prefix, state in (
        ("running", "running"),
        ("exited", "exited"),
        ("stopped", "exited"),
        ("restarting", "restarting"),
        ("paused", "paused"),
        ("created", "created"),
        ("dead", "dead"),
        ("removing", "removing"),
        ("removal in progress", "removing"),
    ):
        if status == prefix or status.startswith(prefix + " ") or status.startswith(prefix + " ("):
            return state
    return "unknown"


def normalized_system_status(value: Any) -> str:
    """Allow only the four documented Beszel Hub system states."""
    return value if value in ("up", "down", "paused", "pending") else "unknown"

def escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


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


def parse_timestamp(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        if parsed > 10_000_000_000:
            parsed /= 1000
        return parsed
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def record_age_seconds(record: dict[str, Any] | None, now: float | None = None) -> float | None:
    if not record:
        return None
    created = parse_timestamp(record.get("created"))
    if created is None:
        return None
    if now is None:
        now = time.time()
    return max(0.0, now - created)


class PrometheusText:
    """Build valid Prometheus text exposition with contiguous metric families."""

    def __init__(self, on_duplicate: Callable[[str], None] | None = None) -> None:
        self.families: dict[str, dict[str, Any]] = {}
        self._on_duplicate = on_duplicate

    def add(
        self,
        name: str,
        value: Any,
        labels: dict[str, Any] | None = None,
        metric_type: str = "gauge",
        help_text: str | None = None,
    ) -> None:
        value = numeric(value)
        if value is None:
            return
        labels = labels or {}
        family = self.families.setdefault(
            name,
            {
                "type": metric_type,
                "help": help_text or f"Beszel exporter metric {name}.",
                "samples": [],
                "keys": set(),
            },
        )
        if family["type"] != metric_type:
            raise ValueError(f"Metric type conflict for {name}")
        key = tuple(sorted((str(k), str(v)) for k, v in labels.items() if v is not None))
        if key in family["keys"]:
            # A single malformed/repeated source record must not abort all hosts.
            # Warning once per family *per process*, counter for every drop.
            # Only the opaque system_id is logged, never other label values.
            system_id = labels.get("system_id")
            system_id = str(system_id) if system_id is not None else "unknown"
            with _LOGGED_DUPLICATE_METRICS_LOCK:
                if name not in _LOGGED_DUPLICATE_METRICS:
                    logger.warning(
                        "Dropping duplicate Prometheus sample for metric %s (first system_id=%s)",
                        name, system_id,
                    )
                    _LOGGED_DUPLICATE_METRICS.add(name)
            logger.debug("Duplicate sample: metric=%s system_id=%s", name, system_id)
            if self._on_duplicate is not None:
                self._on_duplicate(name)
            return
        family["keys"].add(key)
        family["samples"].append((value, labels))

    def info(self, name: str, labels: dict[str, Any], help_text: str | None = None) -> None:
        self.add(name, 1, labels, help_text=help_text)

    def render(self, *, identity_only: bool = False) -> str:
        """Render all metrics or allowlisted identity-only info."""
        lines: list[str] = []
        for name, family in self.families.items():
            if identity_only:
                allowed_labels = INFO_IDENTITY_LABELS.get(name)
                if allowed_labels is None:
                    continue
            lines.append(f"# HELP {name} {escape_help(family['help'])}")
            lines.append(f"# TYPE {name} {family['type']}")
            seen_identity_series: set[tuple[tuple[str, str], ...]] = set()
            for value, labels in family["samples"]:
                if identity_only:
                    labels = {k: v for k, v in labels.items() if k in allowed_labels}
                    key = tuple(sorted((str(k), str(v)) for k, v in labels.items() if v is not None))
                    if key in seen_identity_series:
                        # Full info samples can differ only by a now-removed
                        # state label. Never emit a duplicate identity series.
                        continue
                    seen_identity_series.add(key)
                if labels:
                    encoded = ",".join(
                        f'{key}="{escape_label(label_value)}"'
                        for key, label_value in sorted(labels.items())
                        if label_value is not None
                    )
                    lines.append(f"{name}{{{encoded}}} {value}")
                else:
                    lines.append(f"{name} {value}")
        return "\n".join(lines) + ("\n" if lines else "")


class BeszelAPI:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.static_token = bool(BESZEL_TOKEN)
        self.token = BESZEL_TOKEN
        if self.token:
            self.session.headers["Authorization"] = self.token

    def _clear_token(self) -> None:
        self.token = ""
        self.session.headers.pop("Authorization", None)

    def authenticate(self) -> None:
        if self.token:
            expiry = jwt_expires_at(self.token)
            remaining = None if expiry is None else expiry - time.time()
            if remaining is not None and remaining > TOKEN_REFRESH_MARGIN_SECONDS:
                return
            if self.static_token:
                logger.error(
                    "Configured BESZEL_TOKEN has no valid JWT exp or expires "
                    "within %s seconds; rotate the token",
                    TOKEN_REFRESH_MARGIN_SECONDS,
                )
                raise RuntimeError("Configured BESZEL_TOKEN is invalid or near expiry")
            logger.info("Beszel auth token missing expiry or near expiry; re-authenticating")
            self._clear_token()
        if not BESZEL_USER or not BESZEL_PASSWORD:
            raise RuntimeError("Configure BESZEL_TOKEN or BESZEL_USER and BESZEL_PASSWORD")
        response = self.session.post(
            f"{BESZEL_URL}/api/collections/users/auth-with-password",
            json={"identity": BESZEL_USER, "password": BESZEL_PASSWORD},
            timeout=bounded_request_timeout(),
        )
        check_scrape_deadline()
        response.raise_for_status()
        token = response.json().get("token")
        expiry = jwt_expires_at(token) if isinstance(token, str) else None
        if expiry is None or expiry <= time.time():
            raise RuntimeError("Beszel login returned a token without a valid future JWT exp")
        self.token = token
        self.session.headers["Authorization"] = token

    def reauthenticate(self) -> None:
        """Force a fresh password login after 401 or a suspicious empty systems list."""
        if self.static_token:
            raise RuntimeError("Cannot refresh a static BESZEL_TOKEN")
        self._clear_token()
        self.authenticate()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        check_scrape_deadline()
        self.authenticate()
        response = self.session.get(
            f"{BESZEL_URL}{path}", params=params, timeout=bounded_request_timeout()
        )
        check_scrape_deadline()
        if response.status_code == 401:
            if self.static_token:
                logger.error("Configured BESZEL_TOKEN rejected with HTTP 401")
                raise RuntimeError("Configured BESZEL_TOKEN rejected by Beszel")
            logger.info("Beszel token rejected; re-authenticating")
            self.reauthenticate()
            response = self.session.get(
                f"{BESZEL_URL}{path}", params=params, timeout=bounded_request_timeout()
            )
            check_scrape_deadline()
        response.raise_for_status()
        payload = response.json()
        check_scrape_deadline()
        return payload

    def records(
        self,
        collection: str,
        *,
        fields: str | None = None,
        filter_expr: str | None = None,
        sort: str | None = None,
        per_page: int = 500,
    ) -> list[dict[str, Any]]:
        retried_empty = False
        while True:
            page = 1
            result: list[dict[str, Any]] = []
            while True:
                check_scrape_deadline()
                params: dict[str, Any] = {
                    "page": page, "perPage": per_page, "skipTotal": 1,
                }
                if fields:
                    params["fields"] = fields
                if filter_expr:
                    params["filter"] = filter_expr
                if sort:
                    params["sort"] = sort
                payload = self.get(f"/api/collections/{collection}/records", params=params)
                items = payload.get("items", [])
                result.extend(items)
                if len(items) < per_page:
                    break
                page += 1
            if collection != "systems" or result:
                return result
            if self.static_token or retried_empty:
                logger.warning(
                    "Beszel systems list is empty%s; check token validity and "
                    "system-sharing permissions",
                    " after a fresh password login" if retried_empty else "",
                )
                return result
            # PocketBase can return HTTP 200 + [] for an unauthenticated list.
            # Retry exactly once with a new password-authenticated session.
            logger.warning("Beszel systems list empty; retrying after re-authentication")
            self.reauthenticate()
            retried_empty = True

    def latest_by_relation(
        self,
        collection: str,
        relation_field: str,
        wanted_ids: set[str],
        *,
        fields: str,
        filter_expr: str = 'type="1m"',
        min_created: float | None = None,
        created_format: Literal["pocketbase", "unix_ms"] = "pocketbase",
    ) -> dict[str, dict[str, Any]]:
        """Fetch fresh records by relation, without paging through stale history."""
        if created_format not in ("pocketbase", "unix_ms"):
            raise ValueError(f"Unsupported created_format: {created_format}")
        if not wanted_ids:
            return {}
        if min_created is not None:
            if created_format == "unix_ms":
                cutoff_ms = math.floor(min_created * 1000)
                filter_expr = f"({filter_expr}) && created >= {cutoff_ms}"
            else:
                # PocketBase date fields use millisecond precision. Round
                # down to whole seconds for borderline records; the caller
                # enforces precise freshness with record_age_seconds().
                cutoff = datetime.fromtimestamp(min_created, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S.000Z"
                )
                filter_expr = f'({filter_expr}) && created >= "{cutoff}"'
        found: dict[str, dict[str, Any]] = {}
        pending = set(wanted_ids)
        page = 1
        while pending and page <= BULK_MAX_PAGES:
            check_scrape_deadline()
            params = {
                "page": page,
                "perPage": BULK_PAGE_SIZE,
                "sort": "-created",
                "skipTotal": 1,
                "filter": filter_expr,
                "fields": fields,
            }
            payload = self.get(f"/api/collections/{collection}/records", params=params)
            items = payload.get("items", [])
            for row in items:
                check_scrape_deadline()
                relation_id = row.get(relation_field)
                if relation_id in pending:
                    found[relation_id] = row
                    pending.remove(relation_id)
            if len(items) < BULK_PAGE_SIZE:
                # Exhausted all fresh records: missing/down/disabled IDs are normal.
                break
            if pending and page == BULK_MAX_PAGES:
                logger.warning(
                    "Fresh Beszel history scan reached page limit for %s (%s IDs still missing)",
                    collection,
                    len(pending),
                )
            page += 1
        return found


class BeszelCollector:
    def __init__(self, api: BeszelAPI | None = None, clock: Callable[[], float] = time.time) -> None:
        self.api = api or BeszelAPI()
        self.clock = clock
        self.lock = threading.Lock()
        self.cache = ""
        self.cache_time = 0.0
        self.cache_ttl = 0.0
        # Only allowlisted identity labels are replayable after a Hub outage.
        self.last_good_identity = ""
        self.last_success = 0.0
        self.collection_errors: defaultdict[str, int] = defaultdict(int)
        self.dropped_samples_total = 0

    def _record_dropped_sample(self, _metric_name: str) -> None:
        # Invoked while collect() holds the collector lock. Counts persist across scrapes,
        # including an attempt that subsequently fails for an unrelated reason.
        self.dropped_samples_total += 1

    def optional_records(self, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            check_scrape_deadline()
            records = self.api.records(collection, **kwargs)
            check_scrape_deadline()
            return records
        except ScrapeDeadlineExceeded:
            raise
        except Exception:
            # Network timeouts near the budget boundary are scrape failures,
            # not optional-data omissions.
            check_scrape_deadline()
            self.collection_errors[collection] += 1
            logger.exception("Failed to read Beszel collection %s", collection)
            return []

    def optional_latest_by_relation(
        self,
        collection: str,
        relation_field: str,
        wanted_ids: set[str],
        *,
        fields: str,
        min_created: float,
        created_format: Literal["pocketbase", "unix_ms"] = "pocketbase",
    ) -> dict[str, dict[str, Any]]:
        try:
            check_scrape_deadline()
            records = self.api.latest_by_relation(
                collection,
                relation_field,
                wanted_ids,
                fields=fields,
                min_created=min_created,
                created_format=created_format,
            )
            check_scrape_deadline()
            return records
        except ScrapeDeadlineExceeded:
            raise
        except Exception:
            check_scrape_deadline()
            self.collection_errors[collection] += 1
            logger.exception("Failed to read latest Beszel records from %s", collection)
            return {}

    def collect(self) -> str:
        # Include lock contention in the per-request budget. A second scrape
        # must not wait 15s for the first and then start its own 15s collection.
        started = time.monotonic()
        deadline = started + SCRAPE_BUDGET_SECONDS
        if not self.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            logger.warning("Beszel scrape deadline exceeded waiting for collector lock")
            # The in-flight collector owns mutable state. Return only local
            # diagnostics without reading its cache or unsafe shared dictionaries.
            metrics = PrometheusText()
            metrics.add("beszel_exporter_up", 0)
            metrics.add("beszel_exporter_scrape_duration_seconds", time.monotonic() - started)
            return metrics.render()
        try:
            now = self.clock()
            if self.cache and now - self.cache_time < self.cache_ttl:
                return self.cache
            token = _SCRAPE_DEADLINE.set(deadline)
            try:
                check_scrape_deadline()
                data, identity_data = self._collect_data(now)
                check_scrape_deadline()
                self.last_good_identity = identity_data
                self.last_success = self.clock()
                body = self._with_self_metrics(data, 1, time.monotonic() - started)
                ttl = CACHE_TTL
            except ScrapeDeadlineExceeded:
                logger.warning("Beszel scrape exceeded SCRAPE_BUDGET_SECONDS=%.1f", SCRAPE_BUDGET_SECONDS)
                body = self._with_self_metrics(self.last_good_identity, 0, time.monotonic() - started)
                ttl = FAILURE_CACHE_TTL
            except Exception:
                logger.exception("Beszel scrape failed")
                # Never replay un-timestamped CPU, temperature, network, disk or host
                # availability samples on an outage: Prometheus would record them
                # as fresh at every subsequent scrape.
                body = self._with_self_metrics(self.last_good_identity, 0, time.monotonic() - started)
                ttl = FAILURE_CACHE_TTL
            finally:
                _SCRAPE_DEADLINE.reset(token)
            self.cache = body
            self.cache_time = self.clock()
            self.cache_ttl = ttl
            return body
        finally:
            self.lock.release()

    def _with_self_metrics(self, data: str, up: int, duration: float) -> str:
        metrics = PrometheusText()
        metrics.add("beszel_exporter_up", up, help_text="Whether the last Beszel collection attempt succeeded.")
        metrics.add(
            "beszel_exporter_scrape_duration_seconds",
            duration,
            help_text="Time spent collecting the most recent exporter snapshot.",
        )
        if self.last_success:
            metrics.add(
                "beszel_exporter_last_success_timestamp_seconds",
                self.last_success,
                help_text="Unix timestamp of the last successful Beszel collection.",
            )
        for collection, count in self.collection_errors.items():
            metrics.add(
                "beszel_exporter_collection_errors_total",
                count,
                {"collection": collection},
                metric_type="counter",
                help_text="Total optional Beszel collection read errors.",
            )
        metrics.add(
            "beszel_exporter_dropped_samples_total",
            self.dropped_samples_total,
            metric_type="counter",
            help_text="Total samples omitted because metric name and label set were duplicated.",
        )
        return data + metrics.render()

    def _collect_data(self, now: float) -> tuple[str, str]:
        metrics = PrometheusText(on_duplicate=self._record_dropped_sample)
        systems = self.api.records("systems")
        system_ids = {row.get("id") for row in systems if row.get("id")}
        metrics.add(
            "beszel_exporter_systems",
            len(system_ids),
            help_text="Number of distinct Beszel systems visible to the configured account.",
        )
        up_ids = {
            row["id"]
            for row in systems
            if row.get("id") and row.get("status") == "up"
        }

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

        monitor_ids = {
            row.get("id")
            for row in related["network_monitors"]
            if row.get("id")
        }
        enabled_monitor_ids = {
            row["id"]
            for row in related["network_monitors"]
            if row.get("id") and row.get("enabled") and row.get("system") in up_ids
        }
        system_stats = self.optional_latest_by_relation(
            "system_stats",
            "system",
            system_ids,
            fields="system,stats,created,type",
            min_created=now - MAX_STATS_AGE_SECONDS,
        )
        container_stats = self.optional_latest_by_relation(
            "container_stats",
            "system",
            system_ids,
            fields="system,stats,created,type",
            min_created=now - MAX_STATS_AGE_SECONDS,
        )
        monitor_stats = self.optional_latest_by_relation(
            "network_monitor_stats",
            "monitor",
            monitor_ids,
            fields="monitor,res_min,res_max,total_count,success_count,res_sum,created,type",
            min_created=now - MAX_MONITOR_STATS_AGE_SECONDS,
            created_format="unix_ms",
        )

        # Coverage reports active sources only, but bulk history queries above
        # intentionally retain all IDs (including offline/disabled sources).
        # An offline host or disabled monitor must not lower coverage, even if
        # an older history row for it happens to be returned by the API.
        # Read failures still increment collection_errors_total separately.
        for collection, wanted_ids, found in (
            ("system_stats", up_ids, system_stats),
            ("container_stats", up_ids, container_stats),
            ("network_monitor_stats", enabled_monitor_ids, monitor_stats),
        ):
            history_labels = {"collection": collection}
            metrics.add(
                "beszel_exporter_history_records_wanted",
                len(wanted_ids),
                history_labels,
                help_text="Number of eligible active system or monitor IDs for fresh 1m history.",
            )
            metrics.add(
                "beszel_exporter_history_records_found",
                len(wanted_ids.intersection(found)),
                history_labels,
                help_text="Number of eligible active IDs with fresh 1m history records.",
            )

        for system in systems:
            check_scrape_deadline()
            system_id = system.get("id")
            if not system_id:
                continue
            labels = {"system": system.get("name", system_id), "system_id": system_id}
            system_up = system.get("status") == "up"

            stats_record = system_stats.get(system_id)
            stats_age = record_age_seconds(stats_record, now)
            if stats_age is not None:
                metrics.add(
                    "beszel_system_stats_age_seconds",
                    stats_age,
                    labels,
                    help_text="Age of the newest Beszel system_stats record.",
                )
            stats_fresh = system_up and stats_age is not None and stats_age <= MAX_STATS_AGE_SECONDS
            self.emit_system_record(
                metrics, labels, system, details.get(system_id), system_up,
                include_status=stats_fresh or not system_up,
            )
            if stats_fresh and stats_record:
                self.emit_system_stats(metrics, labels, decoded(stats_record.get("stats"), {}))

            container_record = container_stats.get(system_id)
            container_age = record_age_seconds(container_record, now)
            if container_age is not None:
                metrics.add(
                    "beszel_container_stats_age_seconds",
                    container_age,
                    labels,
                    help_text="Age of the newest Beszel container_stats record.",
                )
            history_fresh = system_up and container_age is not None and container_age <= MAX_STATS_AGE_SECONDS
            history = decoded(container_record.get("stats"), []) if history_fresh and container_record else []

            self.emit_containers(
                metrics,
                labels,
                grouped["containers"][system_id],
                history,
                emit_dynamic=stats_fresh,
                now=now,
            )
            self.emit_smart(metrics, labels, grouped["smart_devices"][system_id], emit_dynamic=stats_fresh)
            self.emit_systemd(
                metrics, labels, grouped["systemd_services"][system_id],
                emit_dynamic=stats_fresh, now=now,
            )
            self.emit_storage_pools(metrics, labels, grouped["zfs_pools"][system_id], emit_dynamic=stats_fresh)
            self.emit_network_monitors(
                metrics,
                labels,
                grouped["network_monitors"][system_id],
                monitor_stats,
                stats_fresh,
                now,
            )
        check_scrape_deadline()
        full = metrics.render()
        check_scrape_deadline()
        identity = metrics.render(identity_only=True)
        check_scrape_deadline()
        return full, identity

    @staticmethod
    def emit_pair(
        metrics: PrometheusText,
        name: str,
        values: Any,
        labels: dict[str, Any],
        pair_labels: tuple[str, str],
        *,
        start: int = 0,
        metric_type: str = "gauge",
    ) -> None:
        if not isinstance(values, (list, tuple)):
            return
        for offset, dimension in enumerate(pair_labels):
            index = start + offset
            if index < len(values):
                metrics.add(name, values[index], {**labels, "direction": dimension}, metric_type=metric_type)

    @staticmethod
    def emit_pair_mib_fallback(
        metrics: PrometheusText,
        name: str,
        values: Any,
        legacy_values: tuple[Any, Any],
        labels: dict[str, Any],
        pair_labels: tuple[str, str],
    ) -> None:
        if isinstance(values, (list, tuple)) and len(values) >= 2:
            BeszelCollector.emit_pair(metrics, name, values, labels, pair_labels)
            return
        for direction, raw in zip(pair_labels, legacy_values, strict=True):
            value = numeric(raw)
            if value is not None:
                metrics.add(name, float(value) * 1024**2, {**labels, "direction": direction})

    @staticmethod
    def emit_system_record(
        metrics: PrometheusText,
        labels: dict[str, Any],
        system: dict[str, Any],
        details: dict[str, Any] | None,
        system_up: bool,
        *,
        include_status: bool = True,
    ) -> None:
        info = decoded(system.get("info"), {})
        details = details or {}
        identity = {
            **labels,
            "agent_version": info.get("v", system.get("v", "")),
            "hostname": details.get("hostname", info.get("h", "")),
            "kernel": details.get("kernel", info.get("k", "")),
            "os_name": details.get("os_name", ""),
            "cpu_model": details.get("cpu", info.get("m", "")),
            "root_disk_name": info.get("rdn", ""),
        }
        if include_status:
            identity["status"] = normalized_system_status(system.get("status"))
        metrics.info("beszel_system_info", identity)
        metrics.add("beszel_system_up", 1 if system_up else 0, labels)
        if system_up:
            metrics.add("beszel_system_uptime_seconds", info.get("u"), labels)
            metrics.add("beszel_system_dashboard_temperature_celsius", info.get("dt"), labels)
        metrics.add("beszel_system_connection_type", info.get("ct"), labels)
        metrics.add("beszel_system_journal_available", info.get("jl"), labels)
        metrics.add("beszel_system_cpu_cores", details.get("cores"), labels)
        metrics.add("beszel_system_cpu_threads", details.get("threads"), labels)
        metrics.add("beszel_system_podman", details.get("podman"), labels)
        metrics.add("beszel_system_detected_memory_bytes", details.get("memory"), labels)

        if system_up:
            services = info.get("sv")
            if isinstance(services, list):
                if len(services) >= 1:
                    metrics.add(
                        "beszel_systemd_services",
                        services[0],
                        labels,
                        help_text="Number of systemd services reported for a Beszel system.",
                    )
                if len(services) >= 2:
                    metrics.add("beszel_systemd_services_failed", services[1], labels)

            updates = info.get("pu")
            if isinstance(updates, list) and updates:
                # Beszel reports [total, security] and omits security when
                # the package manager cannot determine which updates are security.
                total_updates = numeric(updates[0])
                if total_updates is not None and total_updates >= 0:
                    metrics.add("beszel_package_updates_pending", total_updates, labels)
                    if len(updates) >= 2:
                        security_updates = numeric(updates[1])
                        if (
                            security_updates is not None
                            and 0 <= security_updates <= total_updates
                        ):
                            metrics.add(
                                "beszel_package_security_updates_pending",
                                security_updates,
                                labels,
                            )

        for interface, station in decoded(info.get("wf"), {}).items():
            if isinstance(station, dict):
                metrics.info(
                    "beszel_system_wifi_info",
                    {**labels, "interface": interface, "ssid": station.get("s", "")},
                )

    @classmethod
    def emit_system_stats(cls, metrics: PrometheusText, labels: dict[str, Any], stats: dict[str, Any]) -> None:
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
        }
        for name, (key, factor) in gauges.items():
            value = numeric(stats.get(key))
            if value is not None:
                metrics.add(name, float(value) * factor, labels)

        if LEGACY_UNITS:
            legacy = {
                "beszel_system_disk_read_mib_per_second": "dr",
                "beszel_system_disk_write_mib_per_second": "dw",
                "beszel_system_disk_read_peak_mib_per_second": "drm",
                "beszel_system_disk_write_peak_mib_per_second": "dwm",
                "beszel_system_network_sent_mib_per_second": "ns",
                "beszel_system_network_received_mib_per_second": "nr",
                "beszel_system_network_sent_peak_mib_per_second": "nsm",
                "beszel_system_network_received_peak_mib_per_second": "nrm",
            }
            for name, key in legacy.items():
                metrics.add(name, stats.get(key), labels)

        load = stats.get("la")
        if isinstance(load, list):
            for index, window in enumerate(("1m", "5m", "15m")):
                if index < len(load):
                    metrics.add("beszel_system_load_average", load[index], {**labels, "window": window})

        cls.emit_pair_mib_fallback(
            metrics,
            "beszel_system_network_bytes_per_second",
            stats.get("b"),
            (stats.get("ns"), stats.get("nr")),
            labels,
            ("transmit", "receive"),
        )
        cls.emit_pair_mib_fallback(
            metrics,
            "beszel_system_network_peak_bytes_per_second",
            stats.get("bm"),
            (stats.get("nsm"), stats.get("nrm")),
            labels,
            ("transmit", "receive"),
        )
        cls.emit_pair_mib_fallback(
            metrics,
            "beszel_system_disk_io_bytes_per_second",
            stats.get("dio"),
            (stats.get("dr"), stats.get("dw")),
            labels,
            ("read", "write"),
        )
        cls.emit_pair_mib_fallback(
            metrics,
            "beszel_system_disk_io_peak_bytes_per_second",
            stats.get("diom"),
            (stats.get("drm"), stats.get("dwm")),
            labels,
            ("read", "write"),
        )
        cls.emit_pair(
            metrics,
            "beszel_system_disk_io_bytes_total",
            stats.get("diot"),
            labels,
            ("read", "write"),
            metric_type="counter",
        )

        cpu_breakdown = stats.get("cpub")
        if isinstance(cpu_breakdown, list):
            for index, mode in enumerate(("user", "system", "iowait", "steal", "idle")):
                if index < len(cpu_breakdown):
                    metrics.add("beszel_system_cpu_time_percent", cpu_breakdown[index], {**labels, "mode": mode})
        cpu_cores = stats.get("cpus")
        if isinstance(cpu_cores, list):
            for index, value in enumerate(cpu_cores):
                metrics.add("beszel_system_cpu_core_usage_percent", value, {**labels, "core": str(index)})

        for sensor, value in decoded(stats.get("t"), {}).items():
            metrics.add("beszel_system_temperature_celsius", value, {**labels, "sensor": sensor})
        for fan, value in decoded(stats.get("f"), {}).items():
            metrics.add("beszel_system_fan_rpm", value, {**labels, "fan": fan})
        for battery_name, value in decoded(stats.get("bats"), {}).items():
            metrics.add("beszel_system_battery_percent", value, {**labels, "battery": battery_name})
        battery = stats.get("bat")
        if isinstance(battery, list):
            if len(battery) >= 1:
                metrics.add("beszel_system_battery_percent", battery[0], {**labels, "battery": "primary"})
            if len(battery) >= 2:
                metrics.add("beszel_system_battery_state", battery[1], {**labels, "battery": "primary"})

        cls.emit_disk_io_stats(metrics, labels, stats.get("dios"), prefix="beszel_system_disk")
        cls.emit_disk_io_stats(metrics, labels, stats.get("diosm"), prefix="beszel_system_disk_peak")

        for interface, values in decoded(stats.get("ni"), {}).items():
            if not isinstance(values, list):
                continue
            nic_labels = {**labels, "interface": interface}
            cls.emit_pair(metrics, "beszel_system_network_interface_bytes_per_second", values, nic_labels, ("transmit", "receive"))
            cls.emit_pair(
                metrics,
                "beszel_system_network_interface_bytes_total",
                values,
                nic_labels,
                ("transmit", "receive"),
                start=2,
                metric_type="counter",
            )
        for interface, rssi in decoded(stats.get("wf"), {}).items():
            metrics.add("beszel_system_wifi_rssi_dbm", rssi, {**labels, "interface": interface})

        for filesystem, fs in decoded(stats.get("efs"), {}).items():
            if not isinstance(fs, dict):
                continue
            fs_labels = {**labels, "filesystem": filesystem}
            total = numeric(fs.get("d"))
            used = numeric(fs.get("du"))
            metrics.add("beszel_filesystem_total_bytes", gib_to_bytes(total), fs_labels)
            metrics.add("beszel_filesystem_used_bytes", gib_to_bytes(used), fs_labels)
            if total and used is not None:
                metrics.add("beszel_filesystem_usage_percent", float(used) / float(total) * 100, fs_labels)
            read_bps = numeric(fs.get("rb"))
            write_bps = numeric(fs.get("wb"))
            read_peak_bps = numeric(fs.get("rbm"))
            write_peak_bps = numeric(fs.get("wbm"))
            if read_bps is None and numeric(fs.get("r")) is not None:
                read_bps = float(numeric(fs.get("r"))) * 1024**2
            if write_bps is None and numeric(fs.get("w")) is not None:
                write_bps = float(numeric(fs.get("w"))) * 1024**2
            if read_peak_bps is None and numeric(fs.get("rm")) is not None:
                read_peak_bps = float(numeric(fs.get("rm"))) * 1024**2
            if write_peak_bps is None and numeric(fs.get("wm")) is not None:
                write_peak_bps = float(numeric(fs.get("wm"))) * 1024**2
            metrics.add("beszel_filesystem_read_bytes_per_second", read_bps, fs_labels)
            metrics.add("beszel_filesystem_write_bytes_per_second", write_bps, fs_labels)
            metrics.add("beszel_filesystem_read_peak_bytes_per_second", read_peak_bps, fs_labels)
            metrics.add("beszel_filesystem_write_peak_bytes_per_second", write_peak_bps, fs_labels)
            metrics.add("beszel_filesystem_read_bytes_total", fs.get("tr"), fs_labels, metric_type="counter")
            metrics.add("beszel_filesystem_write_bytes_total", fs.get("tw"), fs_labels, metric_type="counter")
            if LEGACY_UNITS:
                for metric_name, key in {
                    "beszel_filesystem_read_mib_per_second": "r",
                    "beszel_filesystem_write_mib_per_second": "w",
                    "beszel_filesystem_read_peak_mib_per_second": "rm",
                    "beszel_filesystem_write_peak_mib_per_second": "wm",
                }.items():
                    metrics.add(metric_name, fs.get(key), fs_labels)
            cls.emit_disk_io_stats(metrics, fs_labels, fs.get("dios"), prefix="beszel_filesystem")
            cls.emit_disk_io_stats(metrics, fs_labels, fs.get("diosm"), prefix="beszel_filesystem_peak")

        for gpu_id, gpu in decoded(stats.get("g"), {}).items():
            if not isinstance(gpu, dict):
                continue
            gpu_labels = {**labels, "gpu": gpu_id}
            metrics.info("beszel_gpu_info", {**gpu_labels, "name": gpu.get("n", "")})
            metrics.add("beszel_gpu_usage_percent", gpu.get("u"), gpu_labels)
            metrics.add("beszel_gpu_memory_used_bytes", mib_to_bytes(gpu.get("mu")), gpu_labels)
            metrics.add("beszel_gpu_memory_total_bytes", mib_to_bytes(gpu.get("mt")), gpu_labels)
            metrics.add("beszel_gpu_power_watts", gpu.get("p"), gpu_labels)
            metrics.add("beszel_gpu_package_power_watts", gpu.get("pp"), gpu_labels)
            for engine, value in decoded(gpu.get("e"), {}).items():
                metrics.add("beszel_gpu_engine_usage_percent", value, {**gpu_labels, "engine": engine})

        for pool_name, pool in decoded(stats.get("z"), {}).items():
            if not isinstance(pool, dict):
                continue
            pool_labels = {**labels, "pool": pool_name}
            metrics.add("beszel_storage_pool_total_bytes", gib_to_bytes(pool.get("d")), pool_labels)
            metrics.add("beszel_storage_pool_used_bytes", gib_to_bytes(pool.get("du")), pool_labels)
            metrics.add("beszel_storage_pool_read_bytes_per_second", pool.get("rb"), pool_labels)
            metrics.add("beszel_storage_pool_write_bytes_per_second", pool.get("wb"), pool_labels)
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
        mappings = (
            (0, "read_time_percent", 1),
            (1, "write_time_percent", 1),
            (2, "utilization_percent", 1),
            (3, "read_await_seconds", 0.001),
            (4, "write_await_seconds", 0.001),
            (5, "weighted_io_percent", 1),
        )
        for index, suffix, factor in mappings:
            if index < len(values):
                value = numeric(values[index])
                if value is not None:
                    metrics.add(f"{prefix}_{suffix}", float(value) * factor, labels)
        if LEGACY_UNITS:
            if len(values) > 3:
                metrics.add(f"{prefix}_read_await_milliseconds", values[3], labels)
            if len(values) > 4:
                metrics.add(f"{prefix}_write_await_milliseconds", values[4], labels)

    @staticmethod
    def emit_containers(
        metrics: PrometheusText,
        labels: dict[str, Any],
        containers: list[dict[str, Any]],
        historical_stats: list[dict[str, Any]],
        *,
        emit_dynamic: bool,
        now: float,
    ) -> None:
        history_by_name = {
            row.get("n"): row
            for row in historical_stats
            if isinstance(row, dict) and row.get("n")
        }
        for container in containers:
            name = container.get("name", "")
            series_labels = {**labels, "container": name}
            # Beszel upserts container records but may keep removed containers
            # or cease updating this collection when using older agents.
            # Fresh host statistics alone cannot validate container state.
            row_age = record_age_seconds(
                {"created": container.get("updated")}, now
            )
            row_fresh = (
                emit_dynamic
                and row_age is not None
                and row_age <= MAX_STATS_AGE_SECONDS
            )
            metrics.info(
                "beszel_container_info",
                {
                    **series_labels,
                    "container_id": container.get("id", ""),
                    "image": container.get("image", ""),
                    **(
                        {
                            "status": normalized_container_status(container.get("status")),
                            "ports": container.get("ports", ""),
                        }
                        if row_fresh else {}
                    ),
                },
            )
            if not row_fresh:
                continue
            metrics.add("beszel_container_cpu_usage_percent", container.get("cpu"), series_labels)
            metrics.add("beszel_container_memory_used_bytes", mib_to_bytes(container.get("memory")), series_labels)
            metrics.add(
                "beszel_container_network_combined_bytes_per_second",
                container.get("net"),
                series_labels,
            )
            metrics.add("beszel_container_health_state", container.get("health"), series_labels)
            metrics.add("beszel_container_update_available", container.get("updatable"), series_labels)
            history = history_by_name.get(name, {})
            BeszelCollector.emit_pair(
                metrics,
                "beszel_container_network_bytes_per_second",
                history.get("b"),
                series_labels,
                ("transmit", "receive"),
            )

    @staticmethod
    def emit_smart(
        metrics: PrometheusText,
        labels: dict[str, Any],
        devices: list[dict[str, Any]],
        *,
        emit_dynamic: bool,
    ) -> None:
        for device in devices:
            device_name = device.get("name", "")
            serial = device.get("serial", "")
            data_labels = {**labels, "device": device_name, "serial": serial}
            metrics.info(
                "beszel_smart_device_info",
                {
                    **data_labels,
                    "model": device.get("model", ""),
                    "firmware": device.get("firmware", ""),
                    "device_type": device.get("type", ""),
                    **({"state": device.get("state", "")} if emit_dynamic else {}),
                },
            )
            if not emit_dynamic:
                continue
            metrics.add("beszel_smart_capacity_bytes", device.get("capacity"), data_labels)
            metrics.add("beszel_smart_temperature_celsius", device.get("temp"), data_labels)
            power_on_hours = numeric(device.get("hours"))
            if power_on_hours is not None:
                metrics.add(
                    "beszel_smart_power_on_seconds_total",
                    float(power_on_hours) * 3600,
                    data_labels,
                    metric_type="counter",
                )
                if LEGACY_UNITS:
                    metrics.add(
                        "beszel_smart_power_on_hours_total",
                        power_on_hours,
                        data_labels,
                        metric_type="counter",
                    )
            metrics.add("beszel_smart_power_cycles_total", device.get("cycles"), data_labels, metric_type="counter")
            state = str(device.get("state", "")).upper()
            if state:
                metrics.add("beszel_smart_passed", 1 if state == "PASSED" else 0, data_labels)
            for attribute in decoded(device.get("attributes"), []):
                if not isinstance(attribute, dict):
                    continue
                attribute_labels = {
                    **data_labels,
                    "attribute": attribute.get("n", ""),
                    "attribute_id": str(attribute.get("id", "")),
                }
                metrics.add("beszel_smart_attribute_value", attribute.get("v"), attribute_labels)
                metrics.add("beszel_smart_attribute_worst", attribute.get("w"), attribute_labels)
                metrics.add("beszel_smart_attribute_threshold", attribute.get("t"), attribute_labels)
                metrics.add("beszel_smart_attribute_raw", attribute.get("rv"), attribute_labels)

    @staticmethod
    def emit_systemd(
        metrics: PrometheusText,
        labels: dict[str, Any],
        services: list[dict[str, Any]],
        *,
        emit_dynamic: bool,
        now: float,
    ) -> None:
        states = ("active", "inactive", "failed", "activating", "deactivating", "reloading")
        substates = ("dead", "running", "exited", "failed", "unknown")
        for service in services:
            service_labels = {**labels, "service": service.get("name", "")}
            row_age = record_age_seconds({"created": service.get("updated")}, now)
            row_fresh = (
                emit_dynamic
                and row_age is not None
                and row_age <= MAX_SYSTEMD_AGE_SECONDS
            )
            state_value = numeric(service.get("state"))
            substate_value = numeric(service.get("sub"))
            state = (
                int(state_value)
                if state_value is not None and float(state_value).is_integer()
                else -1
            )
            substate = (
                int(substate_value)
                if substate_value is not None and float(substate_value).is_integer()
                else -1
            )
            state_known = 0 <= state < len(states)
            state_text = states[state] if state_known else "unknown"
            substate_text = substates[substate] if 0 <= substate < len(substates) else "unknown"
            state_labels = {"state": state_text, "substate": substate_text} if row_fresh else {}
            metrics.info("beszel_systemd_service_info", {**service_labels, **state_labels})
            if not row_fresh:
                continue
            if state_known:
                metrics.add("beszel_systemd_service_active", 1 if state == 0 else 0, service_labels)
                metrics.add("beszel_systemd_service_failed", 1 if state == 2 else 0, service_labels)
            metrics.add("beszel_systemd_service_cpu_usage_percent", service.get("cpu"), service_labels)
            metrics.add("beszel_systemd_service_cpu_peak_percent", service.get("cpuPeak"), service_labels)
            metrics.add("beszel_systemd_service_memory_bytes", service.get("memory"), service_labels)
            metrics.add("beszel_systemd_service_memory_peak_bytes", service.get("memPeak"), service_labels)

    @staticmethod
    def emit_storage_pools(
        metrics: PrometheusText,
        labels: dict[str, Any],
        pools: list[dict[str, Any]],
        *,
        emit_dynamic: bool,
    ) -> None:
        for pool in pools:
            raw_name = pool.get("name", "")
            pool_labels = {**labels, "pool": raw_name}
            metrics.info(
                "beszel_storage_pool_info",
                {
                    **pool_labels,
                    "display_name": pool.get("display_name", raw_name),
                    "pool_type": "btrfs" if str(raw_name).startswith("b:") else "zfs",
                    **({"health": pool.get("health", "")} if emit_dynamic else {}),
                },
            )
            if not emit_dynamic:
                continue
            metrics.add("beszel_storage_pool_size_bytes", pool.get("size"), pool_labels)
            metrics.add("beszel_storage_pool_allocated_bytes", pool.get("alloc"), pool_labels)
            metrics.add("beszel_storage_pool_free_bytes", pool.get("free"), pool_labels)
            scrub = decoded(pool.get("scrub"), {})
            if scrub:
                metrics.info("beszel_storage_pool_scrub_info", {**pool_labels, "state": scrub.get("state", "")})
                progress = str(scrub.get("progress", "")).strip().rstrip("%")
                metrics.add("beszel_storage_pool_scrub_progress_percent", progress, pool_labels)
                metrics.add("beszel_storage_pool_scrub_errors", scrub.get("errors"), pool_labels)
            for vdev in decoded(pool.get("vdevs"), []):
                if not isinstance(vdev, dict):
                    continue
                vdev_labels = {**pool_labels, "vdev": vdev.get("name", "")}
                metrics.info("beszel_storage_pool_vdev_info", {**vdev_labels, "state": vdev.get("state", "")})
                metrics.add("beszel_storage_pool_vdev_read_errors", vdev.get("readErrs"), vdev_labels)
                metrics.add("beszel_storage_pool_vdev_write_errors", vdev.get("writeErrs"), vdev_labels)
                metrics.add("beszel_storage_pool_vdev_checksum_errors", vdev.get("checksumErrs"), vdev_labels)
            for dataset in decoded(pool.get("datasets"), []):
                if not isinstance(dataset, dict):
                    continue
                dataset_labels = {**pool_labels, "dataset": dataset.get("name", ""), "mount": dataset.get("mount", "")}
                metrics.add("beszel_storage_pool_dataset_used_bytes", dataset.get("used"), dataset_labels)
                metrics.add("beszel_storage_pool_dataset_available_bytes", dataset.get("avail"), dataset_labels)

    @staticmethod
    def emit_network_monitors(
        metrics: PrometheusText,
        labels: dict[str, Any],
        monitors: list[dict[str, Any]],
        monitor_stats: dict[str, dict[str, Any]],
        source_fresh: bool,
        now: float,
    ) -> None:
        for monitor in monitors:
            monitor_id = monitor.get("id", "")
            monitor_labels = {
                **labels,
                "monitor_id": monitor_id,
                "target": monitor.get("target", ""),
                "protocol": monitor.get("protocol", ""),
                "port": str(monitor.get("port", "")),
                "server": monitor.get("server", ""),
            }
            metrics.add("beszel_network_monitor_enabled", monitor.get("enabled"), monitor_labels)
            # Beszel stores intervals in seconds and its agent defaults to 30s
            # when the configured value is missing or below one second.
            interval = numeric(monitor.get("interval"))
            if interval is None or interval < 1:
                interval = 30
            metrics.add("beszel_network_monitor_interval_seconds", interval, monitor_labels)

            cert = decoded(monitor.get("certInfo"), {})
            if cert:
                metrics.add(
                    "beszel_network_monitor_tls_cert_expiry_timestamp_seconds",
                    milliseconds_to_seconds(cert.get("expires")),
                    monitor_labels,
                )
                if cert.get("issuer"):
                    metrics.info("beszel_network_monitor_tls_cert_info", {**monitor_labels, "issuer": cert.get("issuer", "")})

            # Beszel updates this row only after the agent returns a result
            # for this monitor. Fresh host stats alone must never validate
            # cached per-monitor current/1h values.
            monitor_updated_age = record_age_seconds(
                {"created": monitor.get("updated")}, now
            )
            current_fresh = (
                source_fresh
                and bool(monitor.get("enabled"))
                and monitor_updated_age is not None
                and monitor_updated_age <= max(2 * interval, MAX_STATS_AGE_SECONDS)
            )
            if current_fresh:
                # The agent reports zero response time when no probe succeeded.
                # Suppress missing latency rather than plotting false 0s.
                current_response = numeric(monitor.get("res"))
                if current_response is not None and current_response > 0:
                    metrics.add(
                        "beszel_network_monitor_response_seconds",
                        microseconds_to_seconds(current_response),
                        {**monitor_labels, "window": "current"},
                    )
                hourly_loss = numeric(monitor.get("loss1h"))
                if hourly_loss is not None and 0 <= hourly_loss < 100:
                    for window, value in {
                        "1h_avg": monitor.get("resAvg1h"),
                        "1h_min": monitor.get("resMin1h"),
                        "1h_max": monitor.get("resMax1h"),
                    }.items():
                        response = numeric(value)
                        if response is not None and response > 0:
                            metrics.add(
                                "beszel_network_monitor_response_seconds",
                                microseconds_to_seconds(response),
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

            record = monitor_stats.get(monitor_id)
            age = record_age_seconds(record, now)
            if age is not None:
                metrics.add("beszel_network_monitor_stats_age_seconds", age, monitor_labels)
            if not source_fresh or not record or age is None or age > MAX_MONITOR_STATS_AGE_SECONDS:
                continue

            # A missing count is unknown, not a zero-valued observation.
            # Keep the three inputs independent: the response average only
            # requires a positive success_count and a real res_sum.
            total = numeric(record.get("total_count"))
            success = numeric(record.get("success_count"))
            response_sum = numeric(record.get("res_sum"))
            # Rolling 1m observations can decrease, so this is a gauge.
            # Categories must be disjoint to support sum without(result).
            # Suppress impossible loss or probe buckets when counts are absent
            # or inconsistent. Average latency is independent of total_count.
            consistent = (
                total is not None
                and success is not None
                and total >= 0
                and 0 <= success <= total
            )
            if consistent:
                for result, count in (("success", success), ("failure", total - success)):
                    metrics.add(
                        "beszel_network_monitor_probes",
                        count,
                        {**monitor_labels, "result": result, "window": "1m"},
                        help_text="Number of successful or failed probes in the latest 1-minute Beszel monitor history aggregate.",
                    )
            # At 100% packet loss the agent stores res_min=res_max=0.
            # All minute response windows require at least one success.
            if success is not None and success > 0:
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
                if response_sum is not None:
                    metrics.add(
                        "beszel_network_monitor_response_seconds",
                        float(response_sum) / float(success) / 1_000_000,
                        {**monitor_labels, "window": "1m_avg"},
                    )
            if consistent and total > 0:
                metrics.add(
                    "beszel_network_monitor_packet_loss_percent",
                    (float(total) - float(success)) * 100 / float(total),
                    {**monitor_labels, "window": "1m"},
                )


collector = BeszelCollector()


@app.get("/metrics")
def metrics_endpoint() -> Response:
    return Response(
        collector.collect(),
        status=200,
        mimetype="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/healthz")
def health_endpoint() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
