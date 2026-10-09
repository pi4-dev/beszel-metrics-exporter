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

it does **not** execute one historical query per system/monitor. Instead, it scans newest `type="1m"` records in bounded pages and keeps the first record for each relation ID. With normal active systems this usually reduces the history lookup to approximately three API requests per exporter refresh instead of multiple requests per system and monitor.

`BeszelAPI.latest()` is also implemented as a true one-record query using `perPage=1`, `sort=-created`, and `skipTotal=1`; it does not traverse all pages.

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

## Hub failure behavior

If the required `systems` request fails (or another error aborts the
collection), the exporter:

1. logs the detailed exception server-side;
2. serves **only `*_info` metric families from the last successful collection**
   (if any), together with current exporter self-monitoring metrics;
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
on the query and backend. `*_info` describes **last-known metadata**, not
necessarily current container or systemd runtime state.

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

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `BESZEL_URL` | `http://beszel:8090` | Beszel Hub URL from the exporter container |
| `BESZEL_USER` | empty | Beszel user email |
| `BESZEL_PASSWORD` | empty | Beszel password |
| `BESZEL_TOKEN` | empty | Optional PocketBase auth token |
| `REQUEST_TIMEOUT` | `10` | HTTP timeout per Hub request |
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

The same flag temporarily restores deprecated `*_await_milliseconds` series while the default metrics use `*_await_seconds`.

### Label cardinality

Fast-changing metadata is intentionally isolated in `*_info` metrics.

For example, SMART metadata:

```text
beszel_smart_device_info{device=...,serial=...,model=...,firmware=...,state=...} 1
```

while numeric SMART metrics carry only stable device identity labels such as `device` and `serial`.

Container image, container ID, status and port strings are similarly restricted to `beszel_container_info`.

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

## Network-monitor metrics

Important series include:

```text
beszel_network_monitor_enabled
beszel_network_monitor_interval_seconds
beszel_network_monitor_response_seconds
beszel_network_monitor_packet_loss_percent
beszel_network_monitor_probe_count
beszel_network_monitor_stats_age_seconds
beszel_network_monitor_tls_cert_expiry_timestamp_seconds
beszel_network_monitor_tls_cert_info
```

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

SMART raw human-readable strings are not exported as labels.

## Duplicate sample handling

A repeated Prometheus series (the same metric name and label set), such as two
container records with the same name, a `bats.primary` and `bat` value in one
system, or duplicate ZFS dataset entries, no longer aborts the entire collection.
The exporter retains the **first emitted value**, drops subsequent duplicates,
and logs a warning once per metric family per collection (not once per drop).
It never logs the potentially sensitive label values in these warnings.

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
beszel_exporter_dropped_samples_total
```

`beszel_exporter_collection_errors_total` increments when an optional Beszel collection cannot be read. Detailed causes are written to exporter logs.

## Breaking changes from the initial version

| Old behavior / metric | New behavior |
| --- | --- |
| `*_await_milliseconds` | `*_await_seconds` by default; old names require `LEGACY_UNITS=true` |
| Always exported `*_mib_per_second` | disabled by default; byte/second metrics are preferred |
| `beszel_container_network_bytes_per_second{direction="total"}` | replaced by `beszel_container_network_combined_bytes_per_second` |
| SMART `model` / `firmware` on every numeric series | metadata moved to `beszel_smart_device_info` |
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
2. pytest unit tests
3. mocked metric generation
4. `promtool check metrics`
5. Docker build
6. container startup smoke test
7. `/healthz` check
8. failure-mode `/metrics` check

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
│   ├── render_mock_metrics.py
│   └── test_exporter.py
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