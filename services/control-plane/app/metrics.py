"""Prometheus exposition.

Two families of metric live on /metrics and they are deliberately kept apart:

  cloudops_resource_*   the fleet's telemetry, re-published so Prometheus can
                        scrape the whole simulated + live estate from one
                        endpoint. This makes CloudOps Sentinel an *exporter*, and
                        it is why the Grafana dashboard and the Prometheus rules
                        in observability/ work without any custom datasource.

  cloudops_collector_*  the control plane's own health: did the last tick
                        succeed, how long did it take, how many scrapes failed.
                        A monitoring system that cannot report on itself is the
                        one component nobody notices has died.

Label cardinality is bounded by the inventory size (a few dozen series), which
is the discipline that keeps a Prometheus instance from falling over.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

_RESOURCE_LABELS = ("resource", "name", "provider", "type", "region", "environment")

# ------------------------------------------------------------------ fleet
RESOURCE_CPU = Gauge(
    "cloudops_resource_cpu_utilization_percent",
    "CPU utilisation of a monitored cloud resource",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_MEMORY = Gauge(
    "cloudops_resource_memory_utilization_percent",
    "Memory utilisation of a monitored cloud resource",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_DISK = Gauge(
    "cloudops_resource_disk_utilization_percent",
    "Disk utilisation of a monitored cloud resource",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_LATENCY = Gauge(
    "cloudops_resource_request_latency_p95_milliseconds",
    "95th percentile request latency of a monitored cloud resource",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_ERROR_RATE = Gauge(
    "cloudops_resource_error_rate_ratio",
    "Fraction of requests failing (0-1)",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_RPS = Gauge(
    "cloudops_resource_requests_per_second",
    "Request throughput of a monitored cloud resource",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_RESTARTS = Gauge(
    "cloudops_resource_restart_count",
    "Observed process/container restarts",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_UP = Gauge(
    "cloudops_resource_up",
    "1 if the resource answered its health probe, 0 otherwise",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_HEALTH_SCORE = Gauge(
    "cloudops_resource_health_score",
    "Composite health score 0-100",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)

METRIC_GAUGES = {
    "cpu_utilization": RESOURCE_CPU,
    "memory_utilization": RESOURCE_MEMORY,
    "disk_utilization": RESOURCE_DISK,
    "latency_p95_ms": RESOURCE_LATENCY,
    "error_rate": RESOURCE_ERROR_RATE,
    "requests_per_second": RESOURCE_RPS,
    "restart_count": RESOURCE_RESTARTS,
    "availability": RESOURCE_UP,
}

# ------------------------------------------------------------------- cost
RESOURCE_COST = Gauge(
    "cloudops_resource_estimated_cost_monthly_usd",
    "Estimated monthly cost of a resource at list price",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
RESOURCE_WASTE = Gauge(
    "cloudops_resource_estimated_waste_monthly_usd",
    "Portion of the monthly cost attributable to unused capacity",
    _RESOURCE_LABELS,
    registry=REGISTRY,
)
FLEET_COST = Gauge(
    "cloudops_fleet_estimated_cost_monthly_usd",
    "Estimated monthly cost of the whole monitored estate",
    registry=REGISTRY,
)
FLEET_WASTE = Gauge(
    "cloudops_fleet_estimated_waste_monthly_usd",
    "Estimated monthly waste across the whole monitored estate",
    registry=REGISTRY,
)
FLEET_SAVING = Gauge(
    "cloudops_fleet_identified_saving_monthly_usd",
    "Monthly saving of all open recommendations",
    registry=REGISTRY,
)

# ------------------------------------------------------------ fleet health
FLEET_HEALTH_SCORE = Gauge(
    "cloudops_fleet_health_score",
    "Mean health score across the estate",
    registry=REGISTRY,
)
FLEET_RESOURCES = Gauge(
    "cloudops_fleet_resources",
    "Number of resources in the inventory by health status",
    ("status",),
    registry=REGISTRY,
)
ACTIVE_ALERTS = Gauge(
    "cloudops_active_alerts",
    "Currently firing alerts by severity",
    ("severity",),
    registry=REGISTRY,
)
OPEN_RECOMMENDATIONS = Gauge(
    "cloudops_open_recommendations",
    "Open recommendations by category",
    ("category",),
    registry=REGISTRY,
)
ACTIVE_INCIDENTS = Gauge(
    "cloudops_active_incidents",
    "Simulated incidents currently running",
    registry=REGISTRY,
)

# --------------------------------------------------------------- collector
COLLECTION_RUNS = Counter(
    "cloudops_collection_runs_total",
    "Collection ticks executed",
    ("outcome",),
    registry=REGISTRY,
)
COLLECTION_DURATION = Histogram(
    "cloudops_collection_duration_seconds",
    "Wall time of one collection tick",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
SAMPLES_WRITTEN = Counter(
    "cloudops_samples_written_total",
    "Metric samples persisted",
    registry=REGISTRY,
)
LOGS_INGESTED = Counter(
    "cloudops_logs_ingested_total",
    "Log records ingested",
    ("level",),
    registry=REGISTRY,
)
SCRAPE_FAILURES = Counter(
    "cloudops_scrape_failures_total",
    "Failed scrapes of a live target",
    ("resource",),
    registry=REGISTRY,
)
ANOMALIES_DETECTED = Counter(
    "cloudops_anomalies_detected_total",
    "Anomalies recorded",
    ("severity",),
    registry=REGISTRY,
)
ALERTS_FIRED = Counter(
    "cloudops_alerts_fired_total",
    "Alert transitions into the firing state",
    ("rule", "severity"),
    registry=REGISTRY,
)
LAST_COLLECTION_TS = Gauge(
    "cloudops_last_collection_timestamp_seconds",
    "Unix timestamp of the last successful collection tick",
    registry=REGISTRY,
)

# ------------------------------------------------------------- http server
HTTP_REQUESTS = Counter(
    "cloudops_http_requests_total",
    "HTTP requests served by the control plane API",
    ("method", "path", "status"),
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "cloudops_http_request_duration_seconds",
    "Control plane API request duration",
    ("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=REGISTRY,
)
BUILD_INFO = Gauge(
    "cloudops_build_info",
    "Build metadata, always 1",
    ("version", "environment", "service"),
    registry=REGISTRY,
)


def labels_for(resource) -> tuple[str, ...]:
    return (
        resource.id,
        resource.name,
        resource.provider,
        resource.type,
        resource.region,
        resource.environment,
    )


def render() -> bytes:
    return generate_latest(REGISTRY)
