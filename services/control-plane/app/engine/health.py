"""Health monitoring and scoring.

Turns raw metrics into the one thing an operator actually wants on a wall
display: is this resource healthy, and if not, why. The score is a deduction
model - start at 100, subtract for each thing that is wrong - because that is
auditable. Anyone can read the reasons list and reconstruct the number, which
is not true of a weighted-average score.

Status thresholds map to the vocabulary Kubernetes already gave everyone:
    healthy    the thing is serving and within objectives
    degraded   serving, but outside an objective (the "investigate" band)
    unhealthy  failing probes, crash-looping, or saturated
    unknown    no telemetry - which is itself a monitoring failure, not "fine"
"""

from __future__ import annotations

from typing import Any

from .inventory import Resource

# (metric, comparison, threshold, penalty, reason template)
DEDUCTIONS: tuple[tuple[str, str, float, int, str], ...] = (
    ("availability", "lt", 1.0, 60, "health probe failing"),
    ("error_rate", "gt", 0.05, 30, "error rate {value:.1%} above 5%"),
    ("error_rate", "gt", 0.02, 12, "error rate {value:.1%} above 2%"),
    ("cpu_utilization", "gt", 95.0, 25, "CPU saturated at {value:.0f}%"),
    ("cpu_utilization", "gt", 85.0, 12, "CPU high at {value:.0f}%"),
    ("memory_utilization", "gt", 95.0, 25, "memory critical at {value:.0f}%"),
    ("memory_utilization", "gt", 90.0, 12, "memory high at {value:.0f}%"),
    ("disk_utilization", "gt", 90.0, 15, "disk {value:.0f}% full"),
    ("latency_p95_ms", "gt", 1500.0, 20, "p95 latency {value:.0f}ms"),
    ("latency_p95_ms", "gt", 800.0, 10, "p95 latency {value:.0f}ms above SLO"),
    ("restart_count", "gt", 3.0, 30, "{value:.0f} restarts"),
    ("restart_count", "gt", 0.0, 5, "{value:.0f} restart(s) observed"),
)

# Staleness: a sample older than this means the collector has lost the target.
STALE_AFTER_SECONDS = 90.0


def _fails(value: float, comparison: str, threshold: float) -> bool:
    return value > threshold if comparison == "gt" else value < threshold


def evaluate_resource(
    resource: Resource,
    latest: dict[str, tuple[float, float]],
    now: float,
) -> dict[str, Any]:
    """Score one resource from its most recent sample of each metric."""
    if not latest:
        # A resource type with no applicable metrics (an unattached elastic IP
        # emits nothing, ever) is not a monitoring failure and must not drag the
        # fleet status down. A resource that SHOULD report and does not is a
        # genuine blind spot and stays "unknown".
        expected = bool(resource.metrics)
        return {
            "resource_id": resource.id,
            "name": resource.name,
            "type": resource.type,
            "provider": resource.provider,
            "environment": resource.environment,
            "owner": resource.owner,
            "region": resource.region,
            "source": resource.source,
            "status": "unknown" if expected else "not_monitored",
            "score": 0 if expected else 100,
            "reasons": (
                ["no telemetry collected"]
                if expected
                else [f"{resource.type} emits no runtime metrics"]
            ),
            "metrics": {},
            "stale": expected,
            "last_seen": None,
        }

    values = {m: v for m, (v, _) in latest.items()}
    newest_ts = max(ts for _, ts in latest.values())
    stale = (now - newest_ts) > STALE_AFTER_SECONDS

    score = 100
    reasons: list[str] = []
    seen_metrics: set[str] = set()

    for metric, comparison, threshold, penalty, template in DEDUCTIONS:
        if metric not in values or metric in seen_metrics:
            continue
        value = values[metric]
        if _fails(value, comparison, threshold):
            score -= penalty
            reasons.append(template.format(value=value))
            # Only the most severe band for a given metric counts, so a box at
            # 97% CPU is not penalised twice for also being above 85%.
            seen_metrics.add(metric)

    if stale:
        score -= 40
        reasons.append(f"telemetry stale ({int(now - newest_ts)}s old)")

    # Configuration gaps are reliability risk, not just audit findings.
    if resource.config.get("health_check") is False:
        score -= 8
        reasons.append("no health check configured")

    score = max(0, min(100, score))
    if stale and score < 60:
        status = "unknown"
    elif score >= 85:
        status = "healthy"
    elif score >= 60:
        status = "degraded"
    else:
        status = "unhealthy"

    return {
        "resource_id": resource.id,
        "name": resource.name,
        "type": resource.type,
        "provider": resource.provider,
        "environment": resource.environment,
        "owner": resource.owner,
        "region": resource.region,
        "source": resource.source,
        "status": status,
        "score": score,
        "reasons": reasons,
        "metrics": {k: round(v, 4) for k, v in values.items()},
        "stale": stale,
        "last_seen": newest_ts,
        "age_seconds": round(now - newest_ts, 1),
    }


def evaluate_fleet(
    resources: list[Resource],
    latest: dict[str, dict[str, tuple[float, float]]],
    now: float,
) -> dict[str, Any]:
    rows = [evaluate_resource(r, latest.get(r.id, {}), now) for r in resources]
    counts = {"healthy": 0, "degraded": 0, "unhealthy": 0, "unknown": 0, "not_monitored": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    scored = [
        r["score"] for r in rows if r["status"] not in ("unknown", "not_monitored")
    ]
    fleet_score = round(sum(scored) / len(scored), 1) if scored else 0.0

    # The fleet is only as healthy as its worst production resource: one
    # unhealthy prod service outweighs a hundred idle healthy ones.
    if counts["unhealthy"]:
        fleet_status = "unhealthy"
    elif counts["degraded"] or counts["unknown"]:
        fleet_status = "degraded"
    else:
        fleet_status = "healthy"

    return {
        "status": fleet_status,
        "score": fleet_score,
        "counts": counts,
        "total": len(rows),
        "resources": sorted(rows, key=lambda r: (r["score"], r["name"])),
        "unhealthy": [r for r in rows if r["status"] in ("unhealthy", "degraded")],
    }
