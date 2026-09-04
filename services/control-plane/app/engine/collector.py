"""The collection loop - the heart of the platform.

Every tick, in order:

  1. expire finished incidents, load the active ones
  2. generate samples for simulated resources
  3. scrape live containers over HTTP (metrics + their structured logs)
  4. persist samples and logs
  5. score health, detect anomalies, evaluate alert rules
  6. republish everything as Prometheus gauges
  7. prune anything past the retention window

Design notes worth defending:

* **Pull, not push.** The control plane scrapes targets; targets do not push to
  it. That is the Prometheus model, and it means a target that dies is detected
  by its absence rather than by its silence being indistinguishable from health.

* **No Docker socket.** Restart counts are derived by watching each target's
  reported process start time change. Mounting /var/run/docker.sock into a
  monitoring container hands it root on the host - the restart count is not
  worth that trade. See docs/SECURITY.md.

* **A failed scrape is data.** It records availability=0 rather than skipping
  the resource, so "down" is a value the alert rules can match on.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from .. import metrics as prom
from ..core.logging_setup import get_logger
from .anomaly import AnomalyDetector
from .health import evaluate_fleet
from .inventory import Inventory, Resource
from .simulator import TelemetrySimulator

log = get_logger("cloudops.collector")

# Prometheus metric names exposed by the demo services, mapped to our vocabulary.
LIVE_GAUGE_MAP = {
    "demo_cpu_utilization_percent": "cpu_utilization",
    "demo_memory_utilization_percent": "memory_utilization",
    "demo_request_latency_p95_seconds": "latency_p95_ms",   # scaled below
}


def parse_prometheus_text(body: str) -> dict[str, float]:
    """Minimal Prometheus text-format parser.

    Only handles unlabelled samples, which is all the demo services emit. Using
    the real client library here would mean pulling a parser dependency to read
    eight numbers; this is 15 lines and has no failure mode we do not control.
    """
    out: dict[str, float] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, raw = parts[0], parts[1]
        if "{" in name:
            name = name.split("{", 1)[0]
        try:
            out[name] = float(raw)
        except ValueError:
            continue
    return out


class Collector:
    def __init__(
        self,
        store,
        inventory: Inventory,
        simulator: TelemetrySimulator,
        detector: AnomalyDetector,
        alert_engine,
        cost_model,
        settings,
    ) -> None:
        self.store = store
        self.inventory = inventory
        self.simulator = simulator
        self.detector = detector
        self.alerts = alert_engine
        self.cost = cost_model
        self.settings = settings

        # Counter state for live targets: counters are monotonic, so a rate needs
        # the previous reading. Keyed by resource id.
        self._counter_state: dict[str, dict[str, float]] = {}
        self._start_times: dict[str, float] = {}
        self._restart_counts: dict[str, int] = {}
        self._log_cursor: dict[str, float] = {}

        self.last_tick: dict[str, Any] = {}
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self.settings.scrape_timeout_seconds,
            headers={"User-Agent": "cloudops-sentinel/collector"},
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- backfill
    def backfill(self, hours: int) -> int:
        """Seed synthetic history so the UI is useful the second it loads.

        Only simulated resources are backfilled - we cannot invent a past for a
        container that started thirty seconds ago, and pretending otherwise
        would put fiction in the same table as measurements.
        """
        if hours <= 0:
            return 0
        now = time.time()
        step = max(self.settings.collect_interval_seconds, 30)
        start = now - hours * 3600
        rows: list[tuple[float, str, str, float]] = []
        logs: list[tuple[float, str, str, str, str, dict]] = []

        ts = start
        while ts < now:
            for resource in self.inventory.simulated:
                for metric, value in self.simulator.collect(resource, ts).items():
                    rows.append((ts, resource.id, metric, value))
            ts += step

        # A sparse, believable log history rather than one line per sample.
        ts = start
        while ts < now:
            for resource in self.inventory.simulated:
                entry = self._synthetic_log(resource, ts, self.simulator.collect(resource, ts))
                if entry:
                    logs.append(entry)
            ts += step * 6

        written = self.store.insert_samples(rows)
        self.store.insert_logs(logs)
        self.store.set_meta("backfilled_at", str(now))
        log.info(
            "backfill complete",
            extra={"context": {"samples": written, "logs": len(logs), "hours": hours}},
        )
        return written

    # ----------------------------------------------------------------- tick
    async def tick(self) -> dict[str, Any]:
        started = time.perf_counter()
        now = time.time()
        outcome = "success"
        try:
            self.store.expire_incidents(now)
            incidents = self.store.active_incidents(now)

            rows: list[tuple[float, str, str, float]] = []
            logs: list[tuple[float, str, str, str, str, dict]] = []

            # --- simulated fleet ---------------------------------------
            for resource in self.inventory.simulated:
                values = self.simulator.collect(resource, now, incidents)
                for metric, value in values.items():
                    rows.append((now, resource.id, metric, value))
                entry = self._synthetic_log(resource, now, values, incidents)
                if entry:
                    logs.append(entry)

            # --- live containers ---------------------------------------
            live = self.inventory.live
            if live and self._client:
                results = await asyncio.gather(
                    *(self._scrape(r, now, incidents) for r in live),
                    return_exceptions=True,
                )
                for resource, result in zip(live, results):
                    if isinstance(result, BaseException):
                        log.warning(
                            "scrape task failed",
                            extra={"context": {"resource_id": resource.id, "error": str(result)}},
                        )
                        prom.SCRAPE_FAILURES.labels(resource=resource.id).inc()
                        rows.append((now, resource.id, "availability", 0.0))
                        continue
                    sample_rows, sample_logs = result
                    rows.extend(sample_rows)
                    logs.extend(sample_logs)

            written = self.store.insert_samples(rows)
            prom.SAMPLES_WRITTEN.inc(written)
            if logs:
                self.store.insert_logs(logs)
                for entry in logs:
                    prom.LOGS_INGESTED.labels(level=entry[3]).inc()

            # --- analysis ----------------------------------------------
            latest = self.store.latest_samples()
            # One read of the shared window, in both shapes the analysers want.
            samples = self.store.window_samples(now - self.settings.anomaly_window * 60)
            window = {
                rid: {m: [v for _, v in series] for m, series in metrics.items()}
                for rid, metrics in samples.items()
            }

            fleet_health = evaluate_fleet(self.inventory.resources, latest, now)
            anomalies = self._detect(latest, window, now)
            alert_result = self.alerts.evaluate(
                latest, {r.id: r.name for r in self.inventory}, now, samples
            )
            for alert in alert_result["fired"]:
                prom.ALERTS_FIRED.labels(
                    rule=alert["rule"], severity=alert["severity"]
                ).inc()

            self._publish(latest, fleet_health, incidents, now)

            # --- retention ---------------------------------------------
            if int(now) % 60 < self.settings.collect_interval_seconds:
                self.store.prune(now - self.settings.retention_hours * 3600)

            prom.LAST_COLLECTION_TS.set(now)
            self.last_tick = {
                "ts": now,
                "samples_written": written,
                "logs_written": len(logs),
                "anomalies": len(anomalies),
                "alerts_fired": len(alert_result["fired"]),
                "alerts_resolved": len(alert_result["resolved"]),
                "active_incidents": len(incidents),
                "fleet_status": fleet_health["status"],
                "fleet_score": fleet_health["score"],
            }
            log.info("collection tick", extra={"context": self.last_tick})
            return self.last_tick

        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            outcome = "error"
            log.error(
                "collection tick failed",
                exc_info=True,
                extra={"context": {"error": str(exc)}},
            )
            return {"ts": now, "error": str(exc)}
        finally:
            prom.COLLECTION_RUNS.labels(outcome=outcome).inc()
            prom.COLLECTION_DURATION.observe(time.perf_counter() - started)

    # --------------------------------------------------------------- scrape
    async def _scrape(
        self, resource: Resource, now: float, incidents: list[dict]
    ) -> tuple[list[tuple], list[tuple]]:
        rows: list[tuple] = []
        logs: list[tuple] = []
        assert self._client is not None

        try:
            response = await self._client.get(f"{resource.endpoint}/metrics")
            response.raise_for_status()
            raw = parse_prometheus_text(response.text)
        except (httpx.HTTPError, OSError) as exc:
            # Down is a measurement, not a gap. Record it so alerts can fire.
            prom.SCRAPE_FAILURES.labels(resource=resource.id).inc()
            log.warning(
                "live target unreachable",
                extra={"context": {"resource_id": resource.id,
                                   "endpoint": resource.endpoint, "error": str(exc)}},
            )
            rows.append((now, resource.id, "availability", 0.0))
            rows.append((now, resource.id, "error_rate", 1.0))
            rows.append(
                (now, resource.id, "restart_count", float(self._restart_counts.get(resource.id, 0)))
            )
            return rows, logs

        rows.append((now, resource.id, "availability", 1.0))

        for source, target in LIVE_GAUGE_MAP.items():
            if source in raw:
                value = raw[source]
                if target == "latency_p95_ms":
                    value *= 1000.0
                rows.append((now, resource.id, target, value))

        # Counters -> rates. First sighting establishes the baseline only.
        previous = self._counter_state.get(resource.id)
        total = raw.get("demo_requests_total")
        failed = raw.get("demo_requests_failed_total")
        if total is not None and failed is not None:
            if previous and "ts" in previous:
                dt = max(now - previous["ts"], 1e-6)
                d_total = total - previous.get("total", total)
                d_failed = failed - previous.get("failed", failed)
                # A counter going backwards means the process restarted; the
                # correct reading is the new absolute value, not a negative rate.
                if d_total < 0 or d_failed < 0:
                    d_total, d_failed = max(total, 0.0), max(failed, 0.0)
                rows.append((now, resource.id, "requests_per_second", max(d_total / dt, 0.0)))
                rows.append(
                    (now, resource.id, "error_rate",
                     (d_failed / d_total) if d_total > 0 else 0.0)
                )
            self._counter_state[resource.id] = {"ts": now, "total": total, "failed": failed}

        # Restart detection without a Docker socket: the process start time
        # changing means a new process, which means a restart.
        start_time = raw.get("demo_process_start_time_seconds")
        if start_time is not None:
            known = self._start_times.get(resource.id)
            if known is not None and abs(start_time - known) > 1.0:
                self._restart_counts[resource.id] = self._restart_counts.get(resource.id, 0) + 1
                log.warning(
                    "restart detected on live target",
                    extra={"context": {"resource_id": resource.id,
                                       "restart_count": self._restart_counts[resource.id]}},
                )
            self._start_times[resource.id] = start_time
        rows.append(
            (now, resource.id, "restart_count", float(self._restart_counts.get(resource.id, 0)))
        )

        if "demo_reported_restart_count" in raw:
            # The service also reports restarts it induced on itself (chaos mode),
            # which covers an in-process restart the start time would not show.
            induced = raw["demo_reported_restart_count"]
            merged = max(float(self._restart_counts.get(resource.id, 0)), induced)
            rows[-1] = (now, resource.id, "restart_count", merged)

        logs.extend(await self._pull_logs(resource, now))
        return rows, logs

    async def _pull_logs(self, resource: Resource, now: float) -> list[tuple]:
        """Fetch the structured log buffer each demo service keeps.

        Real deployments replace this with the node log agent (Fluent Bit ->
        Loki / CloudWatch / Log Analytics). The interface - JSON lines with a
        level, message and context - is identical, which is the point.
        """
        assert self._client is not None
        since = self._log_cursor.get(resource.id, now - 300)
        try:
            response = await self._client.get(
                f"{resource.endpoint}/logs", params={"since": since, "limit": 200}
            )
            response.raise_for_status()
            entries = response.json()
        except (httpx.HTTPError, OSError, json.JSONDecodeError, ValueError):
            return []

        out: list[tuple] = []
        newest = since
        for entry in entries if isinstance(entries, list) else []:
            ts = float(entry.get("ts", now))
            newest = max(newest, ts)
            out.append((
                ts,
                resource.id,
                entry.get("service", resource.name),
                str(entry.get("level", "INFO")).upper(),
                str(entry.get("message", "")),
                entry.get("context") or {},
            ))
        self._log_cursor[resource.id] = newest
        return out

    # ------------------------------------------------------ synthetic logs
    def _synthetic_log(
        self,
        resource: Resource,
        ts: float,
        values: dict[str, float],
        incidents: list[dict] | None = None,
    ) -> tuple | None:
        """Emit a log line for a simulated resource when something is notable.

        Simulated resources still have to produce a log stream, otherwise the
        log-search half of the product has nothing to search on half the fleet.
        The lines are derived from the same values the metrics came from, so the
        two views always agree.
        """
        cpu = values.get("cpu_utilization", 0.0)
        mem = values.get("memory_utilization", 0.0)
        err = values.get("error_rate", 0.0)
        latency = values.get("latency_p95_ms", 0.0)
        available = values.get("availability", 1.0)
        active = [i for i in (incidents or ()) if i.get("resource_id") == resource.id]
        context = {
            "resource_id": resource.id,
            "region": resource.region,
            "environment": resource.environment,
            "cpu": round(cpu, 1),
            "memory": round(mem, 1),
        }
        if active:
            context["incident"] = active[0]["scenario"]

        if available < 0.5:
            return (ts, resource.id, resource.name, "ERROR",
                    "health probe failed: target not responding", context)
        if err > 0.15:
            return (ts, resource.id, resource.name, "ERROR",
                    f"upstream returned 5xx for {err:.1%} of requests", context)
        if mem > 92:
            return (ts, resource.id, resource.name, "ERROR",
                    f"memory pressure critical at {mem:.1f}%, GC thrashing", context)
        if cpu > 92:
            return (ts, resource.id, resource.name, "WARN",
                    f"cpu saturated at {cpu:.1f}%, request queue growing", context)
        if err > 0.03:
            return (ts, resource.id, resource.name, "WARN",
                    f"elevated error rate {err:.2%}", context)
        if latency > 800:
            return (ts, resource.id, resource.name, "WARN",
                    f"p95 latency {latency:.0f}ms exceeds objective", context)
        # Heartbeat, but only occasionally - one INFO line per resource per
        # scrape would be 90% of the log volume and 0% of the value.
        if int(ts) % 300 < self.settings.collect_interval_seconds:
            return (ts, resource.id, resource.name, "INFO",
                    "periodic health summary", context)
        return None

    # ------------------------------------------------------------ analysis
    def _detect(self, latest: dict, window: dict, now: float) -> list[dict]:
        found = self.detector.scan(latest, window, now)
        recorded = []
        for anomaly in found:
            # Dedupe: one sustained incident is one anomaly, not one per tick.
            if self.store.anomaly_exists_recently(
                anomaly["resource_id"], anomaly["metric"],
                now - self.detector.cooldown_seconds,
            ):
                continue
            self.store.insert_anomaly(anomaly)
            prom.ANOMALIES_DETECTED.labels(severity=anomaly["severity"]).inc()
            recorded.append(anomaly)
            log.warning(
                "anomaly detected",
                extra={"context": {"event_type": "anomaly", **anomaly}},
            )
        return recorded

    # ------------------------------------------------------------- publish
    def _publish(
        self, latest: dict, fleet_health: dict, incidents: list[dict], now: float
    ) -> None:
        utilization = {
            rid: {m: v for m, (v, _) in metrics.items()}
            for rid, metrics in latest.items()
        }
        fleet_cost = self.cost.fleet(self.inventory.resources, utilization)
        cost_by_resource = {row["resource_id"]: row for row in fleet_cost["resources"]}
        health_by_resource = {r["resource_id"]: r for r in fleet_health["resources"]}

        for resource in self.inventory:
            labels = prom.labels_for(resource)
            for metric, (value, _) in latest.get(resource.id, {}).items():
                gauge = prom.METRIC_GAUGES.get(metric)
                if gauge is not None:
                    gauge.labels(*labels).set(value)
            row = cost_by_resource.get(resource.id)
            if row:
                prom.RESOURCE_COST.labels(*labels).set(row["monthly_cost"])
                prom.RESOURCE_WASTE.labels(*labels).set(row["waste_monthly"])
            health = health_by_resource.get(resource.id)
            if health:
                prom.RESOURCE_HEALTH_SCORE.labels(*labels).set(health["score"])

        prom.FLEET_COST.set(fleet_cost["total_monthly"])
        prom.FLEET_WASTE.set(fleet_cost["waste_monthly"])
        prom.FLEET_HEALTH_SCORE.set(fleet_health["score"])
        for status, count in fleet_health["counts"].items():
            prom.FLEET_RESOURCES.labels(status=status).set(count)
        prom.ACTIVE_INCIDENTS.set(len(incidents))

        summary = self.alerts.summary()
        for severity in ("critical", "warning", "info"):
            prom.ACTIVE_ALERTS.labels(severity=severity).set(
                summary["by_severity"].get(severity, 0)
            )

    # ----------------------------------------------------------------- loop
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        interval = self.settings.collect_interval_seconds
        log.info("collector started", extra={"context": {"interval_seconds": interval}})
        while not stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
        log.info("collector stopped")
