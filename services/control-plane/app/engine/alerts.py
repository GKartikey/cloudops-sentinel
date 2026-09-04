"""Alerting engine.

Mirrors Prometheus alerting semantics on purpose, because those semantics are
the ones that survived contact with real on-call rotations:

  pending -> firing    a rule must hold continuously for `for_seconds` before it
                       pages anyone. This single mechanism kills the majority of
                       false pages: one bad scrape is not an incident.
  firing  -> resolved  the condition stops matching and stays clear for
                       `resolve_after_seconds`, which stops a flapping target
                       from generating a fresh page every 30 seconds.

Every alert has a stable fingerprint (rule + resource), so the same problem
updates one row rather than creating thousands. State lives in SQLite, which
means alert state survives a restart of the control plane - an alerting system
that forgets everything when it is redeployed is worse than none.

Notification is fan-out to sinks. The webhook sink reads its URL from an
environment variable and is simply absent when unset; there is no default
endpoint and no credential in this file.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ..core.logging_setup import get_logger

log = get_logger("cloudops.alerts")


@dataclass
class AlertRule:
    name: str
    metric: str
    comparison: str          # gt | lt
    threshold: float
    for_seconds: float
    severity: str
    summary: str
    description: str = ""
    runbook: str = ""
    # How the rule reduces a series to one number before comparing:
    #   last     the newest sample (the default; right for gauges)
    #   increase how much the series grew across the window
    #   avg      mean over the window
    aggregation: str = "last"
    window_seconds: float = 0.0

    def matches(self, value: float) -> bool:
        return value > self.threshold if self.comparison == "gt" else value < self.threshold

    def reduce(self, latest: float, window: list[tuple[float, float]], now: float) -> float | None:
        """Collapse the evaluation window to the single number to compare.

        `increase` exists because a counter must never be alerted on directly.
        restart_count only ever goes up, so `restart_count > 2` latches on the
        first crash loop and stays firing until the counter is reset - which
        for a monotonic counter is never. Alerting on the GROWTH over a window
        is what makes the alert resolve once the crashing stops, and it is the
        same reason Prometheus rules are written increase(x[15m]) rather than x.
        """
        if self.aggregation == "last" or not self.window_seconds:
            return latest
        # Re-slice to this rule's own window; the caller supplies a wider one
        # shared by every analyser.
        cutoff = now - self.window_seconds
        values = [v for ts, v in window if ts >= cutoff]
        if len(values) < 2:
            return None
        if self.aggregation == "increase":
            return max(0.0, values[-1] - values[0])
        if self.aggregation == "avg":
            return sum(values) / len(values)
        return latest

    def fingerprint(self, resource_id: str) -> str:
        return f"{self.name}:{resource_id}"


def load_rules(config: dict) -> tuple[list[AlertRule], float]:
    section = (config or {}).get("alerting", {}) or {}
    rules = [
        AlertRule(
            name=r["name"],
            metric=r["metric"],
            comparison=r.get("comparison", "gt"),
            threshold=float(r["threshold"]),
            for_seconds=float(r.get("for_seconds", 0)),
            severity=r.get("severity", "warning"),
            summary=r.get("summary", "{rule} on {resource}"),
            description=r.get("description", ""),
            runbook=r.get("runbook", ""),
            aggregation=r.get("aggregation", "last"),
            window_seconds=float(r.get("window_seconds", 0) or 0),
        )
        for r in section.get("rules", [])
    ]
    return rules, float(section.get("resolve_after_seconds", 120))


# --------------------------------------------------------------------------- sinks
class LogSink:
    """Emits the alert as a structured log line. Always safe, always available.

    In a real cluster this is the sink that matters: the log stream is already
    shipped somewhere, so an alert here is one query away from an operator
    without any extra integration.
    """

    name = "log"

    def __call__(self, alert: dict, event: str) -> None:
        level = log.error if alert["severity"] == "critical" else log.warning
        level(
            f"alert {event}: {alert['summary']}",
            extra={
                "context": {
                    "event_type": "alert",
                    "alert_event": event,
                    "rule": alert["rule"],
                    "resource_id": alert["resource_id"],
                    "metric": alert["metric"],
                    "severity": alert["severity"],
                    "value": alert["value"],
                    "threshold": alert["threshold"],
                    "runbook": alert.get("runbook", ""),
                }
            },
        )


class WebhookSink:
    """POSTs an Alertmanager-shaped payload to a URL from the environment.

    Failures are logged and swallowed: a dead webhook must never take down the
    collection loop that produced the alert.
    """

    name = "webhook"

    def __init__(self, url: str, timeout: float = 3.0) -> None:
        self.url = url
        self.timeout = timeout

    def __call__(self, alert: dict, event: str) -> None:
        payload = {
            "version": "4",
            "status": "resolved" if event == "resolved" else "firing",
            "receiver": "cloudops-sentinel",
            "alerts": [
                {
                    "status": "resolved" if event == "resolved" else "firing",
                    "labels": {
                        "alertname": alert["rule"],
                        "resource": alert["resource_id"],
                        "severity": alert["severity"],
                        "metric": alert["metric"],
                    },
                    "annotations": {
                        "summary": alert["summary"],
                        "description": alert.get("description", ""),
                        "runbook_url": alert.get("runbook", ""),
                    },
                    "startsAt": alert.get("started_at"),
                    "value": alert["value"],
                }
            ],
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                log.info(
                    "alert webhook delivered",
                    extra={"context": {"status": response.status, "rule": alert["rule"]}},
                )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning(
                "alert webhook delivery failed",
                extra={"context": {"error": str(exc), "rule": alert["rule"]}},
            )


# --------------------------------------------------------------------------- engine
class AlertEngine:
    def __init__(
        self,
        store,
        rules: list[AlertRule],
        resolve_after_seconds: float = 120.0,
        sinks: list[Callable[[dict, str], None]] | None = None,
    ) -> None:
        self.store = store
        self.rules = rules
        self.resolve_after_seconds = resolve_after_seconds
        self.sinks = sinks or []

    def evaluate(
        self,
        latest: dict[str, dict[str, tuple[float, float]]],
        resource_names: dict[str, str],
        now: float | None = None,
        window: dict[str, dict[str, list[tuple[float, float]]]] | None = None,
    ) -> dict[str, list[dict]]:
        """Run every rule against every resource's newest sample.

        `window` supplies the recent history a windowed rule (increase / avg)
        needs; gauge rules ignore it entirely.
        """
        now = now or time.time()
        window = window or {}
        fired: list[dict] = []
        resolved: list[dict] = []
        matched_fingerprints: set[str] = set()

        for resource_id, metrics in latest.items():
            display = resource_names.get(resource_id, resource_id)
            for rule in self.rules:
                sample = metrics.get(rule.metric)
                if sample is None:
                    continue
                latest_value, _sample_ts = sample
                series = window.get(resource_id, {}).get(rule.metric, [])
                value = rule.reduce(latest_value, series, now)
                if value is None or not rule.matches(value):
                    continue

                fingerprint = rule.fingerprint(resource_id)
                matched_fingerprints.add(fingerprint)
                existing = self.store.get_alert(fingerprint)
                first_seen = (
                    existing["first_seen"]
                    if existing and existing["status"] != "resolved"
                    else now
                )
                held_for = now - first_seen
                status = "firing" if held_for >= rule.for_seconds else "pending"

                summary = rule.summary.format(
                    resource=display, value=round(value, 3), rule=rule.name
                )
                record = {
                    "fingerprint": fingerprint,
                    "rule": rule.name,
                    "resource_id": resource_id,
                    "metric": rule.metric,
                    "severity": rule.severity,
                    "status": status,
                    "value": round(value, 4),
                    "threshold": rule.threshold,
                    "summary": summary,
                    "description": rule.description,
                    "runbook": rule.runbook,
                    "first_seen": first_seen,
                    "started_at": now if status == "firing" else None,
                    "last_seen": now,
                }
                self.store.upsert_alert(record)

                # Notify only on the pending -> firing edge, never on every tick.
                was_firing = bool(existing) and existing["status"] == "firing"
                if status == "firing" and not was_firing:
                    self.store.record_alert_event(record, "fired", now)
                    self._notify(record, "fired")
                    fired.append(record)

        # Anything firing that no rule matched this tick is a resolution
        # candidate once it has been quiet for resolve_after_seconds.
        cutoff = now - self.resolve_after_seconds
        for stale in self.store.resolve_stale_alerts(cutoff, now):
            if stale["fingerprint"] in matched_fingerprints:
                continue
            stale["status"] = "resolved"
            self.store.record_alert_event(stale, "resolved", now)
            self._notify(stale, "resolved")
            resolved.append(stale)

        return {"fired": fired, "resolved": resolved}

    def _notify(self, alert: dict, event: str) -> None:
        for sink in self.sinks:
            try:
                sink(alert, event)
            except Exception as exc:  # noqa: BLE001 - a sink must never break the loop
                log.warning(
                    "alert sink raised",
                    extra={"context": {"sink": getattr(sink, "name", "?"), "error": str(exc)}},
                )

    def summary(self) -> dict[str, Any]:
        active = self.store.list_alerts(status="firing")
        pending = self.store.list_alerts(status="pending")
        by_severity: dict[str, int] = {}
        for alert in active:
            by_severity[alert["severity"]] = by_severity.get(alert["severity"], 0) + 1
        return {
            "firing": len(active),
            "pending": len(pending),
            "by_severity": by_severity,
            "critical": by_severity.get("critical", 0),
            "warning": by_severity.get("warning", 0),
            "unacknowledged": sum(1 for a in active if not a.get("acked_at")),
        }
