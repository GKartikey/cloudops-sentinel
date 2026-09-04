# Cost model

## The idea the whole thing turns on

> **You are billed for what you provisioned, not for what you used.**

A 16-vCPU database running at 9% CPU costs exactly the same as one running at
90%. That gap is the entire subject of FinOps, and it is invisible to a billing
export — the invoice shows the cost, never the waste. Utilisation lives in the
monitoring system, price lives in the billing system, and the number that
matters only exists when you join them.

CloudOps Sentinel produces two figures for every resource:

| Figure | Meaning |
|---|---|
| `monthly_cost` | What the rate card charges for the **capacity allocated** |
| `effective_monthly_cost` | What that capacity would cost if sized to **observed p95 utilisation** |
| `waste_monthly` | The difference — spend that buys nothing |

## Efficiency is driven by the dominant dimension

```python
util = max(cpu_p95, memory_p95)
efficiency = min(util / 80.0, 1.0)
```

Two decisions in three lines, both of which get challenged and both of which are
deliberate:

**`max`, not average.** A box at 80% memory and 5% CPU is not 42% efficient. It
is 80% efficient, because you cannot shrink it past the dimension that is
actually full. Averaging the dimensions would recommend halving a box that would
immediately start OOM-killing.

**Divide by 80, not 100.** Headroom is not waste. A resource sitting at 80%
utilisation is correctly sized — you need room for a traffic spike, a failover,
a garbage collection pause. Treating 100% as the efficiency target would flag
every well-run production service as 20% wasteful, and the report would be
dismissed by the people who most need to read it.

Consumption-billed types — object storage, serverless — are always 100%
efficient by definition. You cannot over-provision something billed per request.
Their optimisation lever is tiering and right-memory-sizing instead, which is why
those are separate analysers.

## Rate card

`config/pricing.yaml`. Roughly US-East list prices circa 2024, in the right
order of magnitude but **not a billing source of truth**.

```yaml
virtual_machine:
  per_vcpu_hour:      0.0415
  per_gb_ram_hour:    0.0056
  per_gb_disk_month:  0.088
managed_database:
  per_vcpu_hour:      0.1180     # managed services carry a premium
  service_premium:    1.15
object_storage:
  per_gb_month:       0.0230
  cool_per_gb_month:  0.0125     # the tiering saving
serverless_function:
  per_million_invocations: 0.20
  per_gb_second:      0.0000166667
  free_invocations_per_month: 1000000    # the free tier IS modelled
```

Regional multipliers (`westeurope: 1.12`) are applied on top. Worked example for
`prod-web-01` — 8 vCPU, 32 GiB, 256 GB disk, eastus:

```
compute_vcpu    8  × 0.0415 × 730 =  242.36
compute_memory  32 × 0.0056 × 730 =  130.82
storage         256 × 0.088       =   22.53
                                    -------
monthly                              395.71   × 1.00 region = $395.71
```

Verified by a unit test that recomputes it from the YAML independently.

## The ten analysers

Every recommendation carries **evidence** (the numbers that triggered it), an
**action** (a specific change, not "consider optimising") and a **dollar
figure** where one exists.

| Analyser | Trigger | Proposed action | Typical saving |
|---|---|---|---|
| `over_provisioned` | p95 CPU 10–40% over ≥8 samples | Resize down the ladder | 40–60% of the resource |
| `under_utilised` | p95 CPU <10% **and** memory <20% | Decommission, or smallest SKU / spot | 75% of the resource |
| `orphaned_resource` | Disk or IP with `attached: false` | Snapshot, then delete | 100% |
| `storage_tiering` | >500 GB hot tier, no lifecycle policy | Lifecycle rule → cool at 30d, archive at 180d | ~45% of 70% of the data |
| `scheduling` | Non-prod compute costing >$20/mo | Start/stop schedule, 08:00–20:00 Mon–Fri | 64% (730 → 261 hours) |
| `commitment_discount` | Prod, p95 CPU ≥40%, >$50/mo | 1-year RI / Savings Plan | 28% |
| `unhealthy_container` | ≥3 restarts, or availability <95% | Check exit code, OOM status, probe thresholds | — |
| `high_error_rate` | p95 error rate ≥3% | Correlate with deploys; roll back first | — |
| `suspicious_configuration` | Posture flags (see below) | Per-issue remediation | — |
| `missing_health_check` | `health_check: false` | Add liveness + readiness probes | — |

### Rightsizing that will not cause an incident

```python
needed_vcpu   = current_vcpu × (cpu_p95 / 60)      # target 60% after resize
proposed_vcpu = next rung DOWN the ladder that still covers needed_vcpu
proposed_mem  = max(proportional, memory_p95 × 1.25)   # never below observed peak
```

Three guards:

1. **Snap to a real SKU.** "6.4 vCPU" is useless — no cloud sells it. Round down
   the ladder `0.25, 0.5, 1, 2, 4, 8, 16, 32, 64`.
2. **Target 60%, not 100%.** Sizing to the observed peak leaves zero headroom,
   and the first traffic spike after the change becomes an incident that gets
   blamed on the cost tool. Correctly — it would be the cost tool's fault.
3. **Memory never shrinks below the observed peak + 25%.** CPU pressure makes a
   service slow. Memory pressure makes it die. They are not symmetric risks and
   the model does not treat them as such.

### It refuses to guess

Under `min_samples` (default 8) observations, the sizing analysers stay silent
entirely. Confidently telling someone to halve a production database off four
data points is how a cost tool loses its credibility permanently — and it only
gets one chance.

`confidence` is `high` only above 4× the minimum sample count.

### Rightsize before you commit

The `commitment_discount` analyser deliberately skips anything under 40% p95
CPU, and its action text says to rightsize first. Buying a 1-year reservation
for an oversized instance locks in the waste for a year — the discount looks
like a saving on the invoice while costing more than the correct fix.

For the same reason `risk` is `medium` on commitments and `high` on
decommissioning: a contractual lock-in and an irreversible delete are not
low-risk actions just because they save money.

### Posture checks in the cost engine

Configuration findings sit in the same report as cost findings on purpose. The
resources nobody owns are the ones that are both insecure *and* expensive — the
untagged, unencrypted, unmonitored jumpbox from 2023 that everyone is afraid to
delete. `owner: unassigned` is flagged as a **cost accountability** issue for
exactly that reason.

Checks: unencrypted at rest, public storage access, publicly reachable database,
public IP with management ports, plaintext HTTP, static credentials instead of
workload identity, diagnostic logs disabled, prod without backup, no owner tag.

## What the demo estate produces

19 resources, ~$7,530/month, ~$4,200/month of waste (55%), 27 findings worth
**~$4,600/month — $55,000/year**. The headline is `rds-analytics`: a
`db.r6g.4xlarge` at 9% CPU costing $2,715/month.

The waste ratio is high on purpose — the inventory was written with realistic
failure modes rather than a tidy estate. A real mature environment runs 20–35%.

## Limitations — read before quoting a number

This model is **directionally right and precisely wrong.** It is built to make
the optimisation logic demonstrable offline, not to reconcile with an invoice.

Not modelled:
- Enterprise Agreements, negotiated discounts, existing reservations or Savings
  Plans, credits, marketplace charges
- Data egress — frequently 10–20% of a real bill, and the single most commonly
  underestimated line item
- Inter-AZ and cross-region transfer, NAT gateway processing
- Snapshot and backup storage, IOPS/throughput provisioning beyond flat per-GB
- Support plan percentages, taxes
- Spot interruption rates, so the 70% spot discount is an upper bound
- Reserved-instance amortisation and blended vs unblended rates

Utilisation is computed over the retention window (24h default). Real rightsizing
should use **14–30 days** to capture weekly cycles, month-end batch runs, and
seasonal peaks. A 24-hour window will happily recommend shrinking a box that is
only busy on the last day of the month.

### Making it real

- **Azure**: Retail Prices API for rates, Cost Management API for actuals,
  Azure Advisor for a cross-check on the recommendations
- **AWS**: Price List API for rates, Cost and Usage Report to S3 → Athena for
  actuals, Compute Optimizer for a cross-check
- Replace `config/pricing.yaml` and extend the window to 30 days. The analyser
  logic does not change.
