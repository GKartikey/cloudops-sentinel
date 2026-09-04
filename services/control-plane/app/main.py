"""CloudOps Sentinel control plane - application assembly.

Wiring happens once, in `lifespan`, and every component is handed its
dependencies explicitly rather than reaching for a global. That is what lets the
tests construct a Context against a temporary SQLite file and exercise the whole
analysis path without a network, a container, or a running server.

Probe endpoints are separated the way Kubernetes expects, and the distinction is
load-bearing rather than decorative:

  /healthz  liveness  - is this process wedged? Answer without touching the
                        database, because a slow query must not get the pod
                        killed and restarted into the same slow query.
  /readyz   readiness - should this pod receive traffic? Here we DO check the
                        store and whether a collection has completed, because a
                        control plane with no data should be taken out of the
                        load balancer rather than serving empty dashboards.
  /metrics  the Prometheus scrape target.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import metrics as prom
from .api.routes import router as api_router
from .core.config import settings
from .core.logging_setup import (
    configure_logging,
    get_logger,
    new_correlation_id,
    set_correlation_id,
)
from .core.store import Store
from .engine.alerts import AlertEngine, LogSink, WebhookSink, load_rules
from .engine.anomaly import AnomalyDetector
from .engine.collector import Collector
from .engine.cost import CostModel
from .engine.health import evaluate_fleet
from .engine.incidents import IncidentManager
from .engine.inventory import load_inventory, load_yaml
from .engine.recommendations import RecommendationEngine
from .engine.simulator import TelemetrySimulator

log = get_logger("cloudops.main")

STATIC_DIR = Path(__file__).parent / "static"


class Context:
    """Everything the request handlers and the collector share."""

    def __init__(self) -> None:
        self.settings = settings
        self.started_at = time.time()

        self.store = Store(settings.db_path)
        self.inventory = load_inventory(settings.inventory_path)
        pricing = load_yaml(settings.pricing_path)
        rules_config = load_yaml(settings.rules_path)

        self.cost = CostModel(pricing)
        self.simulator = TelemetrySimulator(settings.simulation_seed)
        self.detector = AnomalyDetector(
            z_threshold=settings.anomaly_z_threshold,
            min_samples=settings.anomaly_min_samples,
        )

        rules, resolve_after = load_rules(rules_config)
        sinks: list[Any] = []
        if settings.alert_log_enabled:
            sinks.append(LogSink())
        if settings.alert_webhook_url:
            sinks.append(WebhookSink(settings.alert_webhook_url))
        self.alerts = AlertEngine(self.store, rules, resolve_after, sinks)

        self.recommender = RecommendationEngine(rules_config.get("recommendations", {}), self.cost)
        self.incidents = IncidentManager(self.store, self.inventory)
        self.collector = Collector(
            self.store,
            self.inventory,
            self.simulator,
            self.detector,
            self.alerts,
            self.cost,
            settings,
        )

        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

        log.info(
            "context initialised",
            extra={
                "context": {
                    "resources": len(self.inventory),
                    "live_targets": len(self.inventory.live),
                    "alert_rules": len(rules),
                    "alert_sinks": [getattr(s, "name", "?") for s in sinks],
                    "db": str(settings.db_path),
                }
            },
        )

    def build_recommendations(self, minutes: int = 60) -> list[dict]:
        now = time.time()
        window = self.store.window_values(now - minutes * 60)
        latest = self.store.latest_samples()
        health = evaluate_fleet(self.inventory.resources, latest, now)
        health_rows = {r["resource_id"]: r for r in health["resources"]}
        findings = self.recommender.analyse(self.inventory.resources, window, health_rows)
        by_category: dict[str, int] = {}
        for f in findings:
            by_category[f["category"]] = by_category.get(f["category"], 0) + 1
        for category, count in by_category.items():
            prom.OPEN_RECOMMENDATIONS.labels(category=category).set(count)
        prom.FLEET_SAVING.set(sum(f.get("monthly_saving", 0.0) for f in findings))
        return findings

    async def start_background(self) -> None:
        await self.collector.start()
        if not self.store.get_meta("backfilled_at"):
            # Blocking is deliberate: readiness should be false until there is
            # data, rather than the dashboard flashing an empty state.
            self.collector.backfill(self.settings.backfill_hours)
        await self.collector.tick()
        self._task = asyncio.create_task(self.collector.run_forever(self._stop))

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        await self.collector.stop()
        self.store.close()

    def ready(self) -> tuple[bool, dict]:
        checks = {
            "store": False,
            "inventory": len(self.inventory) > 0,
            "collector": bool(self.collector.last_tick.get("ts")),
        }
        try:
            self.store.query_one("SELECT 1 AS ok")
            checks["store"] = True
        except Exception as exc:  # noqa: BLE001
            log.error("readiness store check failed", extra={"context": {"error": str(exc)}})
        return all(checks.values()), checks


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        service=settings.service_name,
        environment=settings.environment,
        version=settings.version,
    )
    log.info(
        "starting control plane",
        extra={"context": {"version": settings.version, "port": settings.port}},
    )
    prom.BUILD_INFO.labels(
        version=settings.version,
        environment=settings.environment,
        service=settings.service_name,
    ).set(1)

    ctx = Context()
    app.state.ctx = ctx
    await ctx.start_background()
    log.info("control plane ready")
    try:
        yield
    finally:
        log.info("shutting down control plane")
        await ctx.shutdown()


app = FastAPI(
    title="CloudOps Sentinel",
    version=settings.version,
    description=(
        "Cloud infrastructure monitoring, reliability and cost optimisation. "
        "Runs entirely locally against a simulated multi-cloud inventory plus "
        "live Docker workloads."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Correlation id, access log, and the RED metrics for our own API."""
    correlation = request.headers.get("x-correlation-id") or new_correlation_id()
    set_correlation_id(correlation)
    started = time.perf_counter()

    # The route template, not the raw path: /inventory/{resource_id} is one
    # label value, while the raw path would create one series per resource id
    # and eventually take Prometheus down.
    template = request.scope.get("route").path if request.scope.get("route") else request.url.path

    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        status_code = 500
        log.exception("unhandled error serving request")
        response = JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "correlation_id": correlation},
        )

    duration = time.perf_counter() - started
    route = request.scope.get("route")
    path_label = route.path if route else template
    prom.HTTP_REQUESTS.labels(method=request.method, path=path_label, status=str(status_code)).inc()
    prom.HTTP_LATENCY.labels(method=request.method, path=path_label).observe(duration)

    response.headers["x-correlation-id"] = correlation
    # Baseline hardening headers. The dashboard is same-origin and uses no
    # inline event handlers, so a restrictive CSP costs nothing here.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"

    if not request.url.path.startswith(("/metrics", "/healthz", "/static")):
        log.info(
            "request",
            extra={
                "context": {
                    "event_type": "http_access",
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "client": request.client.host if request.client else "unknown",
                }
            },
        )
    return response


app.include_router(api_router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ------------------------------------------------------------------- probes
@app.get("/healthz", tags=["probes"], include_in_schema=True)
async def healthz() -> dict[str, Any]:
    """Liveness. Intentionally does no I/O - see the module docstring."""
    return {"status": "alive", "service": settings.service_name, "version": settings.version}


@app.get("/readyz", tags=["probes"])
async def readyz(request: Request) -> Response:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return JSONResponse(status_code=503, content={"status": "starting"})
    ready, checks = ctx.ready()
    payload = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "auth_enabled": bool(settings.api_token),
        "resources": len(ctx.inventory),
        "last_collection_ts": ctx.collector.last_tick.get("ts"),
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@app.get("/metrics", tags=["probes"], response_class=PlainTextResponse)
async def prometheus_metrics() -> Response:
    return Response(content=prom.render(), media_type="text/plain; version=0.0.4")


@app.get("/", include_in_schema=False)
async def dashboard() -> Response:
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"service": settings.service_name, "docs": "/docs"})
