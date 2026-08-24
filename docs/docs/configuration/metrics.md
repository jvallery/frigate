---
id: metrics
title: Metrics
---

import ConfigTabs from "@site/src/components/ConfigTabs";
import TabItem from "@theme/TabItem";
import NavPath from "@site/src/components/NavPath";

# Metrics

Frigate exposes Prometheus metrics at the `/api/metrics` endpoint that can be used to monitor the performance and health of your Frigate instance.

## Enabling Telemetry

Prometheus metrics are exposed via the telemetry configuration. Enable or configure telemetry to control metric availability.

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > System > Telemetry" /> to configure metrics and telemetry settings.

</TabItem>
<TabItem value="yaml">

Metrics are available at `/api/metrics` by default. No additional Frigate configuration is required to expose them.

</TabItem>
</ConfigTabs>

## Available Metrics

### System Metrics

- `frigate_cpu_usage_percent{pid="", name="", process="", type="", cmdline=""}` - Process CPU usage percentage
- `frigate_mem_usage_percent{pid="", name="", process="", type="", cmdline=""}` - Process memory usage percentage
- `frigate_gpu_usage_percent{gpu_name=""}` - GPU utilization percentage
- `frigate_gpu_mem_usage_percent{gpu_name=""}` - GPU memory usage percentage

### Camera Metrics

- `frigate_camera_fps{camera_name=""}` - Frames per second being consumed from your camera
- `frigate_detection_fps{camera_name=""}` - Number of times detection is run per second
- `frigate_process_fps{camera_name=""}` - Frames per second being processed
- `frigate_skipped_fps{camera_name=""}` - Frames per second skipped for processing
- `frigate_detection_enabled{camera_name=""}` - Detection enabled status for camera
- `frigate_audio_dBFS{camera_name=""}` - Audio dBFS for camera
- `frigate_audio_rms{camera_name=""}` - Audio RMS for camera

### Detector Metrics

- `frigate_detector_inference_speed_seconds{name=""}` - Time spent running object detection in seconds
- `frigate_detection_start{name=""}` - Detector start time (unix timestamp)

### Storage Metrics

- `frigate_storage_free_bytes{storage=""}` - Storage free bytes
- `frigate_storage_total_bytes{storage=""}` - Storage total bytes
- `frigate_storage_used_bytes{storage=""}` - Storage used bytes
- `frigate_storage_mount_type{mount_type="", storage=""}` - Storage mount type info

These gauges report the operating system's figures for the whole filesystem (the same numbers as `df`), not Frigate's own recording footprint. For how this differs from the recordings usage shown in the UI, see [Understanding storage usage](/configuration/record#understanding-storage-usage).

### Service Metrics

- `frigate_service_uptime_seconds` - Uptime in seconds
- `frigate_service_last_updated_timestamp` - Stats recorded time (unix timestamp)
- `frigate_device_temperature{device=""}` - Device Temperature

### Event Metrics

- `frigate_camera_events{camera="", label=""}` - Count of camera events since exporter started

## Configuring Prometheus

Prometheus can scrape the internal unauthenticated port when it is restricted to
a trusted network:

```yaml
scrape_configs:
  - job_name: "frigate"
    metrics_path: "/api/metrics"
    static_configs:
      - targets: ["frigate:5000"]
    scrape_interval: 15s
```

### Dedicated bearer credential

When Prometheus must use the authenticated port `8971` and native authentication
is enabled, Frigate supports an optional metrics-only bearer credential. Mount
a read-only credentials file at
`/run/secrets/FRIGATE_METRICS_TOKENS`. The file must contain one or two unique,
unpadded base64url tokens, one per line. Each token must be between 43 and 128
characters, and the file may end with one newline.

Generate a 32-byte token with Python:

```shell
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Configure Prometheus to read the same token from its own read-only credentials
file and send it as a bearer token:

```yaml
scrape_configs:
  - job_name: "frigate"
    scheme: "https"
    metrics_path: "/api/metrics"
    authorization:
      type: "Bearer"
      credentials_file: "/etc/prometheus/secrets/frigate/token"
    tls_config:
      ca_file: "/etc/prometheus/certs/frigate-ca.pem"
      server_name: "frigate.example"
    static_configs:
      - targets: ["frigate:8971"]
    scrape_interval: 15s
```

Port `8971` uses HTTPS by default. Mount the certificate authority that signed
the Frigate certificate into Prometheus, and set `server_name` to a name covered
by that certificate. The default generated self-signed certificate is not a
durable trust anchor for an unattended credential-bearing scrape. If TLS is
explicitly disabled because another trusted, isolated network layer provides
the transport boundary, set `scheme: "http"` and remove `tls_config`.

This credential authorizes only the exact `GET /api/metrics` request. It does
not create a Frigate user, grant access to other API routes, or issue a cookie.
Missing, invalid, or nonmatching credential files are rejected. Native JWT and
internal-port authentication are not changed.

Frigate reads the file for every opaque bearer authentication attempt. To rotate
without a restart, write the old and new tokens on separate lines, update all
scrapers to the new token, and then remove the old token. Removing the file
revokes every metrics bearer credential. Keep both projected files private and
read-only.

## Example Queries

Here are some example PromQL queries that might be useful:

```promql
# Average CPU usage across all processes
avg(frigate_cpu_usage_percent)

# Total GPU memory usage
sum(frigate_gpu_mem_usage_percent)

# Detection FPS by camera
rate(frigate_detection_fps{camera_name="front_door"}[5m])

# Storage usage percentage
(frigate_storage_used_bytes / frigate_storage_total_bytes) * 100

# Event count by camera in last hour
increase(frigate_camera_events[1h])
```

## Grafana Dashboard

You can use these metrics to create Grafana dashboards to monitor your Frigate instance. Here's an example of metrics you might want to track:

- CPU, Memory and GPU usage over time
- Camera FPS and detection rates
- Storage usage and trends
- Event counts by camera
- System temperatures

A sample Grafana dashboard JSON will be provided in a future update.

## Metric Types

The metrics exposed by Frigate use the following Prometheus metric types:

- **Counter**: Cumulative values that only increase (e.g., `frigate_camera_events`)
- **Gauge**: Values that can go up and down (e.g., `frigate_cpu_usage_percent`)
- **Info**: Key-value pairs for metadata (e.g., `frigate_storage_mount_type`)

For more information about Prometheus metric types, see the [Prometheus documentation](https://prometheus.io/docs/concepts/metric_types/).
