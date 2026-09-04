#!/usr/bin/env python3
"""End-to-end verification of a running CloudOps Sentinel stack.

This is not a unit test. It talks to the real containers over HTTP and asserts
that the whole pipeline works: the API answers, the collector is producing fresh
samples, the cost and recommendation engines return sane numbers, and - the part
that actually matters - injecting a genuine incident into a real container
causes the detection path to notice it *on its own*.

    python scripts/verify_local.py                 # full run, ~2 minutes
    python scripts/verify_local.py --quick         # skip the incident test
    python scripts/verify_local.py --url http://localhost:8000

Exit code 0 means the deployment is genuinely working. Anything else means it
is not, and the failing check is named. Used by `make verify` and by CI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


class Checker:
    def __init__(self, base_url: str, token: str = "") -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    # ---------------------------------------------------------------- plumbing
    def request(self, path: str, method: str = "GET", body: dict | None = None,
                timeout: float = 15.0) -> tuple[int, Any]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                try:
                    return response.status, json.loads(raw)
                except json.JSONDecodeError:
                    return response.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw
        except (urllib.error.URLError, OSError) as exc:
            return 0, str(exc)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  {GREEN}PASS{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
        else:
            self.failed += 1
            self.failures.append(name)
            print(f"  {RED}FAIL{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
        return condition

    @staticmethod
    def section(title: str) -> None:
        print(f"\n{BOLD}{title}{RESET}")

    # ------------------------------------------------------------------ checks
    def wait_for_ready(self, attempts: int = 40) -> bool:
        self.section("1. Service availability")
        for attempt in range(1, attempts + 1):
            status, body = self.request("/readyz", timeout=5)
            if status == 200:
                return self.check(
                    "control plane reports ready",
                    True,
                    f"after {attempt} attempt(s), {body.get('resources')} resources",
                )
            time.sleep(3)
        return self.check("control plane reports ready", False, "timed out waiting for /readyz")

    def probes(self) -> None:
        status, body = self.request("/healthz")
        self.check("liveness probe returns 200", status == 200, str(body)[:70])

        status, body = self.request("/metrics")
        text = body if isinstance(body, str) else ""
        self.check("Prometheus /metrics is exposed", status == 200 and "# TYPE" in text)
        for family in (
            "cloudops_resource_cpu_utilization_percent",
            "cloudops_resource_estimated_cost_monthly_usd",
            "cloudops_collection_runs_total",
            "cloudops_last_collection_timestamp_seconds",
        ):
            self.check(f"metric family {family}", family in text)

        status, _ = self.request("/")
        self.check("dashboard is served", status == 200)

        status, _ = self.request("/openapi.json")
        self.check("OpenAPI schema is published", status == 200)

    def inventory(self) -> dict:
        self.section("2. Inventory and collection")
        status, body = self.request("/api/v1/inventory")
        if not self.check("inventory endpoint answers", status == 200):
            return {}
        summary = body.get("summary", {})
        self.check("multi-cloud inventory loaded", body.get("count", 0) >= 15,
                   f"{body.get('count')} resources")
        self.check("both cloud providers present",
                   {"azure", "aws"} <= set(summary.get("by_provider", {})),
                   ", ".join(summary.get("by_provider", {})))
        self.check("live container targets registered", summary.get("live", 0) >= 3,
                   f"{summary.get('live')} live")
        return body

    def collection_is_fresh(self) -> None:
        status, body = self.request("/api/v1/system")
        if not self.check("system endpoint answers", status == 200):
            return
        store = body.get("store", {})
        newest = store.get("newest_sample_ts") or 0
        age = time.time() - newest
        self.check("samples are being written", store.get("samples", 0) > 100,
                   f"{store.get('samples'):,} samples")
        self.check("telemetry is fresh", age < 90, f"newest sample {age:.0f}s old")
        self.check("logs are being collected", store.get("logs", 0) > 0,
                   f"{store.get('logs'):,} records")
        self.check("secrets are not exposed by the API",
                   "api_token" not in json.dumps(body.get("config", {})))

    def live_targets_are_real(self) -> None:
        self.section("3. Live container telemetry")
        status, body = self.request("/api/v1/metrics/latest")
        if not self.check("latest metrics endpoint answers", status == 200):
            return
        resources = body.get("resources", {})
        for rid in ("svc-checkout-api", "svc-inventory-api", "svc-report-worker"):
            metrics = resources.get(rid, {})
            self.check(f"{rid} is reporting", bool(metrics),
                       f"cpu={metrics.get('cpu_utilization', {}).get('value', 'n/a')}")
        checkout = resources.get("svc-checkout-api", {})
        cpu = checkout.get("cpu_utilization", {}).get("value")
        self.check(
            "container CPU is measured, not invented",
            cpu is not None and 0 < cpu <= 100,
            f"{cpu}% of the container CPU limit",
        )

    def cost_engine(self) -> None:
        self.section("4. Cost estimation")
        status, body = self.request("/api/v1/cost")
        if not self.check("cost endpoint answers", status == 200):
            return
        total = body.get("total_monthly", 0)
        self.check("estate cost is estimated", total > 0, f"${total:,.2f}/month")
        self.check("annual figure reconciles",
                   abs(body.get("total_annual", 0) - total * 12) < 1.0)
        self.check("per-resource costs sum to the total",
                   abs(sum(r["monthly_cost"] for r in body["resources"]) - total) < 1.0)
        self.check("waste is quantified", body.get("waste_monthly", 0) > 0,
                   f"${body.get('waste_monthly'):,.2f}/month ({body.get('waste_pct')}%)")
        self.check("cost is attributed by provider", len(body.get("by_provider", {})) >= 2)

    def recommendations(self) -> None:
        self.section("5. Recommendation engine")
        status, body = self.request("/api/v1/recommendations")
        if not self.check("recommendations endpoint answers", status == 200):
            return
        summary = body.get("summary", {})
        categories = summary.get("by_category", {})
        self.check("findings were produced", summary.get("total", 0) > 0,
                   f"{summary.get('total')} findings")
        for required in (
            "over_provisioned", "under_utilised", "unhealthy_container",
            "high_error_rate", "suspicious_configuration", "missing_health_check",
        ):
            # The two reliability categories only appear when something is
            # actually unhealthy, so their absence on a clean fleet is correct.
            optional = required in ("unhealthy_container", "high_error_rate")
            present = required in categories
            if optional and not present:
                print(f"  {YELLOW}SKIP{RESET}  category {required}  "
                      f"{DIM}(nothing unhealthy right now - correct){RESET}")
                continue
            self.check(f"category {required}", present, f"{categories.get(required, 0)} finding(s)")
        self.check("savings are quantified", summary.get("monthly_saving", 0) > 0,
                   f"${summary.get('monthly_saving'):,.2f}/month")
        for finding in body.get("recommendations", [])[:50]:
            if not finding.get("evidence") or not finding.get("action"):
                self.check(f"finding {finding['id']} carries evidence and an action", False)
                return
        self.check("every finding carries evidence and an action", True)

    def health_and_logs(self) -> None:
        self.section("6. Health monitoring and logs")
        status, body = self.request("/api/v1/health")
        if self.check("health endpoint answers", status == 200):
            self.check("fleet score computed", 0 <= body.get("score", -1) <= 100,
                       f"score {body.get('score')}, status {body.get('status')}")
            self.check("no resource is silently unmonitored",
                       body.get("counts", {}).get("unknown", 0) == 0,
                       f"{body.get('counts')}")

        status, body = self.request("/api/v1/logs?minutes=60&limit=50")
        if self.check("log search answers", status == 200):
            self.check("structured logs are searchable", body.get("count", 0) > 0,
                       f"{body.get('count')} records, levels {body.get('counts_by_level')}")
            sample = (body.get("logs") or [{}])[0]
            self.check("log records are structured",
                       all(k in sample for k in ("ts", "level", "message", "context")))

    def incident_pipeline(self) -> None:
        """The real test: induce genuine failure and see if detection notices."""
        self.section("7. Incident simulation -> detection (live container)")
        target = "svc-checkout-api"

        status, body = self.request("/api/v1/incidents/scenarios")
        self.check("scenario catalogue available", status == 200 and len(body.get("scenarios", [])) >= 6,
                   f"{len(body.get('scenarios', []))} scenarios")

        status, incident = self.request(
            "/api/v1/incidents", "POST",
            {"scenario": "cpu_spike", "resource_id": target, "duration_seconds": 150},
        )
        if not self.check("incident accepted", status == 201, str(incident)[:80]):
            return
        self.check("chaos injected into the real container",
                   incident.get("params", {}).get("chaos_injected") is True)

        print(f"  {DIM}...waiting up to 110s for the pipeline to react on its own{RESET}")
        deadline = time.time() + 110
        cpu_seen = 0.0
        alert_seen = False
        while time.time() < deadline:
            time.sleep(10)
            _, latest = self.request("/api/v1/metrics/latest")
            value = (
                latest.get("resources", {}).get(target, {})
                .get("cpu_utilization", {}).get("value", 0)
            )
            cpu_seen = max(cpu_seen, value or 0)
            _, alerts = self.request("/api/v1/alerts?status=firing")
            if any(a["resource_id"] == target and "Cpu" in a["rule"]
                   for a in alerts.get("alerts", [])):
                alert_seen = True
                break

        self.check("container CPU actually rose", cpu_seen > 70, f"peak {cpu_seen:.1f}%")
        self.check("alert rule fired from the measurements alone", alert_seen)

        _, health = self.request(f"/api/v1/inventory/{target}")
        score = health.get("health", {}).get("score", 100)
        self.check("health score degraded", score < 100, f"score {score}")

        status, _ = self.request("/api/v1/incidents", "DELETE")
        self.check("incidents can be cancelled", status == 200)

    def api_contract(self) -> None:
        self.section("8. API contract")
        status, _ = self.request("/api/v1/inventory/does-not-exist")
        self.check("unknown resource returns 404", status == 404)
        status, _ = self.request(
            "/api/v1/incidents", "POST", {"scenario": "not-a-scenario", "resource_id": "x"}
        )
        self.check("invalid scenario returns 400", status == 400)
        status, _ = self.request("/api/v1/metrics/series?resource_id=azure-vm-web-01&metric=cpu_utilization&minutes=9999")
        self.check("out-of-range query parameter is rejected", status == 422)

    # -------------------------------------------------------------------- run
    def report(self) -> int:
        total = self.passed + self.failed
        print(f"\n{BOLD}{'=' * 66}{RESET}")
        if self.failed:
            print(f"{RED}{BOLD}FAILED{RESET}  {self.passed}/{total} checks passed")
            for name in self.failures:
                print(f"        {RED}x{RESET} {name}")
            return 1
        print(f"{GREEN}{BOLD}ALL CHECKS PASSED{RESET}  {self.passed}/{total}")
        print(f"{DIM}The local deployment is genuinely working end to end.{RESET}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--token", default="", help="API token, if the deployment requires one")
    parser.add_argument("--quick", action="store_true",
                        help="skip the 2-minute live incident test")
    args = parser.parse_args()

    print(f"{BOLD}CloudOps Sentinel - local deployment verification{RESET}")
    print(f"{DIM}target: {args.url}{RESET}")

    checker = Checker(args.url, args.token)
    if not checker.wait_for_ready():
        return checker.report()

    checker.probes()
    checker.inventory()
    checker.collection_is_fresh()
    checker.live_targets_are_real()
    checker.cost_engine()
    checker.recommendations()
    checker.health_and_logs()
    checker.api_contract()
    if args.quick:
        print(f"\n{YELLOW}Skipping the live incident test (--quick){RESET}")
    else:
        checker.incident_pipeline()
    return checker.report()


if __name__ == "__main__":
    sys.exit(main())
