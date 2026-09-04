# Runbook

Every alert rule in `config/rules.yaml` and
`observability/prometheus/alert_rules.yml` links to a section here. An alert
without a runbook is a 3am guessing game, so the CI lint job fails the build if
any rule is missing its `runbook` field.

## How to use this

Each section follows the same shape:

**What fired → What it means → Confirm it → Fix it → Prevent it**

Reproduce any of these locally before you need them for real:

```bash
make incident SCENARIO=cpu_spike TARGET=svc-checkout-api DURATION=180
make stop-incidents
```

---

## high-cpu

**Fired:** `HighCpuUtilization` (>85%, 60s) or `CriticalCpuUtilization` (>95%).

**Means:** the resource is CPU-saturated. Above ~90% the run queue grows,
latency rises non-linearly, and the service starts shedding load. On a
containerised workload this is usually **CFS throttling** against the CPU limit,
not a busy host — the node can look completely idle while the container is
stalled.

**Confirm**

```bash
curl -s localhost:8000/api/v1/metrics/series'?resource_id=svc-checkout-api&metric=cpu_utilization&minutes=60' | python -m json.tool
curl -s 'localhost:8000/api/v1/logs?level=WARN&minutes=15'
```

Distinguish the three causes before acting:

| Pattern | Cause | Action |
|---|---|---|
| Throughput up with CPU | Genuine load | Scale out |
| CPU up, throughput flat or down | Runaway process / hot loop / GC thrash | Profile, roll back |
| CPU up right after a deploy | Regression | Roll back first |

**Fix**
1. Correlate with the most recent deploy. If they line up, **roll back first and
   diagnose after** — restoring service beats understanding it.
2. Genuine load: scale horizontally. `kubectl -n cloudops scale deploy/checkout-api --replicas=4`
   (the HPA does this at 70% automatically; if it did not, check that the pod has
   resource **requests** — the HPA divides by the request, so a pod without one
   has no denominator and the HPA silently does nothing).
3. Throttling: raise the CPU limit. Check `container_cpu_cfs_throttled_seconds_total`.

**Prevent:** HPA on CPU + memory; load-test before peak season; alert on
throttling, not only on utilisation.

---

## memory-leak

**Fired:** `HighMemoryUtilization` (>90%, 60s), or a `trend_drift` anomaly on
`memory_utilization`.

**Means:** memory is climbing toward the limit. The next event is an **OOM
kill** — exit code 137, container restarted, in-flight requests dropped.

The trend detector matters here. A leak never looks sharp at any single sample,
so a plain z-score is structurally blind to it: the baseline drifts upward along
with the value, and by the time it is at 95% the median is at 60% and the
"spread" is enormous. `_trend_score` normalises by the *recent* window's spread
instead of the whole window's, which is what makes a slow ramp visible.

**Confirm**

```bash
curl -s localhost:8000/api/v1/metrics/series'?resource_id=svc-checkout-api&metric=memory_utilization&minutes=360'
kubectl -n cloudops get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'   # OOMKilled?
```

A sawtooth that resets on restart is a leak. A step change that holds is a
legitimate increase in working set.

**Fix**
1. Confirm it is a leak, not growth. Raising the limit on a real leak buys time
   and nothing else — it will hit the new limit too.
2. If it is a leak: roll back to the last known-good image, then heap-profile.
   Common causes are unbounded caches, unclosed connections, and accumulating
   request context.
3. If it is legitimate growth: raise `resources.limits.memory` **and** the
   request together.

**Prevent:** bound every cache; set memory request == limit for the Guaranteed
QoS class on critical workloads; alert on the trend, not just the threshold.

---

## error-burst

**Fired:** `HighErrorRate` (>5%, 60s), `ElevatedErrorRate` (>2%, 120s), or
`DemoServiceErrorBudgetBurn` (fast and slow window both elevated).

**Means:** users are seeing failures right now. This is the one that pages.

**Confirm**

```bash
curl -s 'localhost:8000/api/v1/logs?level=ERROR&minutes=15&limit=50'
curl -s localhost:8000/api/v1/alerts?status=firing
```

**Fix**
1. **Deploy correlation is the first question, always.** Most error bursts are a
   bad release. If the onset lines up with a rollout, roll back — do not debug
   forward with users failing.
2. If not a deploy, check dependency latency for the same window. A downstream
   timeout surfaces as your 5xx.
3. Check for a config or secret change: an expired credential produces a clean,
   instant, 100% error rate with no code change.

**Prevent:** canary or blue/green deploys; circuit breakers with timeouts on
every outbound call; error-budget burn alerts (this repo implements the
multi-window pattern — fast 5m burn confirmed by slow 1h burn, so a 90-second
blip does not page while a real regression still catches quickly).

---

## latency

**Fired:** `LatencySLOBreach` (p95 > 800ms, 90s).

**Means:** the objective is breached. Latency degrades before errors appear —
this is usually the earliest actionable signal you get.

**Confirm:** plot `latency_p95_ms` alongside `requests_per_second` and
`cpu_utilization`.

| Latency | Throughput | CPU | Diagnosis |
|---|---|---|---|
| ↑ | ↑ | ↑ | Load — scale out |
| ↑ | flat | flat | Slow dependency (DB, cache, downstream API) |
| ↑ | ↓ | ↑ | Saturation, queueing, possible collapse |
| ↑ | flat | ↑ | GC pressure or CPU throttling |

**Fix:** find the slow dependency; check connection-pool exhaustion (a pool too
small looks exactly like a slow database); verify timeouts exist on every
outbound call — a missing timeout turns one slow dependency into a fleet-wide
thread pool exhaustion.

**Prevent:** timeouts and budgets on every call; p95/p99 SLOs rather than
averages (an average hides the tail that users actually experience).

---

## crash-loop

**Fired:** `ContainerRestartLoop` — `increase(restart_count[10m]) > 2`.

Note the `increase()`. `restart_count` is a **monotonic counter**, so alerting on
its absolute value latches on the first crash loop and never resolves, because a
counter never goes down. This bug was caught by the test suite and is worth
remembering: never alert directly on a counter.

**Means:** the container starts, fails, and is restarted — Kubernetes
`CrashLoopBackOff`, with exponential backoff up to 5 minutes.

**Confirm**

```bash
docker inspect cloudops-report-worker --format '{{.RestartCount}} {{.State.Status}}'
kubectl -n cloudops describe pod <pod>          # Events explain WHY
kubectl -n cloudops logs <pod> --previous       # the crashed run, not the new one
```

`--previous` is the important flag. Without it you get the logs of the container
that is currently starting, not the one that died.

**Fix** — work the exit code:

| Exit code | Meaning | Action |
|---|---|---|
| 137 | SIGKILL, usually OOM | Raise memory limit; see [memory-leak](#memory-leak) |
| 143 | SIGTERM, graceful | Probably a failing liveness probe |
| 1 | Application error | Read `logs --previous` |
| 0 | Process exited cleanly | Wrong command, or a job in a Deployment |

**A slow-starting app killed by an aggressive liveness probe is the most common
false crash loop.** The fix is a `startupProbe`, not a slacker liveness probe:
the startup probe grants a long grace period once, then hands over to a tight
liveness probe for the rest of the pod's life. The control plane uses exactly
this pattern (30 × 5s startup for the first-boot backfill).

**Prevent:** `startupProbe` for slow starts; liveness that does not depend on
downstream services (or one slow dependency cascades into a fleet-wide restart
storm); resource limits set from measurement, not guesswork.

---

## target-down

**Fired:** `TargetDown` (`availability < 1`, 60s) or Prometheus `up == 0`.

**Means:** the collector could not reach the target at all. This records
`availability=0` rather than skipping the resource — *down is a measurement*.

**Confirm**

```bash
docker compose ps
curl -sv localhost:8081/healthz
docker compose logs checkout-api --tail 50
kubectl -n cloudops get endpoints checkout-api     # empty = no ready pods
```

**Fix**
1. Container not running → check exit code, see [crash-loop](#crash-loop).
2. Running but unreachable → **network policy**. A default-deny policy without
   the DNS exception produces exactly this, and the error looks nothing like a
   policy problem.
3. `Service` has no endpoints → readiness probe failing, or a label selector
   mismatch between the Service and the pod template.
4. Reachable by IP but not by name → DNS. Check CoreDNS.

**Prevent:** readiness probes on everything; test network policies with an
explicit deny-check; alert on `up == 0`, which fires even when the target is too
broken to emit anything at all.

---

## collector-stalled

**Fired:** `CollectorStalled` — `time() - cloudops_last_collection_timestamp_seconds > 120`.

**Means:** the monitoring system itself has stopped. **Every other panel and
every other alert is now stale**, and stale data looks exactly like healthy
data. This is the most important alert in the system for that reason: the
failure mode of monitoring is silence, and silence is indistinguishable from
"everything is fine".

**Confirm**

```bash
curl -s localhost:8000/api/v1/system | python -m json.tool | head -30
docker compose logs control-plane --tail 100 | grep -i error
curl -s localhost:8000/metrics | grep cloudops_collection_runs_total
```

**Fix**
1. `outcome="error"` climbing → read the traceback; the loop catches everything
   and continues, so the error is logged rather than fatal.
2. Ticks taking longer than the interval → check
   `cloudops_collection_duration_seconds`. Usually the SQLite file has grown;
   confirm pruning is running and `RETENTION_HOURS` is sane.
3. Disk full → the volume backing `/data`.
4. `restart` the control plane. Alert state is in SQLite and survives.

**Prevent:** this alert, plus a dead-man's-switch in an external system (a
heartbeat that pages when it *stops* arriving). Monitoring that only alerts on
its own metrics cannot tell you it is dead.

---

## cost-spike

**Fired:** `EstateWasteAboveThreshold` (>30% waste, 30m) or `MonthlySpendJumped`
(+25% vs the previous 6 hours).

**Means:** spend or waste moved sharply. Not an outage — a slow incident that
nobody gets paged for, which is exactly why it grows unchecked until the invoice
arrives.

**Confirm**

```bash
make cost
make recommendations
curl -s localhost:8000/api/v1/cost | python -c "import sys,json;d=json.load(sys.stdin);[print(f\"{r['name']:24} \${r['monthly_cost']:>9,.2f}  waste \${r['waste_monthly']:>9,.2f}\") for r in d['top_waste']]"
```

**Fix** — in order of speed and safety:
1. **Orphans first.** Detached disks and unattached IPs: no risk, immediate
   saving, no owner conversation needed.
2. **Non-prod schedules.** 64% off anything that only needs working hours.
3. **Rightsizing.** Non-prod first, watch p95 latency for a full traffic cycle,
   then prod.
4. **Storage tiering.** Lifecycle rules on cold data.
5. **Commitments last** — and only after rightsizing, or you lock in the
   oversized shape for a year.

If the spike was sudden, check for an autoscaler stuck at max replicas, a
runaway batch job, a forgotten load test, or a data-transfer change (egress is
the most commonly underestimated line on any cloud bill).

**Prevent:** budget alerts at 50/80/100% of forecast; mandatory `owner` and
`cost_center` tags with a policy that blocks untagged resources; scheduled
non-prod shutdown by default; monthly review of the recommendations report.
