"""Demo workload service.

One image, three deployments. SERVICE_ROLE selects the personality (checkout
api, inventory api, report worker), which changes the endpoints it exposes and
the shape of its self-generated traffic - so the fleet has variety without three
codebases to maintain. This is the same pattern as a single application image
promoted across environments with different configuration.

What makes it useful to the platform above it:
  * /metrics    real CPU and memory from the container's own cgroup, plus
                request counters and a rolling p95 latency
  * /logs       the last N structured log records, pulled by the collector
  * /healthz    liveness  - the process is running
  * /readyz     readiness - the process can actually serve
  * /admin/chaos the incident injection surface

It also drives its own load in the background, so the containers have real
traffic to measure without an external load generator in the compose file.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .runtime import ChaosController, LogBuffer, ResourceProbe

SERVICE_NAME = os.getenv("SERVICE_NAME", "demo-service")
SERVICE_ROLE = os.getenv("SERVICE_ROLE", "api")
PORT = int(os.getenv("PORT", "8080"))
BASE_RPS = float(os.getenv("BASE_RPS", "6"))
SELF_TRAFFIC = os.getenv("SELF_TRAFFIC", "true").lower() in ("1", "true", "yes")
BASE_LATENCY_MS = float(os.getenv("BASE_LATENCY_MS", "35"))
STATE_PATH = Path(os.getenv("CHAOS_STATE_PATH", "/tmp/cloudops-chaos-state.json"))
# How long a crash-mode process stays up before it dies. Must exceed the
# collector's scrape interval, or the restart is never observed at all.
CRASH_GRACE_SECONDS = float(os.getenv("CRASH_GRACE_SECONDS", "14"))

PROCESS_START_TIME = time.time()

logs = LogBuffer(capacity=int(os.getenv("LOG_BUFFER_SIZE", "500")))
probe = ResourceProbe()
chaos = ChaosController(STATE_PATH, logs, SERVICE_NAME)


class Counters:
    """Monotonic counters plus a bounded latency window for the p95."""

    def __init__(self) -> None:
        self.requests_total = 0
        self.requests_failed = 0
        self.latencies: list[float] = []

    def observe(self, seconds: float, failed: bool) -> None:
        self.requests_total += 1
        if failed:
            self.requests_failed += 1
        self.latencies.append(seconds)
        # Keep the window small and recent: a p95 over all history stops moving.
        if len(self.latencies) > 400:
            del self.latencies[:-400]

    def p95(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        return ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]


counters = Counters()


class ChaosRequest(BaseModel):
    mode: str = Field(default="none")
    duration_seconds: int = Field(default=120, ge=5, le=3600)
    intensity: float = Field(default=0.8, ge=0.0, le=1.0)


# --------------------------------------------------------------- self traffic
async def traffic_generator(stop: asyncio.Event) -> None:
    """Hit our own endpoints so the container has genuine work to measure."""
    paths = {
        "api": ["/api/checkout", "/api/checkout", "/api/orders"],
        "inventory": ["/api/items", "/api/items", "/api/stock"],
        "worker": ["/api/report"],
    }.get(SERVICE_ROLE, ["/api/checkout"])

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=10.0) as client:
        while not stop.is_set():
            # Poisson-ish spacing rather than a fixed tick, so the latency and
            # throughput series look like traffic instead of a metronome.
            delay = random.expovariate(max(BASE_RPS, 0.1))
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(delay, 2.0))
                break
            except asyncio.TimeoutError:
                pass
            try:
                await client.get(random.choice(paths))
            except (httpx.HTTPError, OSError):
                # Self-traffic failing during an induced outage is expected.
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    chaos.record_start()
    logs.add(
        "INFO", "service starting", SERVICE_NAME,
        role=SERVICE_ROLE, port=PORT, restart_count=chaos.restart_count,
        **probe.info(),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(traffic_generator(stop)) if SELF_TRAFFIC else None
    try:
        yield
    finally:
        stop.set()
        if task:
            task.cancel()
        chaos.clear_effects()
        logs.add("INFO", "service stopping", SERVICE_NAME)


app = FastAPI(title=f"CloudOps demo service ({SERVICE_ROLE})", lifespan=lifespan)


@app.middleware("http")
async def instrumentation(request: Request, call_next):
    """Where induced failure is actually applied to real traffic."""
    if request.url.path in ("/metrics", "/logs", "/healthz", "/readyz", "/admin/chaos"):
        return await call_next(request)

    started = time.perf_counter()

    if chaos.should_crash() and (time.time() - PROCESS_START_TIME) > CRASH_GRACE_SECONDS:
        # Serve normally for a grace period, then die on the next request.
        #
        # The delay is not cosmetic. A process that exits on its very first
        # request is never alive long enough to be scraped, so the restart is
        # invisible to any pull-based monitoring - the counter stays at zero
        # while the container thrashes. Real crash loops almost always survive
        # long enough to be observed once, and this reproduces that. It is also
        # what lets the restart count actually climb across the loop.
        chaos.crash()

    extra = chaos.extra_latency_seconds()
    if extra:
        await asyncio.sleep(extra)
    chaos.leak_memory()

    if chaos.is_outage():
        counters.observe(time.perf_counter() - started, failed=True)
        return JSONResponse(status_code=503, content={"detail": "service unavailable"})

    if chaos.should_fail(random.random()):
        duration = time.perf_counter() - started
        counters.observe(duration, failed=True)
        logs.add(
            "ERROR", f"request failed: {request.url.path}", SERVICE_NAME,
            path=request.url.path, status=500, duration_ms=round(duration * 1000, 2),
        )
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    response = await call_next(request)
    duration = time.perf_counter() - started
    counters.observe(duration, failed=response.status_code >= 500)
    if response.status_code >= 500:
        logs.add("ERROR", f"request failed: {request.url.path}", SERVICE_NAME,
                 path=request.url.path, status=response.status_code)
    elif duration > 1.0:
        logs.add("WARN", f"slow request: {request.url.path}", SERVICE_NAME,
                 path=request.url.path, duration_ms=round(duration * 1000, 2))
    return response


# ------------------------------------------------------------------ business
async def _work(units: int = 1) -> None:
    """Stand-in for real work: a little CPU and a little waiting."""
    await asyncio.sleep(BASE_LATENCY_MS / 1000.0 * random.uniform(0.6, 1.6) * units)
    total = 0
    for i in range(3000 * units):
        total += i * i


@app.get("/api/checkout")
async def checkout() -> dict[str, Any]:
    await _work()
    return {"order_id": f"ord-{random.randint(100000, 999999)}", "status": "confirmed"}


@app.get("/api/orders")
async def orders() -> dict[str, Any]:
    await _work(2)
    return {"orders": [{"id": f"ord-{n}", "total": round(random.uniform(5, 400), 2)}
                       for n in range(5)]}


@app.get("/api/items")
async def items() -> dict[str, Any]:
    await _work()
    return {"items": [{"sku": f"SKU-{n:04d}", "qty": random.randint(0, 200)}
                      for n in range(8)]}


@app.get("/api/stock")
async def stock() -> dict[str, Any]:
    await _work()
    return {"warehouses": 4, "reserved": random.randint(10, 90)}


@app.get("/api/report")
async def report() -> dict[str, Any]:
    await _work(3)
    return {"report_id": f"rpt-{random.randint(1000, 9999)}", "rows": random.randint(500, 5000)}


# -------------------------------------------------------------------- probes
@app.get("/healthz")
async def healthz() -> Response:
    """Liveness: is the process alive and not wedged?

    Notice this stays 200 during an induced outage. Liveness answering 503 would
    make Kubernetes kill and restart the pod, which is the wrong remedy for a
    dependency being down - readiness is what should pull it out of the load
    balancer. Conflating the two turns a partial outage into a restart storm.
    """
    return JSONResponse({"status": "alive", "service": SERVICE_NAME, "role": SERVICE_ROLE})


@app.get("/readyz")
async def readyz() -> Response:
    if chaos.is_outage():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "induced outage", "service": SERVICE_NAME},
        )
    return JSONResponse({"status": "ready", "service": SERVICE_NAME})


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> Response:
    if chaos.is_outage():
        # A target in outage must look down to its scraper, not silently healthy.
        return PlainTextResponse("service unavailable", status_code=503)

    status = chaos.status()
    body = "\n".join([
        "# HELP demo_cpu_utilization_percent CPU used as a percentage of the container CPU limit",
        "# TYPE demo_cpu_utilization_percent gauge",
        f"demo_cpu_utilization_percent {probe.cpu_percent()}",
        "# HELP demo_memory_utilization_percent Memory used as a percentage of the container memory limit",
        "# TYPE demo_memory_utilization_percent gauge",
        f"demo_memory_utilization_percent {probe.memory_percent()}",
        "# HELP demo_memory_used_bytes Resident memory of the container",
        "# TYPE demo_memory_used_bytes gauge",
        f"demo_memory_used_bytes {probe.memory_bytes()}",
        "# HELP demo_requests_total Requests served since process start",
        "# TYPE demo_requests_total counter",
        f"demo_requests_total {counters.requests_total}",
        "# HELP demo_requests_failed_total Requests that returned 5xx since process start",
        "# TYPE demo_requests_failed_total counter",
        f"demo_requests_failed_total {counters.requests_failed}",
        "# HELP demo_request_latency_p95_seconds 95th percentile latency over the recent window",
        "# TYPE demo_request_latency_p95_seconds gauge",
        f"demo_request_latency_p95_seconds {round(counters.p95(), 5)}",
        "# HELP demo_process_start_time_seconds Unix start time of this process",
        "# TYPE demo_process_start_time_seconds gauge",
        f"demo_process_start_time_seconds {PROCESS_START_TIME}",
        "# HELP demo_reported_restart_count Restarts this container has observed",
        "# TYPE demo_reported_restart_count counter",
        f"demo_reported_restart_count {status['restart_count']}",
        "# HELP demo_chaos_active 1 when a chaos mode is engaged",
        "# TYPE demo_chaos_active gauge",
        f"demo_chaos_active {1 if status['mode'] != 'none' else 0}",
        "",
    ])
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@app.get("/logs")
async def get_logs(
    since: float = Query(default=0.0),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    return logs.since(since, limit)


@app.post("/admin/chaos")
async def set_chaos(body: ChaosRequest) -> dict[str, Any]:
    """Incident injection.

    In a real system this endpoint would not exist, or would sit behind an
    authenticated admin route on a separate port that is not exposed outside the
    cluster. It is here because inducing genuine failure is the entire point of
    the demonstration - and it is documented as a deliberate exception in
    docs/SECURITY.md rather than left as an accident.
    """
    try:
        return chaos.set(body.mode, body.duration_seconds, body.intensity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/admin/chaos")
async def get_chaos() -> dict[str, Any]:
    return chaos.status()


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "role": SERVICE_ROLE,
        "uptime_seconds": round(time.time() - PROCESS_START_TIME, 1),
        "requests_total": counters.requests_total,
        "chaos": chaos.status(),
        "limits": probe.info(),
    }
