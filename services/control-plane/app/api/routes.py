"""REST API.

Conventions applied throughout:
  * versioned prefix (/api/v1) so the contract can evolve without breaking the
    dashboard or any scripted consumer
  * plural nouns for collections, filters as query parameters, no verbs in paths
  * every list endpoint is bounded by an explicit limit, so no request can ask
    the service to materialise the entire retention window
  * 404 for a resource that does not exist, 400 for a request that cannot be
    honoured, 401 for a missing credential - the client can branch on status
    alone without parsing the body
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..engine.recommendations import summarise
from ..engine.simulator import scenario_catalog
from .deps import ReadAuth, WriteAuth, get_state

router = APIRouter(prefix="/api/v1")


# ------------------------------------------------------------------- models
class IncidentRequest(BaseModel):
    scenario: str = Field(..., description="Scenario id from /incidents/scenarios")
    resource_id: str = Field(..., description="Target resource id from /inventory")
    duration_seconds: int | None = Field(
        default=None, ge=30, le=3600, description="Defaults to the scenario's own duration"
    )
    magnitude: float = Field(default=1.0, ge=0.1, le=3.0)
    note: str = Field(default="", max_length=280)


class AckRequest(BaseModel):
    acknowledged_by: str = Field(default="operator", max_length=120)


# ------------------------------------------------------------------ system
@router.get("/system", tags=["system"], dependencies=[ReadAuth])
def system(ctx=Depends(get_state)) -> dict[str, Any]:
    """Build metadata, effective configuration (secrets redacted) and store stats."""
    return {
        "service": ctx.settings.service_name,
        "version": ctx.settings.version,
        "environment": ctx.settings.environment,
        "started_at": ctx.started_at,
        "uptime_seconds": round(time.time() - ctx.started_at, 1),
        "config": ctx.settings.redacted(),
        "store": ctx.store.stats(),
        "last_collection": ctx.collector.last_tick,
        "inventory": ctx.inventory.summary(),
    }


# --------------------------------------------------------------- inventory
@router.get("/inventory", tags=["inventory"], dependencies=[ReadAuth])
def list_inventory(
    provider: str | None = None,
    type: str | None = None,
    environment: str | None = None,
    ctx=Depends(get_state),
) -> dict[str, Any]:
    rows = [
        r.to_dict()
        for r in ctx.inventory
        if (provider is None or r.provider == provider)
        and (type is None or r.type == type)
        and (environment is None or r.environment == environment)
    ]
    return {"count": len(rows), "summary": ctx.inventory.summary(), "resources": rows}


@router.get("/inventory/{resource_id}", tags=["inventory"], dependencies=[ReadAuth])
def get_resource(resource_id: str, ctx=Depends(get_state)) -> dict[str, Any]:
    resource = ctx.inventory.get(resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail=f"no such resource: {resource_id}")
    now = time.time()
    latest = ctx.store.latest_samples().get(resource_id, {})
    utilization = {m: v for m, (v, _) in latest.items()}
    from ..engine.health import evaluate_resource

    return {
        "resource": resource.to_dict(),
        "health": evaluate_resource(resource, latest, now),
        "cost": ctx.cost.efficiency(resource, utilization),
        "metrics": {m: {"value": round(v, 4), "ts": ts} for m, (v, ts) in latest.items()},
    }


# ----------------------------------------------------------------- metrics
@router.get("/metrics/latest", tags=["metrics"], dependencies=[ReadAuth])
def latest_metrics(ctx=Depends(get_state)) -> dict[str, Any]:
    latest = ctx.store.latest_samples()
    return {
        "ts": time.time(),
        "resources": {
            rid: {m: {"value": round(v, 4), "ts": ts} for m, (v, ts) in metrics.items()}
            for rid, metrics in latest.items()
        },
    }


@router.get("/metrics/series", tags=["metrics"], dependencies=[ReadAuth])
def metric_series(
    resource_id: str,
    metric: str,
    minutes: int = Query(default=60, ge=1, le=1440),
    ctx=Depends(get_state),
) -> dict[str, Any]:
    if ctx.inventory.get(resource_id) is None:
        raise HTTPException(status_code=404, detail=f"no such resource: {resource_id}")
    since = time.time() - minutes * 60
    points = ctx.store.series(resource_id, metric, since)
    return {
        "resource_id": resource_id,
        "metric": metric,
        "minutes": minutes,
        "count": len(points),
        "points": points,
    }


# ------------------------------------------------------------------ health
@router.get("/health", tags=["health"], dependencies=[ReadAuth])
def fleet_health(ctx=Depends(get_state)) -> dict[str, Any]:
    from ..engine.health import evaluate_fleet

    return evaluate_fleet(ctx.inventory.resources, ctx.store.latest_samples(), time.time())


# -------------------------------------------------------------------- cost
@router.get("/cost", tags=["cost"], dependencies=[ReadAuth])
def fleet_cost(ctx=Depends(get_state)) -> dict[str, Any]:
    utilization = {
        rid: {m: v for m, (v, _) in metrics.items()}
        for rid, metrics in ctx.store.latest_samples().items()
    }
    return ctx.cost.fleet(ctx.inventory.resources, utilization)


@router.get("/cost/{resource_id}", tags=["cost"], dependencies=[ReadAuth])
def resource_cost(resource_id: str, ctx=Depends(get_state)) -> dict[str, Any]:
    resource = ctx.inventory.get(resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail=f"no such resource: {resource_id}")
    latest = ctx.store.latest_samples().get(resource_id, {})
    return ctx.cost.efficiency(resource, {m: v for m, (v, _) in latest.items()})


# --------------------------------------------------------------- anomalies
@router.get("/anomalies", tags=["anomalies"], dependencies=[ReadAuth])
def anomalies(
    minutes: int = Query(default=60, ge=1, le=1440),
    limit: int = Query(default=100, ge=1, le=1000),
    ctx=Depends(get_state),
) -> dict[str, Any]:
    rows = ctx.store.recent_anomalies(time.time() - minutes * 60, limit)
    by_severity: dict[str, int] = {}
    for row in rows:
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + 1
    return {"count": len(rows), "by_severity": by_severity, "anomalies": rows}


# ------------------------------------------------------------------ alerts
@router.get("/alerts", tags=["alerts"], dependencies=[ReadAuth])
def alerts(
    status: str | None = Query(default=None, pattern="^(firing|pending|resolved)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    ctx=Depends(get_state),
) -> dict[str, Any]:
    rows = ctx.store.list_alerts(status=status, limit=limit)
    return {"count": len(rows), "summary": ctx.alerts.summary(), "alerts": rows}


@router.get("/alerts/history", tags=["alerts"], dependencies=[ReadAuth])
def alert_history(
    minutes: int = Query(default=180, ge=1, le=1440),
    limit: int = Query(default=100, ge=1, le=1000),
    ctx=Depends(get_state),
) -> dict[str, Any]:
    rows = ctx.store.alert_history(time.time() - minutes * 60, limit)
    return {"count": len(rows), "events": rows}


@router.post("/alerts/{fingerprint}/acknowledge", tags=["alerts"], dependencies=[WriteAuth])
def acknowledge(fingerprint: str, body: AckRequest, ctx=Depends(get_state)) -> dict[str, Any]:
    if not ctx.store.acknowledge_alert(fingerprint, body.acknowledged_by, time.time()):
        raise HTTPException(status_code=404, detail=f"no such alert: {fingerprint}")
    alert = ctx.store.get_alert(fingerprint)
    ctx.store.record_alert_event(alert, "acknowledged", time.time())
    return {"acknowledged": True, "alert": alert}


# -------------------------------------------------------------------- logs
@router.get("/logs", tags=["logs"], dependencies=[ReadAuth])
def logs(
    minutes: int = Query(default=30, ge=1, le=1440),
    level: str | None = Query(default=None, pattern="^(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)$"),
    resource_id: str | None = None,
    contains: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=200, ge=1, le=1000),
    ctx=Depends(get_state),
) -> dict[str, Any]:
    since = time.time() - minutes * 60
    rows = ctx.store.search_logs(since, level, resource_id, contains, limit)
    return {
        "count": len(rows),
        "counts_by_level": ctx.store.log_counts_by_level(since),
        "logs": rows,
    }


# --------------------------------------------------------- recommendations
@router.get("/recommendations", tags=["recommendations"], dependencies=[ReadAuth])
def recommendations(
    category: str | None = None,
    severity: str | None = None,
    minutes: int = Query(default=60, ge=5, le=1440),
    ctx=Depends(get_state),
) -> dict[str, Any]:
    findings = ctx.build_recommendations(minutes)
    if category:
        findings = [f for f in findings if f["category"] == category]
    if severity:
        findings = [f for f in findings if f["severity"] == severity]
    return {"summary": summarise(findings), "recommendations": findings}


# --------------------------------------------------------------- incidents
@router.get("/incidents/scenarios", tags=["incidents"], dependencies=[ReadAuth])
def scenarios() -> dict[str, Any]:
    return {"scenarios": scenario_catalog()}


@router.get("/incidents", tags=["incidents"], dependencies=[ReadAuth])
def list_incidents(
    limit: int = Query(default=50, ge=1, le=200), ctx=Depends(get_state)
) -> dict[str, Any]:
    now = time.time()
    rows = ctx.store.list_incidents(limit)
    for row in rows:
        row["remaining_seconds"] = (
            max(0, round(row["ends_at"] - now)) if row["status"] == "active" else 0
        )
    return {
        "count": len(rows),
        "active": sum(1 for r in rows if r["status"] == "active"),
        "incidents": rows,
    }


@router.post("/incidents", tags=["incidents"], status_code=201, dependencies=[WriteAuth])
async def start_incident(body: IncidentRequest, ctx=Depends(get_state)) -> dict[str, Any]:
    try:
        incident = await ctx.incidents.start(
            body.scenario,
            body.resource_id,
            body.duration_seconds,
            body.magnitude,
            body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return incident


@router.delete("/incidents/{incident_id}", tags=["incidents"], dependencies=[WriteAuth])
async def stop_incident(incident_id: str, ctx=Depends(get_state)) -> dict[str, Any]:
    if not await ctx.incidents.stop(incident_id):
        raise HTTPException(status_code=404, detail=f"no active incident with id {incident_id}")
    return {"cancelled": True, "incident_id": incident_id}


@router.delete("/incidents", tags=["incidents"], dependencies=[WriteAuth])
async def stop_all_incidents(ctx=Depends(get_state)) -> dict[str, Any]:
    return {"cancelled": await ctx.incidents.stop_all()}


# ---------------------------------------------------------------- overview
@router.get("/overview", tags=["dashboard"], dependencies=[ReadAuth])
def overview(ctx=Depends(get_state)) -> dict[str, Any]:
    """Everything the dashboard needs, in one round trip.

    The dashboard polls every few seconds; making it issue eight requests per
    refresh would multiply the load for no benefit. One aggregate endpoint keeps
    the browser simple and the server's work per refresh bounded.
    """
    from ..engine.health import evaluate_fleet

    now = time.time()
    latest = ctx.store.latest_samples()
    utilization = {rid: {m: v for m, (v, _) in ms.items()} for rid, ms in latest.items()}

    health = evaluate_fleet(ctx.inventory.resources, latest, now)
    cost = ctx.cost.fleet(ctx.inventory.resources, utilization)
    findings = ctx.build_recommendations(60)
    incidents = ctx.store.active_incidents(now)
    for incident in incidents:
        incident["remaining_seconds"] = max(0, round(incident["ends_at"] - now))

    cost_by_resource = {r["resource_id"]: r for r in cost["resources"]}
    health_by_resource = {r["resource_id"]: r for r in health["resources"]}
    rows = []
    for resource in ctx.inventory:
        h = health_by_resource.get(resource.id, {})
        c = cost_by_resource.get(resource.id, {})
        rows.append(
            {
                **resource.to_dict(),
                "status": h.get("status", "unknown"),
                "score": h.get("score", 0),
                "reasons": h.get("reasons", []),
                "metrics": h.get("metrics", {}),
                "monthly_cost": c.get("monthly_cost", 0.0),
                "waste_monthly": c.get("waste_monthly", 0.0),
                "efficiency": c.get("efficiency", 0.0),
            }
        )

    return {
        "ts": now,
        "uptime_seconds": round(now - ctx.started_at, 1),
        "fleet": {
            "status": health["status"],
            "score": health["score"],
            "counts": health["counts"],
            "total": health["total"],
        },
        "cost": {
            "currency": cost["currency"],
            "total_monthly": cost["total_monthly"],
            "total_daily": cost["total_daily"],
            "total_annual": cost["total_annual"],
            "waste_monthly": cost["waste_monthly"],
            "waste_pct": cost["waste_pct"],
            "by_provider": cost["by_provider"],
            "by_type": cost["by_type"],
            "by_environment": cost["by_environment"],
            "top_spenders": cost["top_spenders"][:6],
        },
        "alerts": {
            "summary": ctx.alerts.summary(),
            "firing": ctx.store.list_alerts(status="firing", limit=25),
        },
        "anomalies": ctx.store.recent_anomalies(now - 3600, 25),
        "recommendations": {
            "summary": summarise(findings),
            "top": findings[:12],
        },
        "incidents": incidents,
        "logs": ctx.store.search_logs(now - 1800, limit=40),
        "log_counts": ctx.store.log_counts_by_level(now - 1800),
        "resources": rows,
        "last_collection": ctx.collector.last_tick,
    }
