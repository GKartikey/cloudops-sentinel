"""Recommendation engine.

Rule-based, and every recommendation must carry three things or it does not get
emitted:

  evidence    the numbers that triggered it, so the owner can check the working
  action      a specific change, not "consider optimising"
  saving      a dollar figure where one exists, so it can be prioritised

The engine refuses to recommend anything it lacks evidence for: fewer than
`min_samples` observations and the rightsizing analysers stay silent. Confidently
telling someone to halve a production database off four data points is how a
cost tool loses its credibility permanently.

Analyser categories map one-to-one to the required findings:
    over_provisioned, under_utilised, unhealthy_container, high_error_rate,
    suspicious_configuration, missing_health_check
plus orphaned_resource, storage_tiering, commitment and scheduling analysers.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any

from .inventory import COMPUTE_TYPES, Resource

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Standard sizes to snap a rightsizing proposal to. Recommending "6.4 vCPU" is
# useless because no cloud sells it; we round down to a real purchasable shape.
VCPU_LADDER = (0.25, 0.5, 1, 2, 4, 8, 16, 32, 64)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * p
    low, high = int(idx), min(int(idx) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (idx - low)


def _next_size_down(current: float, needed: float) -> float:
    """Smallest ladder rung that still covers `needed`, below `current`."""
    candidates = [s for s in VCPU_LADDER if s >= needed and s < current]
    return min(candidates) if candidates else current


class RecommendationEngine:
    def __init__(self, thresholds: dict, cost_model) -> None:
        t = thresholds or {}
        self.over_cpu = float(t.get("overprovisioned_cpu_p95", 40))
        self.over_mem = float(t.get("overprovisioned_mem_p95", 50))
        self.under_cpu = float(t.get("underutilised_cpu_p95", 10))
        self.under_mem = float(t.get("underutilised_mem_p95", 20))
        self.min_samples = int(t.get("min_samples", 8))
        self.target_cpu = float(t.get("target_cpu_after_resize", 60))
        self.high_error_rate = float(t.get("high_error_rate", 0.03))
        self.unhealthy_restarts = float(t.get("unhealthy_restart_count", 3))
        self.storage_min_gb = float(t.get("storage_tiering_min_gb", 500))
        self.cost = cost_model

    # ------------------------------------------------------------------ entry
    def analyse(
        self,
        resources: Iterable[Resource],
        window: dict[str, dict[str, list[float]]],
        health_rows: dict[str, dict],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for resource in resources:
            series = window.get(resource.id, {})
            stats = self._summarise(series)
            health = health_rows.get(resource.id, {})
            for analyser in (
                self._orphaned,
                self._over_provisioned,
                self._under_utilised,
                self._unhealthy_container,
                self._high_error_rate,
                self._suspicious_configuration,
                self._missing_health_check,
                self._storage_tiering,
                self._non_prod_schedule,
                self._commitment,
            ):
                findings.extend(analyser(resource, stats, health) or [])

        findings.sort(
            key=lambda f: (
                SEVERITY_ORDER.get(f["severity"], 9),
                -f.get("monthly_saving", 0.0),
            )
        )
        return findings

    def _summarise(self, series: dict[str, list[float]]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for metric, values in series.items():
            if not values:
                continue
            out[metric] = {
                "n": len(values),
                "avg": statistics.fmean(values),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "max": max(values),
                "min": min(values),
                "last": values[-1],
            }
        return out

    @staticmethod
    def _finding(
        resource: Resource,
        category: str,
        severity: str,
        title: str,
        rationale: str,
        action: str,
        evidence: dict,
        monthly_saving: float = 0.0,
        confidence: str = "medium",
        effort: str = "low",
        risk: str = "low",
    ) -> dict[str, Any]:
        return {
            "id": f"{category}:{resource.id}",
            "category": category,
            "severity": severity,
            "resource_id": resource.id,
            "resource_name": resource.name,
            "resource_type": resource.type,
            "provider": resource.provider,
            "environment": resource.environment,
            "owner": resource.owner,
            "region": resource.region,
            "title": title,
            "rationale": rationale,
            "action": action,
            "evidence": evidence,
            "monthly_saving": round(monthly_saving, 2),
            "annual_saving": round(monthly_saving * 12, 2),
            "confidence": confidence,
            "effort": effort,
            "risk": risk,
        }

    # ------------------------------------------------------------- analysers
    def _orphaned(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        """Detached disks and unattached IPs bill at full rate for zero value."""
        if resource.type not in ("managed_disk", "public_ip"):
            return []
        if resource.spec.get("attached", True):
            return []
        monthly = self.cost.estimate(resource)["monthly_cost"]
        return [
            self._finding(
                resource,
                "orphaned_resource",
                "high",
                f"Orphaned {resource.type.replace('_', ' ')} billing at full rate",
                (
                    f"{resource.name} is not attached to any workload but is still "
                    f"provisioned, costing ${monthly:.2f}/month for nothing."
                ),
                f"Snapshot if the data is needed, then delete {resource.name}.",
                {"attached": False, "sku": resource.sku, "monthly_cost": monthly},
                monthly_saving=monthly,
                confidence="high",
                risk="medium",  # deleting a disk is irreversible without a snapshot
            )
        ]

    def _over_provisioned(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        if resource.type not in COMPUTE_TYPES:
            return []
        cpu = stats.get("cpu_utilization")
        mem = stats.get("memory_utilization")
        if not cpu or cpu["n"] < self.min_samples:
            return []
        cpu_p95, mem_p95 = cpu["p95"], (mem or {}).get("p95", 0.0)
        # Under-utilised is a separate, stronger finding; do not emit both.
        if cpu_p95 < self.under_cpu and mem_p95 < self.under_mem:
            return []
        if cpu_p95 >= self.over_cpu or mem_p95 >= self.over_mem:
            return []

        current_vcpu = resource.vcpu
        if current_vcpu <= 0.25:
            return []
        # Size for the observed peak plus headroom to the target utilisation.
        needed_vcpu = current_vcpu * (cpu_p95 / self.target_cpu)
        proposed_vcpu = _next_size_down(current_vcpu, max(needed_vcpu, 0.25))
        if proposed_vcpu >= current_vcpu:
            return []

        ratio = proposed_vcpu / current_vcpu
        proposed_mem = round(max(resource.memory_gb * ratio, 0.25), 2)
        # Never shrink memory below the observed peak plus 25% headroom.
        if mem_p95:
            floor = resource.memory_gb * (mem_p95 / 100.0) * 1.25
            proposed_mem = max(proposed_mem, round(floor, 2))
        delta = self.cost.resize_saving(resource, proposed_vcpu, proposed_mem)
        if delta["monthly_saving"] <= 0.5:
            return []

        return [
            self._finding(
                resource,
                "over_provisioned",
                "medium",
                f"Rightsize {resource.name}: {current_vcpu:g} -> {proposed_vcpu:g} vCPU",
                (
                    f"p95 CPU is {cpu_p95:.1f}% and p95 memory {mem_p95:.1f}% over "
                    f"{cpu['n']} samples. The allocation is roughly "
                    f"{current_vcpu / max(needed_vcpu, 0.01):.1f}x what the workload needs."
                ),
                (
                    f"Resize to {proposed_vcpu:g} vCPU / {proposed_mem:g} GiB "
                    f"(saves ${delta['monthly_saving']:.2f}/month). Change it in "
                    "non-prod first and watch p95 latency for one full traffic cycle."
                ),
                {
                    "cpu_p95": round(cpu_p95, 2),
                    "memory_p95": round(mem_p95, 2),
                    "samples": cpu["n"],
                    "current_vcpu": current_vcpu,
                    "proposed_vcpu": proposed_vcpu,
                    **delta,
                },
                monthly_saving=delta["monthly_saving"],
                confidence="high" if cpu["n"] >= self.min_samples * 4 else "medium",
                risk="medium",
            )
        ]

    def _under_utilised(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        """Barely-used resources: the question is deletion, not resizing."""
        if resource.type not in COMPUTE_TYPES:
            return []
        cpu = stats.get("cpu_utilization")
        if not cpu or cpu["n"] < self.min_samples:
            return []
        mem_p95 = (stats.get("memory_utilization") or {}).get("p95", 0.0)
        rps = (stats.get("requests_per_second") or {}).get("avg", 0.0)
        if cpu["p95"] >= self.under_cpu or mem_p95 >= self.under_mem:
            return []

        monthly = self.cost.estimate(resource)["monthly_cost"]
        # Idle capacity is the portion of the bill doing nothing useful.
        saving = round(monthly * 0.75, 2)
        return [
            self._finding(
                resource,
                "under_utilised",
                "high" if monthly > 200 else "medium",
                f"{resource.name} is idle ({cpu['p95']:.1f}% p95 CPU)",
                (
                    f"p95 CPU {cpu['p95']:.1f}%, p95 memory {mem_p95:.1f}%, average "
                    f"{rps:.1f} req/s over {cpu['n']} samples, while the bill is "
                    f"${monthly:.2f}/month. This resource is being paid for and not used."
                ),
                (
                    "Confirm ownership, then decommission it - or if it must exist, "
                    f"move it to the smallest supported SKU or a serverless/spot tier. "
                    f"Retiring it recovers about ${saving:.2f}/month."
                ),
                {
                    "cpu_p95": round(cpu["p95"], 2),
                    "memory_p95": round(mem_p95, 2),
                    "avg_rps": round(rps, 2),
                    "samples": cpu["n"],
                    "monthly_cost": monthly,
                },
                monthly_saving=saving,
                confidence="high",
                risk="high",  # decommissioning needs an owner sign-off
                effort="medium",
            )
        ]

    def _unhealthy_container(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        restarts = stats.get("restart_count")
        availability = stats.get("availability")
        status = health.get("status")
        problems: list[str] = []

        if restarts and restarts["max"] >= self.unhealthy_restarts:
            problems.append(f"{restarts['max']:.0f} restarts observed")
        if availability and availability["avg"] < 0.95:
            problems.append(f"health probe succeeded only {availability['avg']:.0%} of the time")
        if status == "unhealthy" and not problems:
            problems.append("; ".join(health.get("reasons", [])[:3]) or "health score below 60")
        if not problems:
            return []

        return [
            self._finding(
                resource,
                "unhealthy_container",
                "critical",
                f"{resource.name} is unhealthy",
                "; ".join(problems) + ".",
                (
                    "Pull the last 100 error-level log lines for this resource, check "
                    "the exit code and OOM status of the previous run, and verify the "
                    "liveness/readiness probe thresholds are not tripping a slow start. "
                    "If it is an OOM kill, raise the memory limit before restarting."
                ),
                {
                    "restart_max": (restarts or {}).get("max"),
                    "availability_avg": round((availability or {}).get("avg", 1.0), 4),
                    "health_score": health.get("score"),
                    "health_reasons": health.get("reasons", []),
                },
                confidence="high",
                effort="medium",
            )
        ]

    def _high_error_rate(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        errors = stats.get("error_rate")
        if not errors or errors["n"] < 3:
            return []
        if errors["p95"] < self.high_error_rate:
            return []
        rps = (stats.get("requests_per_second") or {}).get("avg", 0.0)
        failed_per_hour = errors["avg"] * rps * 3600
        return [
            self._finding(
                resource,
                "high_error_rate",
                "critical" if errors["p95"] >= 0.10 else "high",
                f"{resource.name} is failing {errors['p95']:.1%} of requests (p95)",
                (
                    f"p95 error rate {errors['p95']:.2%}, mean {errors['avg']:.2%} over "
                    f"{errors['n']} samples at {rps:.0f} req/s - roughly "
                    f"{failed_per_hour:,.0f} failed requests per hour reaching users."
                ),
                (
                    "Correlate the error onset with the most recent deployment and with "
                    "this resource's dependency latency. If they line up, roll back "
                    "first and diagnose after; if not, check the downstream dependency "
                    "and the circuit-breaker configuration."
                ),
                {
                    "error_rate_p95": round(errors["p95"], 4),
                    "error_rate_avg": round(errors["avg"], 4),
                    "samples": errors["n"],
                    "avg_rps": round(rps, 2),
                    "failed_requests_per_hour": round(failed_per_hour),
                },
                confidence="high",
                effort="medium",
            )
        ]

    def _suspicious_configuration(
        self, resource: Resource, stats: dict, health: dict
    ) -> list[dict]:
        """Posture checks. Each maps to a real CIS / Well-Architected control."""
        config = resource.config or {}
        issues: list[dict] = []

        def add(severity: str, issue: str, control: str, fix: str) -> None:
            issues.append({"severity": severity, "issue": issue, "control": control, "fix": fix})

        if config.get("encryption_at_rest") is False:
            add(
                "critical",
                "Data is stored unencrypted at rest",
                "CIS / encryption-at-rest",
                "Enable platform-managed disk or storage encryption and re-key.",
            )
        if config.get("public_access") is True:
            add(
                "critical",
                "Anonymous public read access is enabled on a storage container",
                "CIS / no public buckets",
                "Disable anonymous access; issue time-boxed SAS tokens or presigned URLs.",
            )
        if config.get("public_ip") is True and resource.type == "managed_database":
            add(
                "critical",
                "Database is reachable from the public internet",
                "least privilege / network isolation",
                "Move it behind a private endpoint or VPC-only subnet and drop the public IP.",
            )
        if config.get("public_ip") is True and config.get("open_ports"):
            ports = config["open_ports"]
            add(
                "critical",
                f"Public IP with management ports {ports} exposed",
                "least privilege / no management ports on the internet",
                "Remove the public IP and reach the host through a bastion, "
                "Azure Bastion, or SSM Session Manager instead.",
            )
        if config.get("https_only") is False:
            add(
                "high",
                "Plaintext HTTP is accepted",
                "encryption-in-transit",
                "Force HTTPS-only and set a minimum TLS version of 1.2.",
            )
        if config.get("managed_identity") is False:
            add(
                "high",
                "Authenticates with static credentials instead of a workload identity",
                "no long-lived secrets",
                "Switch to a managed identity / IAM role for service accounts so no "
                "secret has to be stored or rotated at all.",
            )
        if config.get("diagnostic_logs") is False:
            add(
                "medium",
                "Diagnostic logging is disabled",
                "auditability",
                "Enable resource logs and ship them to the central workspace; without "
                "them an incident on this resource cannot be reconstructed.",
            )
        if (
            config.get("backup_enabled") is False
            and resource.environment == "prod"
            and resource.type in ("virtual_machine", "managed_database")
        ):
            add(
                "high",
                "Production resource has no backup configured",
                "recoverability",
                "Attach a backup policy with a retention period that meets the RPO.",
            )
        if resource.owner in ("unassigned", "", None):
            add(
                "medium",
                "Resource has no owner tag",
                "cost accountability",
                "Tag with owner and cost_center; untagged resources are the ones "
                "nobody dares delete and everybody keeps paying for.",
            )

        if not issues:
            return []

        worst = min(issues, key=lambda i: SEVERITY_ORDER.get(i["severity"], 9))["severity"]
        return [
            self._finding(
                resource,
                "suspicious_configuration",
                worst,
                f"{len(issues)} configuration finding(s) on {resource.name}",
                "; ".join(i["issue"] for i in issues) + ".",
                " ".join(f"({n + 1}) {i['fix']}" for n, i in enumerate(issues)),
                {"issues": issues, "config": config},
                confidence="high",
                effort="low",
            )
        ]

    def _missing_health_check(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        if resource.config.get("health_check") is not False:
            return []
        if resource.type in ("managed_disk", "public_ip", "object_storage"):
            return []
        return [
            self._finding(
                resource,
                "missing_health_check",
                "high" if resource.environment == "prod" else "medium",
                f"{resource.name} has no health check configured",
                (
                    "Without a probe the platform cannot tell a hung process from a "
                    "healthy one: traffic keeps being routed to it, autoscaling and "
                    "rolling deploys have nothing to gate on, and failure is only "
                    "noticed when a user reports it."
                ),
                (
                    "Add a liveness probe (is the process wedged - restart it) and a "
                    "readiness probe (can it serve right now - take it out of the load "
                    "balancer). Point them at a lightweight endpoint that does not call "
                    "downstream dependencies, or one slow dependency will cascade into "
                    "a fleet-wide restart storm."
                ),
                {
                    "health_check": False,
                    "environment": resource.environment,
                    "type": resource.type,
                },
                confidence="high",
                effort="low",
            )
        ]

    def _storage_tiering(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        if resource.type != "object_storage":
            return []
        gb = float(resource.spec.get("storage_gb", 0) or 0)
        if gb < self.storage_min_gb or resource.config.get("lifecycle_policy") is not False:
            return []
        saving = self.cost.tiering_saving(gb * 0.7, resource.region)
        return [
            self._finding(
                resource,
                "storage_tiering",
                "medium",
                f"{resource.name}: {gb:,.0f} GB on a hot tier with no lifecycle policy",
                (
                    f"All {gb:,.0f} GB is billed at the hot/standard rate. Archive data "
                    "is normally read a handful of times after the first month, so the "
                    "majority of it is paying a premium for access it never receives."
                ),
                (
                    "Add a lifecycle rule moving blobs to cool/infrequent-access after "
                    "30 days and to archive after 180. Assuming 70% of the data is cold, "
                    f"that recovers roughly ${saving:.2f}/month."
                ),
                {"storage_gb": gb, "lifecycle_policy": False, "assumed_cold_pct": 70},
                monthly_saving=saving,
                confidence="medium",
                effort="low",
            )
        ]

    def _non_prod_schedule(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        if resource.environment not in ("staging", "dev", "test", "qa"):
            return []
        if resource.type not in COMPUTE_TYPES:
            return []
        monthly = self.cost.estimate(resource)["monthly_cost"]
        if monthly < 20:
            return []
        saving = self.cost.schedule_saving(monthly)
        return [
            self._finding(
                resource,
                "scheduling",
                "medium",
                f"{resource.name} runs 24/7 in {resource.environment}",
                (
                    f"A {resource.environment} resource costing ${monthly:.2f}/month is "
                    "billed for all 730 hours, but is only needed during working hours - "
                    "roughly 36% of them."
                ),
                (
                    "Attach a start/stop schedule (Azure Automation, an EventBridge rule, "
                    f"or a CronJob) covering 08:00-20:00 Mon-Fri. Saves ~${saving:.2f}/month."
                ),
                {"monthly_cost": monthly, "hours_billed": 730, "hours_needed": 261},
                monthly_saving=saving,
                confidence="high",
                effort="low",
            )
        ]

    def _commitment(self, resource: Resource, stats: dict, health: dict) -> list[dict]:
        """Steady prod compute is the textbook case for a reserved commitment."""
        if resource.type not in ("virtual_machine", "kubernetes_node", "managed_database"):
            return []
        if resource.environment != "prod":
            return []
        cpu = stats.get("cpu_utilization")
        if not cpu or cpu["n"] < self.min_samples:
            return []
        # Only recommend a 1-3 year commitment on something that is actually busy;
        # committing to an idle box locks in the waste instead of removing it.
        if cpu["p95"] < self.over_cpu:
            return []
        monthly = self.cost.estimate(resource)["monthly_cost"]
        if monthly < 50:
            return []
        saving = self.cost.commitment_saving(monthly, "reserved_1yr")
        return [
            self._finding(
                resource,
                "commitment_discount",
                "low",
                f"{resource.name} is a candidate for a 1-year commitment",
                (
                    f"Steady production workload at {cpu['p95']:.0f}% p95 CPU costing "
                    f"${monthly:.2f}/month on on-demand pricing. Utilisation is high "
                    "enough that the capacity is genuinely needed."
                ),
                (
                    "Rightsize first, then buy a 1-year Reserved Instance / Savings Plan "
                    f"for the resulting size (~${saving:.2f}/month). Buying the commitment "
                    "before rightsizing locks in the oversized shape for a year."
                ),
                {
                    "cpu_p95": round(cpu["p95"], 2),
                    "monthly_cost": monthly,
                    "discount_pct": round(
                        self.cost.commitments.get("reserved_1yr_discount", 0) * 100
                    ),
                },
                monthly_saving=saving,
                confidence="medium",
                effort="low",
                risk="medium",  # a commitment is a contractual lock-in
            )
        ]


def summarise(findings: list[dict]) -> dict[str, Any]:
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for f in findings:
        by_category[f["category"]] = by_category.get(f["category"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
    monthly = round(sum(f.get("monthly_saving", 0.0) for f in findings), 2)
    return {
        "total": len(findings),
        "by_category": by_category,
        "by_severity": by_severity,
        "monthly_saving": monthly,
        "annual_saving": round(monthly * 12, 2),
        "security_findings": by_category.get("suspicious_configuration", 0),
        "reliability_findings": (
            by_category.get("unhealthy_container", 0)
            + by_category.get("high_error_rate", 0)
            + by_category.get("missing_health_check", 0)
        ),
    }
