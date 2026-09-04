"""Synthetic telemetry generator for the simulated half of the fleet.

Two properties matter and drive the whole design:

1. **Deterministic.** value(resource, metric, t) is a pure function of its
   arguments plus the seed. That is what makes the six-hour backfill on first
   boot continuous with the live samples collected a moment later - no seam, no
   re-randomising on restart, and a reproducible demo every single time.

2. **Not white noise.** Real infrastructure wanders. We use value noise
   (hash-anchored samples interpolated with smoothstep, summed over two
   octaves) plus a diurnal sine, so the series has the autocorrelation that
   makes a z-score detector meaningful. Pure random draws would make every
   anomaly detector look brilliant for the wrong reason.

Incidents are applied as modifiers on top of the baseline, which is why the
detector genuinely has to notice them rather than being told.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Any

from .inventory import Resource

# Seconds between value-noise anchors. Lower = twitchier series.
_ANCHOR_SECONDS = 90.0
_DAY_SECONDS = 86400.0


def _unit_hash(*parts: Any) -> float:
    """Stable float in [0, 1) derived from the arguments. The determinism
    backbone: no RNG state, so any sample can be recomputed at any time."""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return struct.unpack("<Q", digest)[0] / float(1 << 64)


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _value_noise(seed: int, key: str, t: float, period: float) -> float:
    """Smooth pseudo-random signal in [-1, 1] with the given anchor period."""
    x = t / period
    i = math.floor(x)
    frac = x - i
    a = _unit_hash(seed, key, i) * 2.0 - 1.0
    b = _unit_hash(seed, key, i + 1) * 2.0 - 1.0
    return a + (b - a) * _smoothstep(frac)


def _wander(seed: int, key: str, t: float) -> float:
    """Two octaves of value noise, roughly in [-1, 1]."""
    coarse = _value_noise(seed, key + ":c", t, _ANCHOR_SECONDS * 8)
    fine = _value_noise(seed, key + ":f", t, _ANCHOR_SECONDS)
    return 0.7 * coarse + 0.3 * fine


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Incident scenarios. Each entry describes how a metric is bent while the
# incident is active. `mul` scales the baseline, `add` shifts it, `set` pins it.
# `ramp` means the effect grows from 0 to full over the incident's lifetime,
# which is what makes a memory leak look like a leak rather than a step change.
SCENARIOS: dict[str, dict[str, Any]] = {
    "cpu_spike": {
        "label": "CPU saturation",
        "description": "A runaway process pins CPU near 100% while latency climbs and throughput sags.",
        "default_duration": 300,
        "effects": {
            "cpu_utilization": {"set": 96.0, "jitter": 3.0},
            "latency_p95_ms": {"mul": 3.2},
            "requests_per_second": {"mul": 0.65},
            "error_rate": {"add": 0.015},
        },
    },
    "memory_leak": {
        "label": "Memory leak",
        "description": "Memory climbs steadily toward the limit; latency degrades as GC pressure rises.",
        "default_duration": 600,
        "effects": {
            "memory_utilization": {"set": 97.0, "ramp": True, "jitter": 1.5},
            "latency_p95_ms": {"mul": 1.9, "ramp": True},
            "cpu_utilization": {"mul": 1.35, "ramp": True},
        },
    },
    "crash_loop": {
        "label": "Crash loop (CrashLoopBackOff)",
        "description": "The container restarts repeatedly; availability flaps and errors spike.",
        "default_duration": 420,
        "effects": {
            "restart_count": {"add_per_minute": 2.5},
            "availability": {"flap": 0.45},
            "error_rate": {"add": 0.22},
            "requests_per_second": {"mul": 0.4},
        },
    },
    "latency_degradation": {
        "label": "Latency degradation",
        "description": "A slow downstream dependency drags p95 latency past the SLO with few outright errors.",
        "default_duration": 480,
        "effects": {
            "latency_p95_ms": {"mul": 5.5, "ramp": True},
            "error_rate": {"add": 0.008},
            "cpu_utilization": {"mul": 1.15},
        },
    },
    "error_burst": {
        "label": "Error burst",
        "description": "A bad deploy pushes the 5xx rate far above the SLO while traffic continues.",
        "default_duration": 300,
        "effects": {
            "error_rate": {"set": 0.31, "jitter": 0.05},
            "latency_p95_ms": {"mul": 1.6},
        },
    },
    "traffic_surge": {
        "label": "Traffic surge",
        "description": "Throughput triples; CPU and latency follow it up. Load, not a fault.",
        "default_duration": 420,
        "effects": {
            "requests_per_second": {"mul": 3.4},
            "cpu_utilization": {"mul": 1.85},
            "latency_p95_ms": {"mul": 1.7},
            "memory_utilization": {"mul": 1.2},
        },
    },
    "outage": {
        "label": "Total outage",
        "description": "The target stops answering health probes entirely.",
        "default_duration": 240,
        "effects": {
            "availability": {"set": 0.0},
            "error_rate": {"set": 1.0},
            "requests_per_second": {"set": 0.0},
            "latency_p95_ms": {"mul": 0.0},
        },
    },
    "disk_pressure": {
        "label": "Disk pressure",
        "description": "The volume fills toward 100%; writes slow and errors creep up.",
        "default_duration": 600,
        "effects": {
            "disk_utilization": {"set": 96.0, "ramp": True, "jitter": 1.0},
            "latency_p95_ms": {"mul": 1.8, "ramp": True},
            "error_rate": {"add": 0.01, "ramp": True},
        },
    },
    "cost_spike": {
        "label": "Runaway cost",
        "description": "Autoscaling floors the fleet at max capacity: full utilisation, full price, no traffic to justify it.",
        "default_duration": 900,
        "effects": {
            "cpu_utilization": {"set": 88.0, "jitter": 4.0},
            "memory_utilization": {"set": 84.0, "jitter": 4.0},
            "requests_per_second": {"mul": 0.9},
        },
    },
}


class TelemetrySimulator:
    """Generates a metric value for a simulated resource at an arbitrary time."""

    def __init__(self, seed: int) -> None:
        self.seed = seed

    # ------------------------------------------------------------- baseline
    def baseline(self, resource: Resource, metric: str, ts: float) -> float:
        profile = resource.profile or {}
        kind = profile.get("kind", "steady")
        key = f"{resource.id}:{metric}"

        if metric == "availability":
            return 1.0
        if metric == "restart_count":
            # Healthy workloads restart on deploys: roughly once every few hours.
            return float(int(ts / 9000.0) % 3) if kind != "dead" else 0.0

        base = self._metric_base(resource, metric, profile)
        if base <= 0.0:
            return 0.0

        wander = _wander(self.seed, key, ts)
        # Idle and dead resources are steadier than busy ones; scale the noise
        # with the signal so a 6%-CPU box does not swing +/-15 points.
        amplitude = 0.14 if kind in ("idle", "dead", "storage") else 0.22
        value = base * (1.0 + amplitude * wander)

        if kind == "diurnal":
            # Business-hours shape: peak mid-afternoon UTC, trough overnight.
            phase = ((ts % _DAY_SECONDS) / _DAY_SECONDS) * 2.0 * math.pi
            value *= 1.0 + 0.35 * math.sin(phase - math.pi / 2.0)
        elif kind == "bursty":
            burst = _value_noise(self.seed, key + ":burst", ts, _ANCHOR_SECONDS * 3)
            if burst > 0.55:
                value *= 1.0 + 1.6 * (burst - 0.55)

        return self._clamp_metric(metric, value)

    def _metric_base(self, resource: Resource, metric: str, profile: dict) -> float:
        if metric == "cpu_utilization":
            return float(profile.get("cpu", 0.0))
        if metric == "memory_utilization":
            return float(profile.get("mem", 0.0))
        if metric == "latency_p95_ms":
            return float(profile.get("latency_ms", 0.0))
        if metric == "error_rate":
            return float(profile.get("error_rate", 0.0))
        if metric == "requests_per_second":
            return float(profile.get("rps", 0.0))
        if metric == "disk_utilization":
            # Disk fills slowly and monotonically rather than wandering: model it
            # as a slow saw derived from the resource id so each box differs.
            floor = 30.0 + 40.0 * _unit_hash(self.seed, resource.id, "disk")
            return floor
        return 0.0

    @staticmethod
    def _clamp_metric(metric: str, value: float) -> float:
        if metric in ("cpu_utilization", "memory_utilization", "disk_utilization"):
            return _clamp(value, 0.0, 100.0)
        if metric == "error_rate":
            return _clamp(value, 0.0, 1.0)
        if metric == "availability":
            return _clamp(value, 0.0, 1.0)
        return max(0.0, value)

    # ------------------------------------------------------------ incidents
    def sample(
        self,
        resource: Resource,
        metric: str,
        ts: float,
        incidents: list[dict] | None = None,
    ) -> float:
        value = self.baseline(resource, metric, ts)
        for incident in incidents or ():
            if incident.get("resource_id") not in (resource.id, "*"):
                continue
            value = self.apply_incident(value, metric, ts, incident)
        return self._clamp_metric(metric, value)

    def apply_incident(self, value: float, metric: str, ts: float, incident: dict) -> float:
        scenario = SCENARIOS.get(incident.get("scenario", ""))
        if not scenario:
            return value
        effect = scenario["effects"].get(metric)
        if not effect:
            return value

        started = float(incident.get("started_at", ts))
        ends = float(incident.get("ends_at", ts))
        span = max(ends - started, 1.0)
        progress = _clamp((ts - started) / span, 0.0, 1.0)
        magnitude = float(incident.get("magnitude", 1.0))
        # A ramped effect eases in; everything else is at full strength at once.
        strength = magnitude * (_smoothstep(progress) if effect.get("ramp") else 1.0)

        result = value
        if "set" in effect:
            result = value + (float(effect["set"]) - value) * strength
        if "mul" in effect:
            result = result * (1.0 + (float(effect["mul"]) - 1.0) * strength)
        if "add" in effect:
            result = result + float(effect["add"]) * strength
        if "add_per_minute" in effect:
            minutes = max(ts - started, 0.0) / 60.0
            result = result + float(effect["add_per_minute"]) * minutes * magnitude
        if "flap" in effect:
            # Deterministic on/off flapping: the target is up some of the time.
            flip = _unit_hash(self.seed, incident.get("id", ""), metric, int(ts // 20))
            result = 0.0 if flip < float(effect["flap"]) else result
        if "jitter" in effect:
            noise = _wander(self.seed, f"{incident.get('id','')}:{metric}", ts)
            result += float(effect["jitter"]) * noise * strength

        return result

    def collect(
        self, resource: Resource, ts: float, incidents: list[dict] | None = None
    ) -> dict[str, float]:
        """Every metric that applies to this resource, at time `ts`."""
        return {m: self.sample(resource, m, ts, incidents) for m in resource.metrics}


def scenario_catalog() -> list[dict]:
    """Scenario list for the API and the dashboard's incident panel."""
    return [
        {
            "id": key,
            "label": spec["label"],
            "description": spec["description"],
            "default_duration_seconds": spec["default_duration"],
            "affects": sorted(spec["effects"].keys()),
        }
        for key, spec in SCENARIOS.items()
    ]
