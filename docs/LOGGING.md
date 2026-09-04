# Logging

## One rule: write JSON to stdout, and nothing else

```json
{"timestamp":"2026-09-03T17:13:30.873Z","level":"WARNING","logger":"cloudops.alerts",
 "message":"alert fired: CPU above 85% on checkout-api","service":"cloudops-control-plane",
 "environment":"local","version":"1.0.0","correlation_id":"03b40f9b90884cdb",
 "event_type":"alert","rule":"HighCpuUtilization","resource_id":"svc-checkout-api",
 "metric":"cpu_utilization","severity":"warning","value":99.51,"threshold":85.0,
 "runbook":"docs/RUNBOOK.md#high-cpu"}
```

No log files. No log rotation. No syslog. No shipper library compiled into the
application. The container writes to stdout; the runtime captures it; something
downstream forwards it.

This is not laziness, it is the [twelve-factor](https://12factor.net/logs)
contract, and it is what makes the application portable across every log
pipeline without knowing which one it is running under:

| Platform | Picks up stdout via |
|---|---|
| Docker | `json-file` driver (capped here at 10 MB × 3 in compose) |
| Kubernetes | kubelet → `/var/log/containers` → Fluent Bit / Fluentd DaemonSet |
| Azure | Container Insights → Log Analytics → KQL |
| AWS | `awslogs` / Fluent Bit → CloudWatch Logs |
| Self-hosted | Promtail → Loki, or Filebeat → Elasticsearch |

An application that writes its own log files inside a container creates three
problems at once: the files vanish when the container is replaced, they fill the
writable layer, and they are invisible to the platform's log collection. The
Kubernetes manifests mount a **read-only root filesystem**, which makes writing
log files impossible by construction rather than by convention.

## Why structured, not formatted

A human-readable log line is a string that has to be parsed with a regex that
breaks the moment someone changes the wording. A structured record is queryable
the moment it lands:

```
# Loki
{app="cloudops"} | json | severity="critical" | resource_id="svc-checkout-api"

# CloudWatch Logs Insights
fields @timestamp, message, value
| filter event_type = "alert" and severity = "critical"
| sort @timestamp desc

# Azure Log Analytics (KQL)
ContainerLogV2
| where LogMessage.event_type == "alert"
| summarize count() by tostring(LogMessage.rule)
```

The same query works whatever the message text says, because the fields are
fields.

## Correlation IDs

Every request gets one — generated, or honoured from an inbound
`x-correlation-id` header so a trace survives across services. It is stored in a
`ContextVar`, which is the async-safe equivalent of thread-local storage: with
hundreds of concurrent coroutines interleaving on one thread, a module-level
variable would leak one request's ID into another's log lines.

The ID is echoed in the response header, so a user reporting "request X failed"
hands you the exact key to retrieve every line the system emitted while serving
it.

## Event types

Machine-readable logs need a discriminator field. `event_type` is the one:

| `event_type` | Emitted when | Key fields |
|---|---|---|
| `http_access` | Every API request | `method`, `path`, `status`, `duration_ms` |
| `alert` | Alert fires or resolves | `rule`, `severity`, `value`, `threshold`, `runbook` |
| `anomaly` | A detection is recorded | `metric`, `baseline`, `score`, `method` |
| `incident_start` | A simulation begins | `scenario`, `target_kind`, `chaos_injected` |
| *(absent)* | Ordinary operational lines | — |

An alert firing is a log line **and** a metric **and** a database row. The log
line is the one that matters operationally: the log stream is already shipped
somewhere, so an alert there is one query away from an operator with no extra
integration to build.

## Levels, and what actually earns each one

| Level | Meaning | Example here |
|---|---|---|
| `ERROR` | Something failed and needs a human | Health probe failing, alert firing at critical, collection tick raised |
| `WARN` | Degraded, or an anomaly worth attention | Elevated error rate, scrape failure, restart detected, chaos engaged |
| `INFO` | Normal operation, low volume | Startup, collection tick summary, request access log |
| `DEBUG` | Off by default | — |

The discipline that matters is **not logging INFO per sample**. With 19
resources at a 10-second interval that would be 164,000 lines a day of pure
noise, in which the one line that mattered would be invisible. The synthetic log
generator emits a line only when something is notable, plus a heartbeat every
five minutes:

```python
if available < 0.5:  ERROR "health probe failed"
elif err > 0.15:     ERROR "upstream returned 5xx for {err:.1%} of requests"
elif mem > 92:       ERROR "memory pressure critical, GC thrashing"
elif cpu > 92:       WARN  "cpu saturated, request queue growing"
...
elif int(ts) % 300 < interval:  INFO "periodic health summary"
```

## Log collection without a log aggregator

Each demo service keeps its last 500 records in a **bounded** `deque` and
exposes them at `GET /logs?since=<ts>`. The collector pulls incrementally,
tracking a per-resource cursor.

In a real cluster this is replaced by the node log agent, and the interface is
identical — JSON records with a level, a message and a context object. The
buffer is a convenience for a laptop, not an architecture.

`maxlen=500` is load-bearing. An unbounded buffer inside a container with a
512 MiB memory limit is an OOM kill waiting for a busy afternoon, and the
crash would look like an application bug rather than a logging bug.

## Access logging, once

`configure_logging` disables `uvicorn.access` outright:

```python
access = logging.getLogger("uvicorn.access")
access.handlers.clear()
access.propagate = False
access.disabled = True
```

The middleware already emits a richer access log with the correlation ID, the
route template and the duration. Leaving uvicorn's enabled would double the log
volume and hand the shipper **two different schemas for the same event** — and
the two would disagree, because uvicorn logs the raw path while the middleware
logs the route template.

This was a real bug found during verification: the logging setup ran during the
FastAPI lifespan, *after* uvicorn had configured its own logging, and was
re-enabling propagation on a logger that `--no-access-log` had already
suppressed. Every request was logged twice.

## Cardinality

Prometheus labels use the **route template**, never the raw path:

```python
path_label = route.path      # "/api/v1/inventory/{resource_id}"
```

The raw path would mint a new time series per resource ID. That is the classic
way to take a Prometheus instance down: label cardinality is multiplicative, and
an unbounded label is an unbounded number of series. Here, cardinality is
bounded by the inventory size — a few dozen series.

## Verified in CI

The e2e job parses the last 200 log lines from the running container and fails
the build if any line is not valid JSON or is missing `timestamp`, `level`,
`message` or `service`. Structured logging is a contract with the log pipeline:
if one line is unparseable the shipper drops it, and the evidence is gone
exactly when an incident needs it.
