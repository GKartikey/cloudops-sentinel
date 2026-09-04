# CloudOps Sentinel

**Cloud infrastructure monitoring, reliability and cost optimisation — running
entirely on your laptop, with no cloud account and no spend.**

Monitors a 19-resource multi-cloud estate, scores its health, estimates its
bill, detects anomalies, fires alerts, and recommends specific fixes worth
**~$55,000/year**. Three of those resources are real Docker containers whose CPU
and memory are read from their own cgroup — and the incident simulator makes
them genuinely misbehave, so the detection path has to work it out from the
measurements like it would in production.

```bash
git clone <this-repo> && cd cloudops-sentinel
make up          # build and start everything
make verify      # prove it actually works (50+ checks, ~3 min)
make demo        # guided incident walkthrough
```

Dashboard: **http://localhost:8000** · API docs: `/docs` · Metrics: `/metrics`

---

## Why this is not another mock dashboard

The estate is split in half, deliberately.

| | Simulated (16 resources) | Live (3 containers) |
|---|---|---|
| **Source** | `config/inventory.yaml` | Real Docker containers |
| **Telemetry** | Deterministic model | cgroup CPU/memory, scraped over HTTP |
| **Chaos** | Metric modifiers | Actually burns CPU, leaks memory, returns 500s, exits and restarts |
| **Gives you** | Breadth — RDS, S3, Lambda, load balancers, orphaned disks, two clouds, real SKUs and regions | Truth |

Everything above the collector treats both identically. The health scorer, the
anomaly detector, the alert engine and the cost model only ever see a `Resource`
and a stream of samples — they cannot tell which is which.

So when you run `make demo`, this is real:

```
==> Step 3 - injecting a REAL incident: cpu_spike on svc-checkout-api
    chaos in pod  True
==> Step 4 - watching the pipeline react
    t+30s  cpu= 62.4%  ...  health=100 (healthy)  alerts=0
    t+60s  cpu= 91.8%  ...  health= 83 (degraded) alerts=0
    t+90s  cpu= 91.5%  ...  health= 83 (degraded) alerts=2
==> Step 5 - what the platform worked out by itself
    [critical] CPU above 95% on checkout-api  -> docs/RUNBOOK.md#high-cpu
    [warning ] CPU above 85% on checkout-api  -> docs/RUNBOOK.md#high-cpu
```

Nothing told the alert engine an incident was requested.

## What it does

**Monitoring** — CPU, memory, disk, request latency (p95), error rate,
throughput, restart count, availability. Pull-based collection every 10s, so a
dead target is detected by its absence rather than by silence.

**Health** — a deduction model (start at 100, subtract for each fault) producing
`healthy` / `degraded` / `unhealthy` / `not_monitored`, with the reasons listed.
Auditable: anyone can reconstruct the number from the reasons.

**Cost** — provisioned vs effective spend per resource, waste quantified,
attributed by provider, type, environment, owner and region. The demo estate:
**$7,532/month, 55% of it waste.**

**Anomaly detection** — robust z-score (median + MAD, not mean + stdev, so one
outlier cannot blind it) plus a trend detector for slow ramps that a z-score is
structurally blind to, like a memory leak.

**Alerting** — Prometheus semantics: `pending → firing → resolved`, `for`
durations so one bad scrape never pages, stable fingerprints, state in SQLite so
it survives a restart, and every rule links to a runbook section.

**Recommendations** — 10 analysers covering over-provisioned, under-utilised,
unhealthy containers, high error rates, suspicious configuration, missing health
checks, orphaned resources, storage tiering, non-prod scheduling and commitment
discounts. Every finding carries evidence, a specific action, and a dollar
figure.

**Incident simulation** — 9 scenarios: `cpu_spike`, `memory_leak`, `crash_loop`,
`latency_degradation`, `error_burst`, `traffic_surge`, `outage`,
`disk_pressure`, `cost_spike`. Time-boxed, so a demo cannot leave the fleet
wedged.

## Stack

Python 3.12 · FastAPI · SQLite (WAL) · Docker · Kubernetes · Prometheus ·
Grafana · GitHub Actions. No cloud account, no paid service, no CDN, no npm.

## Getting started

**Prerequisites:** Docker Desktop (or Docker Engine + Compose v2), Python 3.11+
for the scripts. That is all.

```bash
make up                 # build + start, waits for readiness
make verify             # 50+ check end-to-end verification, incl. a live incident
make verify-quick       # same, minus the 2-minute incident test
make test               # 69 unit tests
make demo               # narrated incident walkthrough
make down               # stop (history preserved)
make clean              # stop and wipe volumes
```

Ports: control plane `8000`, demo services `8081`–`8083`. Override in `.env`
(copy `.env.example`).

### With Prometheus and Grafana

```bash
make observability
```

Prometheus `:9090` · Grafana `:3000` (anonymous viewer access; the admin
password is generated into `secrets/`, which is gitignored). The dashboard and
datasource are provisioned from `observability/`, so there is nothing to click.

### On Kubernetes

```bash
make k8s-validate       # render + schema-validate, no cluster needed
make k8s-apply          # apply to the current context
```

### Handy

```bash
make cost               # cost summary
make recommendations    # top findings by saving
make metrics            # the Prometheus exposition
make incident SCENARIO=memory_leak TARGET=svc-checkout-api
make stop-incidents
make logs
```

## API

Versioned at `/api/v1`. Full OpenAPI at `/docs`.

| Endpoint | Returns |
|---|---|
| `GET /healthz` `/readyz` | Liveness (no I/O) and readiness (checks the store and the collector) |
| `GET /metrics` | Prometheus exposition — the whole estate as `cloudops_*` series |
| `GET /api/v1/overview` | Everything the dashboard needs, one round trip |
| `GET /api/v1/inventory[/{id}]` | The estate, filterable by provider/type/environment |
| `GET /api/v1/metrics/latest` \| `/series` | Newest sample per series \| history |
| `GET /api/v1/health` | Fleet health with per-resource reasons |
| `GET /api/v1/cost[/{id}]` | Cost, waste and efficiency |
| `GET /api/v1/recommendations` | Findings with evidence, action and saving |
| `GET /api/v1/anomalies` \| `/alerts` \| `/logs` | Detections, alert state, log search |
| `POST /api/v1/incidents` | Inject an incident |
| `DELETE /api/v1/incidents[/{id}]` | Cancel one, or all |

## Security

No credential exists anywhere in this repository — not a placeholder, not a
default, not a test fixture. Secrets come from environment variables and are
simply **absent** when unset (the webhook sink is not registered at all; there
is no fallback endpoint). Both images are multi-stage, run as non-root UID
10001, drop all capabilities, and set `no-new-privileges`. Kubernetes adds a
read-only root filesystem, `seccompProfile: RuntimeDefault`, and the
`restricted` Pod Security Standard.

RBAC is namespaced, read-only, and explicitly excludes `secrets` — a monitoring
component that can read secrets can read every credential in the namespace.
NetworkPolicy is default-deny with one permitted east-west path, and egress
excludes `169.254.0.0/16` so an SSRF cannot reach the cloud instance metadata
endpoint and steal an IAM role.

**All of this is asserted in CI.** A future edit that drops `runAsNonRoot`, adds
an RBAC wildcard, or commits a literal credential fails the build. Full detail
in [docs/SECURITY.md](docs/SECURITY.md).

## CI/CD

`lint → test → build → e2e + k8s-validate → security → publish → deploy (gated)`

The e2e job runs the **same** `scripts/verify_local.py` a developer runs — there
is no CI-only test path that can pass while the real thing is broken. It brings
the stack up, injects real chaos into a real container, and asserts the
detection path noticed. The build job fails if an image runs as root or lacks a
`HEALTHCHECK`; `k8s-validate` asserts the security properties above; secret
scanning is a hard failure while unfixable base-image CVEs are not.

## Repository layout

```
config/            inventory, rate card, alert rules — one source of truth,
                   shared by docker-compose and the K8s ConfigMap generator
services/
  control-plane/   FastAPI app: api/, core/ (config, logging, store), engine/
  demo-service/    one image, three roles, real cgroup metrics + chaos
kustomization.yaml kustomize root - generates the ConfigMap from config/
k8s/base/          namespace/PSA, RBAC, config, StatefulSet, Deployments,
                   HPA, NetworkPolicy
observability/     Prometheus scrape + rules, Grafana datasource + dashboard
docs/              architecture, security, cost model, runbook, logging, limits
scripts/           verify_local.py, demo.sh
tests/             69 unit tests
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — components, the collection loop, and why each choice
- [Security](docs/SECURITY.md) — credentials, hardening, RBAC, least privilege, threat model
- [Cost model](docs/COST_MODEL.md) — the maths, the analysers, and what is not modelled
- [Runbook](docs/RUNBOOK.md) — one section per alert: confirm, fix, prevent
- [Logging](docs/LOGGING.md) — structured logging and why stdout is the contract
- [Limitations](docs/LIMITATIONS.md) — the honest list, with fixes

## Honest scope

Sixteen of the nineteen resources are simulated, the cost model will not
reconcile with a real invoice, and SQLite means exactly one writer so the
control plane cannot scale horizontally. Every one of those is a deliberate
trade-off with a stated reason and a stated fix in
[docs/LIMITATIONS.md](docs/LIMITATIONS.md) — worth reading before you form a
view of what this does and does not prove.

## Licence

MIT.
