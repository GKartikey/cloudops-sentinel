"""Incident simulation.

Two different things happen depending on the target:

  simulated resource -> the incident record is stored and the telemetry
                        generator bends that resource's metrics while it is
                        active. Nothing real is harmed.

  live container     -> the control plane ALSO calls the target's /admin/chaos
                        endpoint, so the container genuinely burns CPU, leaks
                        memory, injects 500s, sleeps before responding, or exits
                        so its restart policy restarts it.

The second case is what makes this a demonstration rather than a puppet show:
the detection path has no idea an incident was requested. It sees a container
that really is at 95% CPU, scrapes it over the network like any other target,
and has to work the problem out from the numbers.

Everything is time-boxed. An incident carries an end time and expires on its
own, so a demo cannot leave the fleet wedged.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from ..core.logging_setup import get_logger
from .inventory import Inventory
from .simulator import SCENARIOS

log = get_logger("cloudops.incidents")

# How a scenario is translated into an instruction the demo container understands.
CHAOS_TRANSLATION: dict[str, dict[str, Any]] = {
    "cpu_spike": {"mode": "cpu_burn", "intensity": 0.95},
    "memory_leak": {"mode": "memory_leak", "intensity": 0.8},
    "crash_loop": {"mode": "crash", "intensity": 1.0},
    "latency_degradation": {"mode": "latency", "intensity": 0.9},
    "error_burst": {"mode": "errors", "intensity": 0.35},
    "traffic_surge": {"mode": "load", "intensity": 1.0},
    "outage": {"mode": "outage", "intensity": 1.0},
    "disk_pressure": {"mode": "latency", "intensity": 0.5},
    "cost_spike": {"mode": "cpu_burn", "intensity": 0.85},
}

MAX_DURATION_SECONDS = 3600


class IncidentManager:
    def __init__(self, store, inventory: Inventory, timeout: float = 3.0) -> None:
        self.store = store
        self.inventory = inventory
        self.timeout = timeout

    async def start(
        self,
        scenario: str,
        resource_id: str,
        duration_seconds: int | None = None,
        magnitude: float = 1.0,
        note: str = "",
    ) -> dict[str, Any]:
        spec = SCENARIOS.get(scenario)
        if not spec:
            raise ValueError(f"unknown scenario '{scenario}'")
        resource = self.inventory.get(resource_id)
        if resource is None:
            raise ValueError(f"unknown resource '{resource_id}'")

        duration = int(duration_seconds or spec["default_duration"])
        duration = max(30, min(duration, MAX_DURATION_SECONDS))
        magnitude = max(0.1, min(float(magnitude), 3.0))
        now = time.time()

        incident = {
            "id": f"inc-{uuid.uuid4().hex[:10]}",
            "scenario": scenario,
            "resource_id": resource_id,
            "started_at": now,
            "ends_at": now + duration,
            "status": "active",
            "magnitude": magnitude,
            "params": {
                "label": spec["label"],
                "duration_seconds": duration,
                "target_kind": "live" if resource.is_live else "simulated",
                "affects": sorted(spec["effects"].keys()),
            },
            "note": note,
        }
        self.store.insert_incident(incident)

        injected = False
        if resource.is_live:
            injected = await self._inject(resource.endpoint, scenario, duration, magnitude)
            incident["params"]["chaos_injected"] = injected

        log.warning(
            "incident simulation started",
            extra={
                "context": {
                    "event_type": "incident_start",
                    "incident_id": incident["id"],
                    "scenario": scenario,
                    "resource_id": resource_id,
                    "duration_seconds": duration,
                    "target_kind": incident["params"]["target_kind"],
                    "chaos_injected": injected,
                }
            },
        )
        return incident

    async def _inject(
        self, endpoint: str, scenario: str, duration: int, magnitude: float
    ) -> bool:
        translation = CHAOS_TRANSLATION.get(scenario)
        if not translation:
            return False
        payload = {
            "mode": translation["mode"],
            "duration_seconds": duration,
            "intensity": min(translation["intensity"] * magnitude, 1.0),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{endpoint}/admin/chaos", json=payload)
                response.raise_for_status()
            return True
        except (httpx.HTTPError, OSError) as exc:
            # The simulated half of the incident still applies, so this degrades
            # rather than fails - worth a loud log line, not an exception.
            log.warning(
                "chaos injection failed; falling back to simulated effect only",
                extra={"context": {"endpoint": endpoint, "error": str(exc)}},
            )
            return False

    async def stop(self, incident_id: str) -> bool:
        incident = self.store.query_one(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        )
        cancelled = self.store.cancel_incident(incident_id)
        if cancelled and incident:
            resource = self.inventory.get(incident["resource_id"])
            if resource is not None and resource.is_live:
                await self._clear(resource.endpoint)
            log.info(
                "incident cancelled",
                extra={"context": {"incident_id": incident_id,
                                   "resource_id": incident["resource_id"]}},
            )
        return cancelled

    async def stop_all(self) -> int:
        active = self.store.active_incidents(time.time())
        count = self.store.cancel_all_incidents()
        for incident in active:
            resource = self.inventory.get(incident["resource_id"])
            if resource is not None and resource.is_live:
                await self._clear(resource.endpoint)
        log.info("all incidents cancelled", extra={"context": {"count": count}})
        return count

    async def _clear(self, endpoint: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(f"{endpoint}/admin/chaos", json={"mode": "none"})
        except (httpx.HTTPError, OSError):
            log.warning("failed to clear chaos", extra={"context": {"endpoint": endpoint}})
