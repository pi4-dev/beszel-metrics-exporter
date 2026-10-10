# Beszel Metrics Exporter

A Prometheus-compatible exporter for [Beszel](https://github.com/henrygd/beszel).

It reads monitoring data from the Beszel Hub / PocketBase API and exposes it on `/metrics`, allowing `vmagent`, Prometheus, VictoriaMetrics, OpenObserve, Grafana, and other Prometheus-compatible components to consume Beszel data without modifying the Beszel agents.

## Architecture

```text
Beszel agents
      |
      v
  Beszel Hub
  PocketBase API
      |
      v
Beszel Metrics Exporter :9105/metrics
      |
      | scrape
      v
    vmagent
      |
      | remote_write
      v
OpenObserve / VictoriaMetrics / other backend
      |
      v
    Grafana
```

The exporter is read-only. It never refreshes SMART/ZFS/systemd data and never writes to Beszel collections.

## Highlights

- Host CPU, memory, storage, network, temperature, fan, battery and Wi-Fi metrics
- Per-interface network counters and rates
- Docker / Podman container metrics
- SMART device and SMART attribute metrics
- systemd service metrics
- Beszel network-monitor latency, packet loss, probe and TLS certificate metrics
- ZFS / Btrfs pools, vdevs, datasets and scrub state
- GPU utilization, memory, power and engine metrics
- Prometheus metric families with `# HELP` and `# TYPE`
- Bounded bulk retrieval for Beszel `1m` history records
- Stale-data suppression
- Metadata-only fallback during Beszel Hub outages (dynamic samples immediately omitted)
- Read-only, non-root container image
- Unit tests, Ruff, `promtool check metrics`, Docker smoke tests and multi-arch GHCR publishing

## Data retrieval model

The exporter reads current-state collections such as:

- `systems`
- `system_details`
- `containers`
- `smart_devices`
- `systemd_services`
- `network_monitors`
- `zfs_pools`

For high-frequency history collections:

- `system_stats`
- `container_stats`
- `network_monitor_stats`

it does **not** execute one historical query per system/monitor. Instead, it scans newest `type="1m"` records in bounded pages and keeps the first record for each relation ID.

**Only fresh history is requested from PocketBase:** `system_stats` and `container_stats` use `created >= now - MAX_STATS_AGE_SECONDS` (default 180 seconds), while `network_monitor_stats` uses `created >= now - MAX_MONITOR_STATS_AGE_SECONDS` (default 600 seconds). The server-side filter uses a PocketBase **date string** for `system_stats` and `container_stats`, but a **numeric Unix millisecond timestamp** for `network_monitor_stats.created` (Beszel's custom number field). The exporter checks exact record age after retrieval. A date-string filter on `network_monitor_stats` returns no valid history, suppressing `window="1m_min"`, `"1m_max"`, `"1m_avg"` and probe counts.

With normal rates and active systems this usually means approximately **three API requests** (one per history collection) per refresh. Offline hosts and disabled monitors do not cause historical pages to be scanned indefinitely, and no warning is generated solely because their IDs have no fresh records. A warning is emitted only if the fresh-result scan reaches the configured page limit while IDs remain unaccounted for. When fresh volume alone exceeds `BULK_MAX_PAGES × BULK_PAGE_SIZE`, some sources can still be missing from a scrape.

**Important:** `*_stats_age_seconds` is emitted only when a recent history
record was found. Once a record exceeds its freshness window (180 seconds for
system/container, 600 seconds for network monitors, by default), the age
metric **disappears**, rather than increasing indefinitely. Consequently a
rule like `beszel_system_stats_age_seconds > 300` **never fires** with the
default 180-second cutoff. The `*_up` and identity metrics continue to
represent known host status while the Hub is reachable.

To detect a host that Beszel marks up but whose system-statistics series has
disappeared, use this PromQL expression:

```promql
beszel_system_up == 1
unless on(system_id) beszel_system_stats_age_seconds
```

Use a rule `for: 5m` (or an interval suitable for the scrape cadence) to
avoid transient alerts at startup. For gaps in container history, replace
`beszel_system_stats_age_seconds` with `beszel_container_stats_age_seconds`.
For enabled monitors, use:

```promql
(
  (
    beszel_network_monitor_enabled == 1
    and on(system_id) (beszel_system_up == 1)
  )
  and on(system_id, monitor_id)
    (beszel_network_monitor_interval_seconds < 300)
)
unless on(system_id, monitor_id) beszel_network_monitor_stats_age_seconds
```

The monitor query deliberately excludes long-interval monitors to avoid false
positives. Upstream Beszel runs monitor probes on the configured interval and
persists `network_monitor_stats` history for new probe results; an interval
greater than the freshness window can legitimately leave no `1m` record
inside the window. The example uses **`interval_seconds < 300`** with the
default `MAX_MONITOR_STATS_AGE_SECONDS=600`, restricting the alert to monitor
intervals shorter than **half** the freshness window.

On agent startup/restart, Beszel's `getStagger()` delays the first probe by
approximately **0.5–1 × interval**. A probe just before restart can already
be nearly one interval old; even with an immediate restart, the gap between
stored probe records can approach **2 × interval**. The period without fresh
history can therefore reach `max(0, 2 × interval - 600)` seconds (plus agent
downtime and scheduling/collection delays). For example, at 450 seconds, the
gap can approach 300 seconds, enough to make `for: 5m` borderline; at
540 seconds, the gap can reach 480 seconds and trigger a false alert.

Keeping the interval below half the cutoff prevents this specific restart
gap under the idealized no-downtime model. **If you change the freshness limit,
adjust `300` to half the configured value.** Prolonged agent downtime,
missed probes or other delays can still trigger the alert. A missing series
by itself does not prove a monitor failure.
See [Beszel agent scheduling](https://github.com/henrygd/beszel/blob/main/agent/network_monitor_schedule.go)
and [Hub stats persistence](https://github.com/henrygd/beszel/blob/main/internal/hub/systems/system.go).

A healthy `beszel_exporter_up == 1` should also be required in the alert
rule, or monitored separately. During Hub outages `beszel_system_up` is
intentionally absent.

The cutoff uses the **exporter's UTC clock** against timestamps written by the
Hub. Both systems need synchronized time (NTP/chrony). If the exporter clock is
ahead of the Hub by more than the configured freshness threshold, fresh Hub
records can be filtered out and dynamic series silently disappear. An exporter
clock behind the Hub can make future-dated records appear artificially fresh.
Check clock synchronization before increasing the freshness limits.

## Stale data handling

Prometheus cannot know that a value read from a persistent Beszel record is old. Without explicit handling, a stopped host could continue exporting its last CPU, memory or temperature sample as if it were current.

The exporter therefore emits:

```text
beszel_system_stats_age_seconds
beszel_container_stats_age_seconds
beszel_network_monitor_stats_age_seconds
```

Dynamic host/container/SMART/systemd/network-monitor/storage values are suppressed when the source system does not have fresh `system_stats` data.

Default freshness limit:

```text
MAX_STATS_AGE_SECONDS=180
```

The separate network-monitor history record limit defaults to:

```text
MAX_MONITOR_STATS_AGE_SECONDS=600
```

With a reachable Hub, identity metadata and `beszel_system_up` remain
available to identify down hosts. If the Hub itself becomes unavailable,
`beszel_system_up` is **not** replayed from cache: its last value no longer
represents a verified host state.

## Global scrape deadline

The exporter uses one **monotonic, per-request time budget** (default
`SCRAPE_BUDGET_SECONDS=15`), not a separate full timeout for every API call.
Every Hub request (including login and retry after HTTP 401) receives a timeout
bounded by the budget remaining and `REQUEST_TIMEOUT`. Pagination stops when
the budget is exhausted; optional collection errors cannot silently suppress a
deadline failure. The budget starts **before waiting for the collector lock**,
so concurrent scrapes cannot each wait for a prior full scan and then run a new
full scan.

On deadline exhaustion, the scrape returns `beszel_exporter_up=0`, only
last-known **identity-only `*_info` samples** (when the collector lock was
acquired), and exporter diagnostics. It never publishes partial results as successful or
replays dynamic values. The usual `FAILURE_CACHE_TTL` reduces repeated Hub
load, and the next attempt retries. If lock acquisition itself times out, only
minimal exporter diagnostics are returned, without reading concurrent
mutable collector state.

Keep this budget below `scrape_timeout` (20 seconds in the supplied vmagent
example); the default 15 seconds leaves ~5 seconds for HTTP response overhead.
**Limitation:** the Python requests timeout is a socket connect/read inactivity
limit rather than a strict total-response deadline. A server that continuously
streams bytes slowly or CPU-heavy parsing can exceed the exact wall-clock
limit inside a single operation; deadline checks stop subsequent work, but this
is a cooperative rather than forcibly preemptive deadline.

## Hub failure behavior

If the required `systems` request fails (or another error aborts the
collection), the exporter:

1. logs the detailed exception server-side;
2. replays **only allowlisted identity labels** from the last successful
   `*_info` snapshot (if any), together with current exporter metrics;
   `status`, `state`, `substate`, `health`, ports, SSIDs and other
   operational details are excluded; state-only families such as
   `beszel_storage_pool_health_info` and
   `beszel_storage_pool_scrub_info` are not replayed;
3. drops historical CPU, memory, temperatures, network counters, disk metrics,
   monitor results, `*_age_seconds`, and even the last-known
   `beszel_system_up` value;
4. sets `beszel_exporter_up=0`, retaining
   `beszel_exporter_last_success_timestamp_seconds` for freshness checks;
5. caches the degraded response briefly (`FAILURE_CACHE_TTL`, default 5 seconds).

Dynamic time series therefore disappear at the **first attempted collection
that detects the outage**, rather than appearing as flat lines for hours.
A previously successful response may still be served until its normal
`CACHE_TTL` (default 15 seconds) expires. Prometheus then marks omitted
series stale using its normal staleness handling; actual visualization depends
on the query and backend. The reduced `*_info` series represent **last-known
identity only**, not current container, service, SMART or pool health.
In addition to operational status, the container `ports` label is omitted
while host statistics are stale; it can change following redeployment or
restart.

Even with a reachable Hub, `status`, `state`, `substate` and `health`
labels from container/SMART/systemd/pool records are omitted when the source
system's statistics are stale. For hosts still marked `up` but missing
fresh system statistics, `beszel_system_info` likewise omits `status`;
`beszel_system_up` separately reflects the Hub's view of system status.
When writing state/health alerts based on `*_info`, also gate on
`beszel_exporter_up == 1` and `beszel_system_up == 1` to avoid conflating
identity metadata with verified live state. The exporter-only alerts should
still detect Hub outages independently.

Metadata is kept until replaced by the next successful collection or the
exporter restarts; no raw metric snapshot is replayed in failure mode. If the
first collection fails, the response contains only exporter self-monitoring.
The failure cache avoids a queue of Hub timeouts during an outage.

The HTTP response does **not** expose the internal exception text or Beszel URL.

## Authentication

### Recommended: dedicated readonly user

Create a dedicated Beszel user with role `readonly` and grant it access to the monitored systems.

Beszel system access and the `readonly` role are separate concepts: a readonly user still needs to be present in each system's `users` relation unless the Hub is configured with:

```text
BESZEL_HUB_SHARE_ALL_SYSTEMS=true
```

Configure the exporter with:

```env
BESZEL_USER=monitoring@example.invalid
BESZEL_PASSWORD=change-me
```

The exporter re-authenticates when a password-authenticated request receives HTTP 401.

### Static token

Alternatively:

```env
BESZEL_TOKEN=your-token
```

When `BESZEL_TOKEN` is set, username/password authentication is not used. Static-token expiry behavior depends on the Beszel/PocketBase configuration, so username/password authentication is generally safer for unattended operation.

## Quick start

```bash
git clone https://github.com/pi4-dev/beszel-metrics-exporter.git
cd beszel-metrics-exporter
cp beszel-metrics-exporter.env.example beszel-metrics-exporter.env
chmod 600 beszel-metrics-exporter.env
```

Edit `beszel-metrics-exporter.env`, then start:

```bash
docker compose up -d --build
```

Health check:

```bash
curl http://127.0.0.1:9105/healthz
```

Expected response:

```json
{"status":"ok"}
```

**Important:** `/healthz` is a *process liveness* endpoint, not a Hub
readiness check. It returns `{"status":"ok"}` even if authentication to Beszel
fails, the Hub is unavailable, or the last collection exceeded its deadline.
Docker HEALTHCHECK uses this endpoint to confirm the exporter process responds.
To monitor upstream availability and data collection, alert on
`beszel_exporter_up == 0` and the age of
`beszel_exporter_last_success_timestamp_seconds` from `/metrics`.

Metrics:

```bash
curl -s http://127.0.0.1:9105/metrics | less
```

## Environment and local Docker Compose overrides

`docker-compose.yaml` loads `beszel-metrics-exporter.env` using `env_file:`.
This file contains the Beszel credentials and exporter runtime settings. Docker
Compose passes them into the container without requiring an implicit `.env`
file or a `--env-file` CLI option.

First-time setup:

```bash
cp beszel-metrics-exporter.env.example beszel-metrics-exporter.env
chmod 600 beszel-metrics-exporter.env
# Edit beszel-metrics-exporter.env to set the Beszel URL and credentials.
docker compose up -d --build
```

If upgrading an existing deployment that used `.env`, move the existing values
once, before starting the new Compose configuration:

```bash
mv .env beszel-metrics-exporter.env
```

Both `.env` and `beszel-metrics-exporter.env` are Git-ignored and excluded from the Docker
build context. The legacy `.env` file is no longer used by the exporter's
`env_file` directive. Docker Compose may still automatically read a local
`.env` for its own interpolation if one exists; the supplied base Compose file
does not depend on it.

Create `docker-compose.override.yaml` alongside `docker-compose.yaml` for
machine-specific settings; Docker Compose merges it automatically when you run
`docker compose up -d --build`. Override files are Git-ignored.

For example, change the host port to `19105` with Docker Compose 2.24.4+:

```yaml
services:
  beszel-metrics-exporter:
    ports: !override
      - "127.0.0.1:19105:9105"
```

The `!override` tag replaces the original port mapping rather than exposing
both ports. You can also use an override to join a pre-existing monitoring
network, provided that network can reach the Beszel Hub.

## Network exposure

The supplied Compose file publishes the exporter only on loopback:

```yaml
ports:
  - "127.0.0.1:9105:9105"
```

This is intentional because the endpoint contains infrastructure metadata such as hostnames, container images, SSIDs and disk serial numbers.

If `vmagent` shares a Docker network with the exporter, the safer configuration is to remove `ports:` entirely and scrape:

```text
beszel-metrics-exporter:9105
```

from that internal network.

## vmagent

An example scrape job is provided in `vmagent-scrape.yaml`:

```yaml
scrape_configs:
  - job_name: beszel
    scrape_interval: 60s
    scrape_timeout: 20s
    static_configs:
      - targets:
          - beszel-metrics-exporter:9105
```

The exporter is independent of the remote-write target. Existing `vmagent -> OpenObserve` or `vmagent -> VictoriaMetrics` configuration does not need to change.

For Grafana/OpenObserve dashboards it is recommended to include the scrape job in selectors:

```promql
{job="beszel"}
```

This prevents dashboard variables from mixing label values from unrelated jobs.

### Verify stale-series handling in OpenObserve

A transition from `beszel_systemd_service_info{...,state="active"}` to
`beszel_systemd_service_info{...}` (no `state` label) creates two distinct
Prometheus series. Prometheus/vmagent scraping sends the old series'
**staleness marker** downstream via `remote_write` when staleness tracking is
enabled. For vmagent, verify that neither `-promscrape.noStaleMarkers`
nor `no_stale_markers: true` is set. See the
[vmagent staleness documentation](https://docs.victoriametrics.com/victoriametrics/vmagent/#prometheus-staleness-markers).
The remote-write protocol defines a special StaleNaN value
(`0x7ff0000000000002`), but
support for ingesting remote-write samples is **not itself proof** that a
particular OpenObserve version uses stale markers correctly during PromQL
evaluation. That behavior has **not been verified against a running
OpenObserve instance**.

To validate your installed version:

1. Query `beszel_systemd_service_info{job="beszel",system_id="<id>",service="<service>"}`
   in both OpenObserve and a reference Prometheus engine.
2. Make the source's system statistics stale (or stop the Beszel Hub),
   then wait at least one successful scrape of the exporter.
3. Repeat an **instant-vector** query across the transition. The old
   `state="active"` series must stop appearing after its stale marker;
   only the reduced-label identity series should remain.
4. Test your dashboard's real `on(...)`/`group_left(...)` join. If the
   old and new series coexist, the join can fail with a many-to-many
   matching error. Compare the query outcome against reference Prometheus.

Until that check passes, avoid matching changing `*_info` labels in
critical alerts; use stable IDs such as `system_id` and `service`,
plus explicit exporter/source freshness checks. This is a receiver/query
compatibility test, not something the exporter can enforce by including
more labels.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `BESZEL_URL` | `http://beszel:8090` | Beszel Hub URL from the exporter container |
| `BESZEL_USER` | empty | Beszel user email |
| `BESZEL_PASSWORD` | empty | Beszel password |
| `BESZEL_TOKEN` | empty | Optional PocketBase auth token |
| `REQUEST_TIMEOUT` | `10` | HTTP timeout per Hub request |
| `SCRAPE_BUDGET_SECONDS` | `15` | Maximum collection budget (seconds), including collector-lock waiting and all Hub requests; keep below scraper timeout |
| `CACHE_TTL` | `15` | Successful full-scrape cache TTL |
| `FAILURE_CACHE_TTL` | `5` | Negative-cache TTL after a required Hub request fails |
| `MAX_STATS_AGE_SECONDS` | `180` | Maximum age of dynamic host data |
| `MAX_MONITOR_STATS_AGE_SECONDS` | `600` | Maximum age of `network_monitor_stats` used for 1m aggregates |
| `BULK_PAGE_SIZE` | `500` | Page size for bulk newest-record scans |
| `BULK_MAX_PAGES` | `5` | Maximum pages scanned per bulk history collection |
| `LEGACY_UNITS` | `false` | Emit deprecated MiB/s and millisecond-await metric names |
| `LOG_LEVEL` | `INFO` | Python log level |
| `LISTEN_HOST` | `0.0.0.0` | Gunicorn bind address |
| `LISTEN_PORT` | `9105` | Gunicorn bind port |

Unlike the initial implementation, `LISTEN_HOST` and `LISTEN_PORT` are used by both direct Python execution and the production Gunicorn container through `gunicorn.conf.py`.
When using Docker Compose, the `environment:` values in `docker-compose.yaml` take precedence over `env_file:`, so change `LISTEN_HOST` and `LISTEN_PORT` in `docker-compose.override.yaml` and update `ports:` there as needed (editing only `beszel-metrics-exporter.env` will not change them).

## Metric conventions

All metrics use the `beszel_` prefix.

### Base units

Prometheus base units are used by default:

- capacity / memory: bytes
- throughput: bytes/second
- latency / await: seconds
- temperatures: Celsius
- percentages: percentage points (`0..100`)
- timestamps: Unix seconds

Beszel fields stored as GiB, MiB, microseconds or milliseconds are converted by the exporter.

Older Beszel rate fields expressed in MiB/s are converted to bytes/s when newer byte-based fields are unavailable. Their original `*_mib_per_second` metric names are emitted only with:

```env
LEGACY_UNITS=true
```

The same flag temporarily restores deprecated `*_await_milliseconds` and
`beszel_smart_power_on_hours_total` series. The default SMART power-on lifetime
counter is `beszel_smart_power_on_seconds_total` (Beszel's `hours` field
multiplied by 3600). Existing queries for `*_hours_total` should migrate to
the new name or opt in to `LEGACY_UNITS=true` temporarily.

### Label cardinality

Fast-changing metadata is intentionally isolated in `*_info` metrics.

For example, with **fresh host statistics**, SMART metadata can include
a last-known operational state:

```text
beszel_smart_device_info{system_id="sys1",device="/dev/sda",serial="abc",model="Disk",firmware="1.0",state="PASSED"} 1
```

When the host's statistics are **stale**, the same `*_info` family omits the
unverified `state` label:

```text
beszel_smart_device_info{system_id="sys1",device="/dev/sda",serial="abc",model="Disk",firmware="1.0"} 1
```

During **Hub outage fallback**, only allowlisted identity labels survive
(`system`, `system_id`, `device`, `serial` for SMART), and `model` and
`firmware` are not replayed. Numeric SMART series use stable device/serial
identity labels, never model or firmware.

Container image, container ID, status and port strings are similarly restricted
to `beszel_container_info`. `status` and `ports` are included only when
the host has fresh system statistics, and the Hub outage fallback retains only
identity labels. Such changes in the label set create distinct Prometheus
series; see the OpenObserve stale-marker verification instructions above.

### Lifecycle metadata label cardinality

Beszel stores Docker container status as **display text** (for example
`Up 2 minutes`, `Up 3 hours`, or `Exited (137) 20 seconds ago`). Passing that
text directly as a Prometheus label creates a new time series whenever the
uptime changes.

`beszel_container_info{status="..."}` now uses bounded lifecycle states:
`running`, `exited`, `restarting`, `paused`, `created`, `dead`, `removing`,
or `unknown`. A Docker health suffix (such as `(healthy)` or `(unhealthy)`)
does not change the lifecycle status; `beszel_container_health_state` provides
the separate numerical health signal. Unrecognized status strings become
`unknown` rather than arbitrary new label values.

`beszel_system_info.status` uses only `up`, `down`, `paused`, `pending`, or
`unknown`. `beszel_systemd_service_info` uses enumerated `state`/`substate`
labels with `unknown` for unmapped codes; state transitions may create a
bounded number of additional series but cannot include arbitrary uptime text.

**Compatibility:** dashboards filtering for `status=~"Up.*"` or exact
`status="Up ..."` values must switch to `status="running"`. Historical
raw-status series remain in the metrics backend until its configured
retention expires; this update prevents new uptime-derived series.
## Container network metrics

TX/RX use:

```text
beszel_container_network_bytes_per_second{direction="transmit"}
beszel_container_network_bytes_per_second{direction="receive"}
```

The aggregate value from the current Beszel container record is separate:

```text
beszel_container_network_combined_bytes_per_second
```

There is intentionally no `direction="total"` sample in the TX/RX family, because summing the old family without filtering `direction` double-counted traffic.

## Package-update metrics

Beszel reports pending updates as `pu=[totalUpdates, securityUpdates]`;
`securityUpdates` is a subset of `totalUpdates` and may be omitted if its
classification is unknown. The exporter exposes **two separate gauges**:
`beszel_package_updates_pending` (all pending updates) and
`beszel_package_security_updates_pending` (security only, **absent when
unknown**). Both have only the normal system labels; neither uses a
`type` label. Do not add these two overlapping counts together. When
security is greater than total (inconsistent source data), security is
suppressed. Migrate `beszel_package_updates_pending{type="all"}` to
`beszel_package_updates_pending` and
`beszel_package_updates_pending{type="security"}` to
`beszel_package_security_updates_pending`.

Upstream definition: [Beszel system Info.PackageUpdates](https://github.com/henrygd/beszel/blob/main/internal/entities/system/system.go).

## Network-monitor metrics

Important series include:

```text
beszel_network_monitor_enabled
beszel_network_monitor_interval_seconds
beszel_network_monitor_response_seconds
beszel_network_monitor_packet_loss_percent
beszel_network_monitor_probes
beszel_network_monitor_stats_age_seconds
beszel_network_monitor_tls_cert_expiry_timestamp_seconds
beszel_network_monitor_tls_cert_info
```

`beszel_network_monitor_probes` is a **gauge** for counts in the latest
1-minute history aggregate, not a cumulative counter. It uses **mutually
exclusive** `result="success"` and `result="failure"` values, both with
`window="1m"`. Failed probes are calculated as `total_count - success_count`.
These values can safely be summed across `result` to recover the total number
of probes for each monitor/window:

```promql
sum without (result) (beszel_network_monitor_probes{window="1m"})
```

Do not use `rate()`/`increase()` on this gauge. The prior
`result="total"` overlapped with `result="success"`, so summing those
older series would double-count successful probes. Update any PromQL queries
or Grafana panels selecting `result="total"` to sum the new disjoint
categories instead. The former `beszel_network_monitor_probe_count` name
was invalid for a non-histogram/non-summary metric in
`promtool check metrics`; the old series is no longer emitted.

Common labels:

```text
system
system_id
monitor_id
target
protocol
port
server
```

`beszel_network_monitor_interval_seconds` uses the value of
`network_monitors.interval` **in seconds**. Upstream,
[the Hub maps the integer interval into the agent configuration](https://github.com/henrygd/beszel/blob/main/internal/hub/network_monitors.go),
and [the agent multiplies it by `time.Second`](https://github.com/henrygd/beszel/blob/main/agent/network_monitor_schedule.go).
When the stored interval is **0, missing or invalid**, the exporter reports
the **effective default of 30 seconds**, matching the agent's fallback for
intervals below one second. Other valid positive intervals are exported
unchanged. The alert filter `< 300` above also uses seconds.

Latency windows include, where available:

```text
current
1m_min
1m_avg
1m_max
1h_min
1h_avg
1h_max
```

**Missing latency at 100% packet loss is intentional:** Beszel reports zero
response times when no probe succeeds, but the exporter does not interpret
these zeros as valid 0-second latency. The `1m_min` and `1m_max`
series require `success_count > 0`; `1m_avg` also requires a present,
numeric `res_sum` value. Missing `res_sum` must not be treated as 0 s.
Missing `total_count` or `success_count` suppresses probe counts and
1m packet loss instead of fabricating zero-valued observations.
`current` requires `res > 0`.
The `1h_*` windows require `loss1h < 100` and a positive response-time
value. Probe counts and packet-loss percentages remain available even when
latency series are suppressed.

Packet-loss windows include:

```text
current
1m
1h
```

### TLS days remaining

`beszel_network_monitor_tls_cert_days_remaining` is intentionally no longer exported because it can be derived without creating another stored time series:

```promql
(beszel_network_monitor_tls_cert_expiry_timestamp_seconds - time()) / 86400
```

## SMART metrics

Numeric SMART series do not include `model` or `firmware` labels. Those fields are present only on `beszel_smart_device_info`, preventing a firmware upgrade from creating new copies of every SMART attribute time series.

SMART power-on time is exported as seconds (`beszel_smart_power_on_seconds_total`).
The former hours-based series is optional under `LEGACY_UNITS=true`.

SMART raw human-readable strings are not exported as labels.

## GPU metrics

GPU numeric series (`beszel_gpu_usage_percent`,
`beszel_gpu_memory_used_bytes`, `beszel_gpu_memory_total_bytes`,
`beszel_gpu_power_watts`, `beszel_gpu_package_power_watts`, and
`beszel_gpu_engine_usage_percent`) use stable `system`, `system_id`,
`gpu` labels (plus `engine` for per-engine usage).
The descriptive Beszel GPU name/model field `g[*].n` is exported only on
`beszel_gpu_info{gpu="...",name="..."}`, not on numeric series.
Queries that used `name` on numeric GPU metrics must join the info series
or display the GPU identifier instead.

## ZFS / Btrfs pool metrics

Two **distinct source representations** are intentionally exposed; they
should not be treated as interchangeable or summed together:

| Metric family | Beszel source | Fields and unit handling |
| --- | --- | --- |
| `beszel_storage_pool_total_bytes`, `beszel_storage_pool_used_bytes` | Recent `system_stats.stats.z` | `d` and `du` values supplied in GiB; converted to bytes |
| `beszel_storage_pool_read_bytes_per_second`, `beszel_storage_pool_write_bytes_per_second` | Recent `system_stats.stats.z` | `rb`, `wb` rates, already in bytes/s |
| `beszel_storage_pool_size_bytes`, `beszel_storage_pool_allocated_bytes`, `beszel_storage_pool_free_bytes` | `zfs_pools` collection | `size`, `alloc`, `free` values used directly, expected in bytes |
| `beszel_storage_pool_vdev_*`, `beszel_storage_pool_dataset_*`, `beszel_storage_pool_scrub_*` | `zfs_pools` collection | Vdev errors, dataset sizes and scrub state |

`total_bytes` and `size_bytes` can look similar but may represent
different source updates and accounting semantics. Use one source consistently
in a dashboard; for pool capacity use `size_bytes`, `allocated_bytes` and
`free_bytes` together, and prefer the `stats.z` series for live I/O.
`zfs_pools` collection sizes are not implicitly recomputed from
`system_stats`, and missing optional collection data does not suppress
fresh `stats.z` samples.

## Duplicate sample handling

A repeated Prometheus series (the same metric name and label set), such as two
container records with the same name, a `bats.primary` and `bat` value in one
system, or duplicate ZFS dataset entries, no longer aborts the entire collection.
The exporter retains the **first emitted value**, drops subsequent duplicates,
and logs a warning once per metric family per **exporter process lifetime**
(not once per scrape or per drop). The warning contains the first affected
`system_id` when available; every dropped duplicate also writes a DEBUG
message with its metric family and `system_id`, helping identify additional
affected hosts when `LOG_LEVEL=DEBUG`. No container names, targets, serial
numbers, SSIDs or other potentially sensitive label values are logged.
DEBUG logging is opt-in and can be verbose if duplicates persist.

`beszel_exporter_dropped_samples_total` is a **cumulative counter** of all
dropped duplicates since the exporter process started. It increments only when
a new collection runs; responses served from `CACHE_TTL` or
`FAILURE_CACHE_TTL` do not increment it again. Even if a separate later error
forces metadata-only fallback, the cumulative counter is retained.

This counter has no labels, avoiding a second cardinality problem. The
following PromQL shows the number of drops over a period:

```promql
increase(beszel_exporter_dropped_samples_total[1h])
```

A non-zero result means source data or exporter label mapping should be
investigated: conflicting data is *discarded*, not merged or summed.
## Exporter self-monitoring

```text
beszel_exporter_up
beszel_exporter_scrape_duration_seconds
beszel_exporter_last_success_timestamp_seconds
beszel_exporter_collection_errors_total{collection="..."}
beszel_exporter_history_records_wanted{collection="..."}
beszel_exporter_history_records_found{collection="..."}
beszel_exporter_dropped_samples_total
```

The two `beszel_exporter_history_records_*` metrics are **gauges**, with
exactly three `collection` label values: `system_stats`, `container_stats`,
and `network_monitor_stats`. `wanted` counts distinct system IDs (or monitor
IDs for network-monitor history) and `found` counts distinct IDs with a
matching recent `type="1m"` record. Both are emitted on every successful
collection, including `0` when no source IDs exist or no rows were found.
No host, system, or monitor labels are used.

Compare the two numbers to detect unexpected loss of history, for example:

```promql
beszel_exporter_history_records_wanted{collection="network_monitor_stats"}
-
beszel_exporter_history_records_found{collection="network_monitor_stats"}
```

A gap alone is **not proof of a broken query**: offline systems and disabled
or infrequently probed monitors may have no fresh history. A historical
query failure also produces `found=0` but increments
`beszel_exporter_collection_errors_total{collection="..."}`; inspect
exporter logs. If the entire scrape fails, the history gauges are omitted
rather than replayed from an earlier successful scrape.

`beszel_exporter_collection_errors_total` increments when an optional
Beszel collection cannot be read. Detailed causes are written to exporter logs.

## Breaking changes from the initial version

| Old behavior / metric | New behavior |
| --- | --- |
| `*_await_milliseconds` | `*_await_seconds` by default; old names require `LEGACY_UNITS=true` |
| Always exported `*_mib_per_second` | disabled by default; byte/second metrics are preferred |
| `beszel_container_network_bytes_per_second{direction="total"}` | replaced by `beszel_container_network_combined_bytes_per_second` |
| SMART `model` / `firmware` on every numeric series | metadata moved to `beszel_smart_device_info` |
| `beszel_smart_power_on_hours_total` | `beszel_smart_power_on_seconds_total` by default; hours series requires `LEGACY_UNITS=true` |
| GPU `name` model label on numeric metrics | model moved to `beszel_gpu_info`; numeric series keyed by stable GPU identifier |
| Missing / invalid systemd `state` treated as inactive | `state="unknown"`; omit active/failed gauges until state is known |
| `beszel_network_monitor_probe_count` | renamed to `beszel_network_monitor_probes` (`gauge` with disjoint `result="success"/"failure"` and `window="1m"`); replace `result="total"` queries with `sum without(result)` |
| `beszel_network_monitor_probes{result="total"/"success"}` | replaced overlapping values with mutually exclusive `success`/`failure` buckets; old `result="total"` is removed |
| `beszel_systemd_services_total` | renamed to `beszel_systemd_services` (gauge counting currently reported services); update PromQL queries, alerts and dashboards |
| `beszel_package_updates_pending{type="all"/"security"}` | replaced with separate gauges `beszel_package_updates_pending` (without `type`) and `beszel_package_security_updates_pending`; do not sum these overlapping counts |
| `beszel_network_monitor_tls_cert_days_remaining` | calculate from expiry timestamp in PromQL |
| Persistent last host values exported indefinitely | stale dynamic metrics are suppressed |
| Raw container status like `Up 2 hours` on `beszel_container_info` | normalized to bounded lifecycle states such as `running` or `exited` |

## Metric exposition format

Metric samples are grouped by family and every family contains both metadata lines:

```text
# HELP beszel_system_cpu_usage_percent ...
# TYPE beszel_system_cpu_usage_percent gauge
beszel_system_cpu_usage_percent{...} 12.3
```

The collector rejects duplicate name+label samples before rendering. CI also validates mocked exporter output with `promtool check metrics`.

## Gunicorn control socket

The production container runs Gunicorn with one worker and a read-only
filesystem. Its interactive control interface is unnecessary, so
`gunicorn.conf.py` sets `control_socket_disable = True` (Gunicorn 25.1+).
Process management and restarts remain the responsibility of Docker.

## Development

Create a virtual environment and install the pinned development dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

Run lint and tests:

```bash
ruff check .
pytest -q
```

Validate representative Prometheus output:

```bash
PYTHONPATH=.:tests python tests/render_mock_metrics.py > /tmp/beszel.metrics
docker run --rm -i --entrypoint /bin/promtool \
  prom/prometheus:v3.15.0 check metrics < /tmp/beszel.metrics
```

Build the container:

```bash
docker build -t beszel-metrics-exporter .
```

## CI

CI runs on pull requests and pushes to `main` (avoiding duplicate push+PR runs for feature branches) and performs:

1. Ruff linting
2. pytest unit tests (including `test_metric_mappings.py` and
   `test_metric_coverage.py`)
3. mocked metric generation using a **full mapping fixture**
4. Docker Compose configuration validation (including `env_file` precedence,
   local override merging and published loopback port checks)
5. Docker image build and check of the effective Gunicorn configuration
   (`control_socket_disable = True`)
6. container startup smoke test, `/healthz` check and degraded `/metrics`
   check (`beszel_exporter_up 0`)
7. Docker HEALTHCHECK status verification (must reach `healthy`)
8. `promtool check metrics` for the full mocked exposition

The coverage contract discovers every statically named `beszel_*` family
in `beszel_exporter.py`, expands the four dynamic disk-I/O prefixes into
their metric names, and checks that the full mock scrape actually emits
them all. Only explicitly enumerated `LEGACY_UNITS` metric names are
excluded from default-mode coverage. The fixture performs a successful
scrape after a synthetic optional collection failure, so exporter error
counters and `beszel_exporter_last_success_timestamp_seconds` are also
covered. Adding a new metric without extending the fixture fails pytest;
`promtool` then checks the generated exposition for naming/type errors.

GitHub Actions are pinned to commit SHAs.

## Container publishing

The `Publish container` workflow publishes multi-architecture images for:

```text
linux/amd64
linux/arm64
```

to:

```text
ghcr.io/pi4-dev/beszel-metrics-exporter
```

`main` publishes `latest` and SHA tags; `v*` Git tags also publish a matching version tag.

## Dependency management

Runtime dependencies are pinned in `requirements.lock`; development tools are pinned in `requirements-dev.txt`. Dependabot is configured for Python, Docker and GitHub Actions.

## Repository layout

```text
.
├── beszel-metrics-exporter.env.example
├── .github/
│   ├── dependabot.yml
│   └── workflows/
│       ├── ci.yml
│       └── publish.yml
├── Dockerfile
├── README.md
├── beszel_exporter.py
├── docker-compose.yaml
├── gunicorn.conf.py
├── pyproject.toml
├── pytest.ini
├── requirements.in
├── requirements.lock
├── requirements.txt
├── requirements-dev.txt
├── tests/
│   ├── conftest.py
│   ├── metric_coverage_fixture.py
│   ├── render_mock_metrics.py
│   ├── test_exporter.py
│   ├── test_metric_coverage.py
│   └── test_metric_mappings.py
└── vmagent-scrape.yaml
```

## Upstream compatibility

This project consumes Beszel's Hub/PocketBase data model rather than an official Beszel Prometheus endpoint. Collection schemas can change between Beszel releases.

When upgrading Beszel, verify:

```bash
curl -fsS http://127.0.0.1:9105/metrics >/tmp/beszel.metrics
grep '^beszel_exporter_up ' /tmp/beszel.metrics
grep '^beszel_exporter_collection_errors_total' /tmp/beszel.metrics || true
```

and review exporter logs for collection/schema errors.

## Upstream project

Beszel: https://github.com/henrygd/beszel

This exporter is independent and is not part of the upstream Beszel project.