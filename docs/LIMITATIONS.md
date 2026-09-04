# Limitations

Written deliberately and kept honest. A portfolio project that claims no
limitations is either untested or misrepresented, and the fastest way to fail a
technical interview is to defend a weakness you have not noticed. Everything
below is a known trade-off with a stated reason and a stated fix.

## 1. Most of the estate is simulated

**16 of 19 resources are generated from a YAML inventory, not discovered from a
real cloud account.** Their telemetry comes from a deterministic model.

*Why:* the hard requirement was that this runs on a laptop with no cloud spend.
A real multi-cloud estate that produces these findings would cost thousands of
dollars a month.

*What is genuinely real:* the 3 Docker containers. Their CPU and memory are read
from their own cgroup — the same source `docker stats` and the kubelet use —
expressed as a percentage of the container's limit. They are scraped over HTTP.
Chaos genuinely burns CPU, leaks memory, returns 500s, and exits so the restart
policy restarts it. The detection path is not told an incident was requested; it
works it out from the measurements.

*Fix:* replace `load_inventory()` with an Azure Resource Graph or AWS Resource
Groups Tagging API call. Nothing downstream changes, because nothing downstream
knows where the estate came from.

## 2. The cost model is directionally right and precisely wrong

It will not reconcile with an invoice, and it is not intended to. Not modelled:
Enterprise Agreements, negotiated discounts, existing reservations, credits,
**data egress** (often 10–20% of a real bill and the most commonly
underestimated line), inter-AZ transfer, NAT gateway processing, snapshots,
provisioned IOPS, support plans, taxes, spot interruption rates.

*Fix:* Azure Retail Prices API / AWS Price List API for rates; Cost Management
API / Cost and Usage Report for actuals. See [COST_MODEL.md](COST_MODEL.md).

## 3. The rightsizing window is too short

Utilisation is computed over the retention window — **24 hours by default**.
Real rightsizing needs **14–30 days** to capture weekly cycles, month-end batch
runs and seasonal peaks. A 24-hour window will cheerfully recommend shrinking a
box that is only busy on the last day of the month.

*Fix:* raise `RETENTION_HOURS`, and move the store off SQLite first (see below).

## 4. SQLite means exactly one writer

The control plane is a `StatefulSet` with `replicas: 1`, not a `Deployment`,
precisely because two pods writing one SQLite file would corrupt it. This is the
single biggest architectural constraint.

Consequences, stated plainly:
- **No horizontal scaling** of the control plane.
- **No high availability.** Losing the node means a monitoring gap.
- The `PodDisruptionBudget` is `maxUnavailable: 1`, which permits eviction. With
  one replica a PDB cannot preserve availability anyway — setting
  `maxUnavailable: 0` would not fix that, it would just make `kubectl drain`
  hang forever and block node upgrades, which is a worse failure than a
  20-second gap.

*Fix:* Postgres/TimescaleDB for the mutable tables, Prometheus or Mimir for the
series. The queries port over largely unchanged; the schema was chosen with that
in mind.

## 5. Retention is 24 hours and pruning is coarse

`DELETE FROM samples WHERE ts < ?` roughly once a minute. No downsampling, so
older data is discarded at full resolution rather than rolled up.

*Fix:* continuous aggregates (Timescale) or recording rules + a longer-retention
tier (Mimir/Thanos), keeping 5-minute rollups for 90 days.

## 6. Restart detection is inferred, not authoritative

Restarts are detected by watching each target's reported process start time
change, cross-checked with a counter the container persists across restarts.

*Why not the real thing:* the authoritative source is the Docker API, and
mounting `/var/run/docker.sock` into a monitoring container hands it **root on
the host**. A restart counter is not worth that trade.

*Known gap:* a container that crashes faster than the scrape interval is never
observed alive, so the restart is invisible. This is a real property of all
pull-based monitoring, and it was found during verification — the first crash
loop test recorded zero restarts because the process died within a second. The
demo service now serves for a 14-second grace period before crashing, which is
also what real crash loops do.

*Fix in Kubernetes:* read `containerStatuses[].restartCount` from the API using
the read-only Role that already exists, or scrape kube-state-metrics.

## 7. Anomaly detection is statistical, not learned

Robust z-score plus trend drift. No seasonality model, so a legitimate Monday
morning traffic ramp can register as drift. No multivariate correlation, so it
cannot say "CPU and latency rose together, therefore load, not a fault."

*Why:* explainable at 3am, no training job, no model artefact, works on the
twelfth sample of a brand-new resource. "The model said so" does not survive a
page.

*Known gaps:* fixed thresholds do not adapt per-resource; there is no automatic
suppression of anomalies that are consequences of another anomaly, so one
incident can produce several correlated detections.

*Fix:* seasonal decomposition (STL) or Prophet for daily/weekly patterns; alert
grouping by causal proximity.

## 8. Alert routing is minimal

Two sinks: structured log, and a generic webhook. No Alertmanager integration,
no PagerDuty/Opsgenie, no severity-based routing to different teams, no
maintenance windows, no inhibition rules (a `TargetDown` should suppress the
`HighErrorRate` alert for the same resource — it does not).

*Fix:* the webhook payload is already Alertmanager-shaped. Point Prometheus at a
real Alertmanager and use the rules in `observability/prometheus/alert_rules.yml`,
which express the same conditions in PromQL.

## 9. The dashboard polls

5-second polling of `/api/v1/overview`, not WebSockets or SSE. Fine at this
scale, wasteful at a thousand resources. There is no pagination, no time-range
picker beyond the chart, no drill-down beyond one resource, and no dark/light
toggle (it is dark only).

*Fix:* SSE for push, server-side pagination, virtualised table rows.

## 10. Kubernetes manifests are validated, not deployed

CI renders them with kustomize, schema-validates with kubeconform, and asserts
the security properties. They have **not** been applied to a live cluster in
this project's CI, because a real cluster costs money and a kind cluster in CI
would test kind, not production.

*Untested in a live cluster:* the HPA actually scaling (needs metrics-server),
NetworkPolicy actually enforcing (needs a policy-capable CNI — on a CNI that
ignores them the objects apply cleanly and do nothing, which is a dangerous
illusion), PVC binding with a real StorageClass, the ConfigMap hash triggering a
rollout.

*Fix:* add a `kind` job to CI for smoke-level validation, and a staging cluster
for the rest. `make k8s-apply` works against any real cluster today.

## 11. No authentication on the demo services

`POST /admin/chaos` is an unauthenticated endpoint that makes a container
misbehave. It exists because inducing genuine failure is the point of the
demonstration.

*Mitigation:* the NetworkPolicy restricts reachability on 8080 to the control
plane alone. In a real deployment it would be compiled out or bound to a
separate, unexposed, authenticated admin port.

## 12. Single-tenant, no user model

No users, no roles, no audit trail of who acknowledged which alert beyond a
free-text `acked_by`, no multi-tenancy. The API token is a single shared
credential with two levels (read, write).

*Fix:* OIDC via the cloud identity provider, RBAC mapped to groups, per-user
audit.

## 13. Testing gaps

69 unit tests plus a 55-check end-to-end script that runs against real
containers. Not covered: load or soak testing (behaviour at 10,000 resources is
unknown), chaos testing of the control plane *itself* (what happens if SQLite is
corrupted mid-write), browser tests for the dashboard, and mutation testing.

Coverage is measured in CI but no minimum threshold is enforced — a number that
gets gamed is worse than no number.

## 14. Windows-authored, Linux-verified

Developed on Windows with Docker Desktop; containers run Linux. The cgroup
reading in `ResourceProbe` falls back through cgroup v2 → cgroup v1 → `/proc`,
and only the v2 path has been exercised here. Running the service directly on
macOS (no cgroups, no `/proc`) falls back to a static memory limit.

---

## What I would build next, in order

1. **Postgres/Timescale store** — unblocks HA, horizontal scale, and a 30-day
   rightsizing window in one change. Everything else is downstream of this.
2. **Real cloud connector** — Azure Resource Graph and AWS Resource Groups
   Tagging API behind the existing `Resource` interface.
3. **Alertmanager integration** — routing, inhibition, maintenance windows.
4. **kind cluster in CI** — turn the Kubernetes manifests from validated into
   verified.
5. **Seasonality-aware detection** — so Monday morning is not an anomaly.
