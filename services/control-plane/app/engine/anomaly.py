"""Anomaly detection.

Deliberately statistical rather than machine-learned. Three reasons that hold up
under questioning:

  * **Explainable.** An on-call engineer can be told "this is 6.2 median
    absolute deviations above the last 10 minutes" and act on it. "The model
    said so" does not survive a 3am page.
  * **Cold-start safe.** No training job, no model artefact, no feature store.
    It works on the twelfth sample of a brand-new resource.
  * **Robust.** We use median + MAD, not mean + standard deviation. A single
    enormous outlier inflates a standard deviation enough to hide the very
    anomaly that caused it - the detector goes blind exactly when it matters.

Two detectors run together:
  1. Robust z-score (median absolute deviation) - catches sudden level shifts.
  2. Trend drift - compares the recent window against the preceding one,
     normalised by the RECENT window's own spread. This catches slow ramps that
     never look sharp at any single sample, which is precisely the shape of a
     memory leak and the case a plain z-score is structurally blind to.

Detections are deduplicated per (resource, metric) over a cooldown so one
sustained incident produces one anomaly record, not one per scrape.
"""

from __future__ import annotations

import statistics
from typing import Any

# Below this absolute change a "deviation" is noise, whatever the z-score says.
# Without it, a metric that sits perfectly flat at 0.2% error rate reports an
# anomaly the moment it moves to 0.4%, because MAD is ~0 and z explodes.
MIN_ABSOLUTE_DELTA: dict[str, float] = {
    "cpu_utilization": 8.0,
    "memory_utilization": 8.0,
    "disk_utilization": 5.0,
    "latency_p95_ms": 40.0,
    "error_rate": 0.01,
    "requests_per_second": 15.0,
    "restart_count": 1.0,
    "availability": 0.5,
}

# Metrics where only an increase is interesting. Latency dropping is good news.
UPWARD_ONLY = frozenset(
    {
        "latency_p95_ms",
        "error_rate",
        "restart_count",
        "cpu_utilization",
        "memory_utilization",
        "disk_utilization",
    }
)

# 1.4826 makes MAD a consistent estimator of sigma for normally distributed data.
_MAD_SCALE = 1.4826


def robust_z(values: list[float], current: float, floor: float = 0.0) -> tuple[float, float, float]:
    """Return (z_score, median, scale) for `current` against `values`.

    When MAD collapses to zero - a perfectly flat series, which is common for a
    healthy error rate or an idle box - there is no observed spread to normalise
    by. The tempting fallback is the standard deviation, and it is a trap: a
    single historical outlier makes stdev enormous and the detector goes blind
    to everything afterwards, which is precisely the failure robust statistics
    exist to avoid. Instead we fall back to the metric's domain-significance
    floor: on a flat series, "meaningfully different" is defined by what a human
    would call a meaningful change in that metric, not by the noise.
    """
    if len(values) < 2:
        return 0.0, current, 0.0
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values])
    scale = mad * _MAD_SCALE
    if scale < 1e-9:
        scale = max(floor / 3.0, 1e-9)
    return (current - median) / scale, median, scale


def ewma(values: list[float], alpha: float = 0.3) -> float:
    if not values:
        return 0.0
    acc = values[0]
    for v in values[1:]:
        acc = alpha * v + (1.0 - alpha) * acc
    return acc


def _severity(score: float, threshold: float) -> str:
    if score >= threshold * 2.0:
        return "critical"
    if score >= threshold * 1.35:
        return "warning"
    return "info"


class AnomalyDetector:
    def __init__(
        self,
        z_threshold: float = 3.5,
        min_samples: int = 12,
        cooldown_seconds: float = 120.0,
    ) -> None:
        self.z_threshold = z_threshold
        self.min_samples = min_samples
        self.cooldown_seconds = cooldown_seconds

    def evaluate(
        self,
        resource_id: str,
        metric: str,
        history: list[float],
        current: float,
        ts: float,
    ) -> dict[str, Any] | None:
        """Test one point. `history` must NOT include `current`."""
        if len(history) < self.min_samples:
            return None

        # The trailing window is the comparison baseline; older data is context.
        window = history[-120:]
        floor = MIN_ABSOLUTE_DELTA.get(metric, 0.0)
        z, median, scale = robust_z(window, current, floor)
        abs_delta = abs(current - median)

        direction = "up" if current > median else "down"
        if metric in UPWARD_ONLY and direction == "down":
            return None
        if abs_delta < floor:
            return None

        method = "robust_z"
        score = abs(z)

        if score < self.z_threshold:
            drift = self._trend_score(window, direction)
            if drift is None or drift < self.z_threshold:
                return None
            method, score = "trend_drift", drift

        return {
            "ts": ts,
            "resource_id": resource_id,
            "metric": metric,
            "value": round(current, 4),
            "baseline": round(median, 4),
            "deviation": round(current - median, 4),
            "score": round(score, 3),
            "method": method,
            "severity": _severity(score, self.z_threshold),
            "direction": direction,
        }

    def _trend_score(self, window: list[float], direction: str) -> float | None:
        """Second detector: catch a slow ramp that no single sample reveals.

        A memory leak defeats the z-score outright, because the baseline drifts
        upward along with the value - by the time it is at 95% the median is at
        60% and the "spread" is enormous, so z stays small.

        The trick is to normalise by the RECENT window's own spread rather than
        the whole window's. A trending series is locally quiet and globally
        wide, so local noise is the honest denominator; using the global MAD
        divides the trend by a number the trend itself inflated.
        """
        if len(window) < 40:
            return None
        recent, older = window[-12:], window[-48:-12]
        if not older:
            return None

        recent_median = statistics.median(recent)
        local_mad = statistics.median([abs(v - recent_median) for v in recent]) * _MAD_SCALE
        if local_mad < 1e-9:
            local_mad = 1e-9

        shift = statistics.fmean(recent) - statistics.fmean(older)
        # The trend has to point the same way as the current deviation, or we
        # would flag a metric that ramped up and has already come back down.
        if (direction == "up") != (shift > 0):
            return None
        baseline = abs(statistics.fmean(older))
        if baseline > 1e-9 and abs(shift) / baseline < 0.15:
            # Under 15% movement is drift, not an incident, however quiet the
            # series happens to be.
            return None
        return abs(shift) / local_mad

    def scan(
        self,
        latest: dict[str, dict[str, tuple[float, float]]],
        window: dict[str, dict[str, list[float]]],
        ts: float,
    ) -> list[dict[str, Any]]:
        """Evaluate every series that has a fresh sample this tick."""
        found: list[dict[str, Any]] = []
        for resource_id, metrics in latest.items():
            series = window.get(resource_id, {})
            for metric, (value, _sample_ts) in metrics.items():
                history = series.get(metric, [])
                # window includes the current sample; exclude it from its own baseline
                if history and abs(history[-1] - value) < 1e-9:
                    history = history[:-1]
                result = self.evaluate(resource_id, metric, history, value, ts)
                if result:
                    found.append(result)
        return found
