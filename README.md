# Beszel Metrics Exporter

A Prometheus-compatible metrics exporter for [Beszel](https://github.com/henrygd/beszel).

The exporter reads current monitoring data from the Beszel Hub / PocketBase API and exposes it on `/metrics`. It is intended for environments where `vmagent`, Prometheus, VictoriaMetrics, OpenObserve, or another Prometheus-compatible component already handles scraping and/or `remote_write`.

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
```

The exporter does not require changes to Beszel agents and does not write anything to Beszel.

## Exported data

The exporter reads the following Beszel collections when available:

- `systems`
- `system_details`
- `system_stats`
- `containers`
- `container_stats`
- `smart_devices`
- `systemd_services`
- `network_monitors`
- `network_monitor_stats`
- `zfs_pools`

Optional collections are handled independently. If an optional collection is unavailable or inaccessible, the exporter continues serving the remaining metrics and increments `beszel_exporter_collection_errors_total`.

### Host metrics

- Host availability and uptime
- CPU usage and peak CPU usage
- Per-core CPU usage
- CPU user/system/iowait/steal/idle breakdown
- Load average: 1, 5 and 15 minutes
- Total/used memory and memory utilization
- Buffer/cache memory
- ZFS ARC memory
- Swap total/used
- Root filesystem total/used/utilization
- Disk read/write throughput
- Disk cumulative read/write counters
- Disk utilization
- Read/write await latency
- Weighted I/O
- Network transmit/receive throughput
- Per-interface transmit/receive throughput
- Per-interface cumulative byte counters
- Temperatures
- Fan speeds
- Battery state and percentage
- Wi-Fi RSSI
- Package update counts
- systemd service summary
- Agent and host information

### Extra filesystems

For filesystems collected by Beszel, the exporter exposes:

- Total and used space
- Utilization percentage
- Read/write throughput
- Read/write cumulative counters
- Read/write peak throughput when present
- I/O utilization and await statistics

### GPU metrics

- GPU utilization
- GPU memory used/total
- GPU power
- Package power where available
- Per-engine utilization where provided by Beszel

### Docker / Podman containers

- CPU usage
- Memory usage
- Aggregate network throughput
- TX/RX network throughput from `container_stats`
- Health state
- Update availability
- Container metadata through an `*_info` metric

Container image, ID, status and port data are kept on the single `beszel_container_info` series instead of being duplicated across every container metric.

### SMART

- SMART overall state
- Capacity
- Temperature
- Power-on hours
- Power cycles
- Normalized SMART attribute value
- Worst value
- Threshold
- Raw numeric value

SMART raw strings are intentionally not exported as metric labels. Values such as changing human-readable power-on-time strings would otherwise create unnecessary time-series cardinality.

### systemd services

- Active/failed state
- Main state and substate in `beszel_systemd_service_info`
- CPU current/peak
- Memory current/peak

### Network monitors

- Enabled state and configured interval
- Current latency
- 1-hour average/min/max latency
- 1-minute average/min/max latency derived from `network_monitor_stats`
- Current, 1-hour and 1-minute packet loss
- Probe counts
- TLS certificate expiration timestamp
- TLS certificate days remaining
- Certificate issuer

Beszel stores network monitor response time in microseconds. The exporter converts it to seconds to follow Prometheus naming conventions.

### ZFS / Btrfs storage pools

- Pool size/allocated/free
- Health
- Read/write throughput from current system statistics
- Scrub state/progress/errors
- Vdev state and read/write/checksum errors
- Dataset used/available space

Beszel identifies Btrfs pools with its internal `b:` pool-name prefix. The exporter exposes a `pool_type` label with either `zfs` or `btrfs`.

## What is not exported

### Arbitrary OS processes

Beszel does not currently persist a generic per-process dataset containing PID, command, user, CPU, RSS, VSZ and per-process I/O in the Hub collections used by this exporter.

`systemd_services` is exported because Beszel does collect those service-level metrics.

If arbitrary process metrics are required, run a dedicated Prometheus process exporter as another `vmagent` scrape target instead of trying to synthesize data that Beszel does not expose.

## Metric naming

All metrics use the `beszel_` prefix.

Common labels:

| Label | Meaning |
| --- | --- |
| `system` | Beszel system name |
| `system_id` | Beszel/PocketBase system record ID |
| `container` | Container name |
| `interface` | Network interface name |
| `filesystem` | Extra filesystem identifier |
| `gpu` | GPU identifier |
| `device` | SMART device name |
| `service` | systemd service name |
| `monitor_id` | Network monitor record ID |
| `target` | Network monitor target |
| `pool` | Storage pool identifier |

Examples:

```text
beszel_system_cpu_usage_percent{system="nas01",system_id="..."} 7.12

beszel_system_cpu_time_percent{mode="iowait",system="nas01",system_id="..."} 0.42

beszel_system_network_interface_bytes_per_second{direction="receive",interface="eth0",system="nas01",system_id="..."} 24812

beszel_container_cpu_usage_percent{container="immich-server",system="nas01",system_id="..."} 3.81

beszel_smart_temperature_celsius{device="/dev/sda",system="nas01",system_id="..."} 37

beszel_network_monitor_response_seconds{protocol="icmp",target="1.1.1.1",window="1m_avg",system="nas01",system_id="..."} 0.0124
```

## Unit normalization

The exporter normalizes several Beszel values to Prometheus-friendly base units:

| Beszel source | Exported unit |
| --- | --- |
| Host memory / filesystem capacity stored as GiB | bytes |
| Container and GPU memory stored as MiB | bytes |
| Network monitor response time stored as microseconds | seconds |
| Network monitor certificate expiration stored as milliseconds since epoch | seconds since epoch |
| Per-interface network rate | bytes/second |
| Disk I/O rate | bytes/second |

Legacy Beszel MiB/s fields are retained with `_mib_per_second` in their metric names so the unit is explicit.

## Requirements

- A reachable Beszel Hub
- A Beszel user that can read monitored systems and the related collections
- Docker / Docker Compose for the provided deployment example
- A Prometheus-compatible scraper such as `vmagent`

Python 3.13 is used by the supplied container image.

## Authentication

Two authentication modes are supported.

### Username and password

This is the recommended option for long-running deployments. Create a dedicated read-only Beszel account and configure:

```env
BESZEL_USER=monitoring@example.invalid
BESZEL_PASSWORD=change-me
```

The exporter re-authenticates automatically after an HTTP 401 response.

### Existing PocketBase token

Alternatively:

```env
BESZEL_TOKEN=your-token
```

When `BESZEL_TOKEN` is set, username/password authentication is not used. A static token may expire depending on the Beszel/PocketBase configuration, so username/password authentication is normally preferable for an unattended exporter.

## Quick start

Clone the repository:

```bash
git clone https://github.com/pi4-dev/beszel-metrics-exporter.git
cd beszel-metrics-exporter
```

Create the local environment file:

```bash
cp .env.example .env
```

Edit `.env` and configure at least:

```env
BESZEL_URL=http://beszel:8090
BESZEL_USER=monitoring@example.invalid
BESZEL_PASSWORD=change-me
```

Build and start:

```bash
docker compose up -d --build
```

Check health:

```bash
curl http://127.0.0.1:9105/healthz
```

Expected response:

```json
{"status":"ok"}
```

Check metrics:

```bash
curl -s http://127.0.0.1:9105/metrics | less
```

## vmagent integration

An example is included in `vmagent-scrape.yaml`:

```yaml
scrape_configs:
  - job_name: beszel
    scrape_interval: 60s
    scrape_timeout: 20s

    static_configs:
      - targets:
          - beszel-metrics-exporter:9105
```

Merge that job into the existing `vmagent` scrape configuration.

The exporter is intentionally independent of the `remote_write` destination. Existing `vmagent -> OpenObserve`, `vmagent -> VictoriaMetrics`, or other remote-write configuration can remain unchanged.

## OpenObserve topology

A typical deployment is:

```text
Beszel Hub
    |
    v
Beszel Metrics Exporter
    |
    v
vmagent
    |
    +---- remote_write ----> OpenObserve
```

This avoids adding a second telemetry collector to every monitored host: Beszel agents continue collecting the host/container/storage data, while this exporter converts the Hub data into Prometheus time series.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `BESZEL_URL` | `http://beszel:8090` | Beszel Hub URL visible from the exporter |
| `BESZEL_USER` | empty | Beszel/PocketBase user |
| `BESZEL_PASSWORD` | empty | Beszel/PocketBase password |
| `BESZEL_TOKEN` | empty | Optional existing auth token |
| `REQUEST_TIMEOUT` | `10` | HTTP request timeout in seconds |
| `CACHE_TTL` | `15` | Exporter result cache in seconds |
| `LISTEN_HOST` | `0.0.0.0` | Flask development-server bind address; Gunicorn binds separately in Docker |
| `LISTEN_PORT` | `9105` | Flask development-server port |
| `EXPORTER_PORT` | `9105` | Host port used by the supplied Compose file |

## Caching

A single `/metrics` request can query several Beszel collections. To avoid repeatedly hitting the Hub when multiple scrapers request metrics at nearly the same time, the complete exposition is cached for `CACHE_TTL` seconds.

For the provided 60-second `vmagent` scrape interval, a 15-second cache is a reasonable default.

## Cardinality considerations

The exporter deliberately avoids placing fast-changing values into labels.

Examples:

- SMART raw strings are not labels.
- Container image, ID, status and port information are isolated in `beszel_container_info`.
- Numeric health, CPU, memory and network values remain metric samples, not labels.

Some dimensions are inherently cardinality-producing, including SMART attributes, GPU engines, filesystems, network interfaces, systemd services and network monitor targets. Review retention and cardinality limits when monitoring many Beszel systems.

## Security

- Use a dedicated read-only Beszel account.
- Do not commit `.env`; it is ignored by `.gitignore`.
- Keep the exporter on a trusted monitoring network.
- `/metrics` is not authenticated by the exporter.
- If `vmagent` runs on the same host, consider binding the published port to loopback instead of all interfaces.

For a same-host deployment, change the Compose port mapping to:

```yaml
ports:
  - "127.0.0.1:9105:9105"
```

## Exporter self-monitoring

The exporter exposes:

- `beszel_exporter_up`
- `beszel_exporter_scrape_duration_seconds`
- `beszel_exporter_last_success_timestamp_seconds`
- `beszel_exporter_collection_errors_total{collection="..."}`

`beszel_exporter_collection_errors_total` is useful when Beszel adds/removes a collection, access rules change, or a read-only account cannot access a specific dataset.

## Troubleshooting

### HTTP 401 / 403 from Beszel

Verify credentials and collection access. A dedicated account must be able to list the systems it is expected to export.

### `/metrics` returns HTTP 500

Check exporter logs:

```bash
docker compose logs -f beszel-metrics-exporter
```

Then test Beszel reachability from the exporter network.

### Some metric groups are missing

Not every Beszel installation has every feature enabled. For example, SMART, ZFS/Btrfs, systemd, GPU data, Wi-Fi, or network monitors may legitimately be absent.

Check:

```bash
curl -s http://127.0.0.1:9105/metrics | grep beszel_exporter_collection_errors
```

### vmagent target is down

Verify the exporter from the `vmagent` network namespace / container network and check the target address in `vmagent-scrape.yaml`.

## Development

Run directly:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export BESZEL_URL=http://127.0.0.1:8090
export BESZEL_USER=monitoring@example.invalid
export BESZEL_PASSWORD=change-me

python beszel_exporter.py
```

Syntax validation:

```bash
python -m py_compile beszel_exporter.py
```

Container build:

```bash
docker build -t beszel-metrics-exporter .
```

## CI

The included GitHub Actions workflow validates Python syntax and builds the container image on pushes and pull requests.

## Compatibility notes

This exporter intentionally consumes Beszel's Hub/PocketBase data model rather than an official Prometheus endpoint. Beszel may change collection fields between releases.

Failure of an optional collection is isolated where possible, and the exporter exposes per-collection error counters to make schema/access regressions visible.

When upgrading Beszel, verify at least:

```bash
curl -fsS http://127.0.0.1:9105/metrics >/tmp/beszel.metrics
grep '^beszel_exporter_up ' /tmp/beszel.metrics
grep '^beszel_exporter_collection_errors_total' /tmp/beszel.metrics
```

## Repository layout

```text
.
|-- .env.example
|-- .github/workflows/ci.yml
|-- .dockerignore
|-- .gitignore
|-- Dockerfile
|-- README.md
|-- beszel_exporter.py
|-- compose.yaml
|-- requirements.txt
`-- vmagent-scrape.yaml
```

## Upstream

Beszel project: https://github.com/henrygd/beszel

This project is an independent exporter and is not part of the upstream Beszel project.
