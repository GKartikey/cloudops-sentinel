"""Alert lifecycle, recommendation coverage, storage and the log parser."""

from __future__ import annotations

import pytest


# --------------------------------------------------------------------- alerts
class TestAlertEngine:
    @pytest.fixture
    def engine(self, store, rules_config):
        from app.engine.alerts import AlertEngine, load_rules

        rules, resolve_after = load_rules(rules_config)
        return AlertEngine(store, rules, resolve_after, sinks=[])

    @staticmethod
    def _latest(cpu: float, ts: float = 1000.0):
        return {"r1": {"cpu_utilization": (cpu, ts)}}

    def test_a_breach_starts_pending_not_firing(self, engine, store):
        """One bad scrape must never page anyone."""
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1000.0)
        alert = store.get_alert("HighCpuUtilization:r1")
        assert alert["status"] == "pending"

    def test_it_fires_once_the_for_duration_elapses(self, engine, store):
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1000.0)
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1070.0)
        assert store.get_alert("HighCpuUtilization:r1")["status"] == "firing"

    def test_notification_only_on_the_firing_edge(self, store, rules_config):
        """An alert that re-notifies every tick trains people to ignore it."""
        from app.engine.alerts import AlertEngine, load_rules

        events: list[tuple[str, str]] = []
        rules, resolve_after = load_rules(rules_config)
        engine = AlertEngine(
            store, rules, resolve_after,
            sinks=[lambda alert, event: events.append((alert["rule"], event))],
        )
        for ts in (1000.0, 1070.0, 1080.0, 1090.0, 1100.0):
            engine.evaluate(self._latest(99.0), {"r1": "web"}, now=ts)
        fired = [e for e in events if e[1] == "fired"]
        # HighCpu and CriticalCpu each fire exactly once, never repeatedly.
        assert len(fired) == 2
        assert {rule for rule, _ in fired} == {"HighCpuUtilization", "CriticalCpuUtilization"}

    def test_it_resolves_after_the_condition_clears(self, engine, store):
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1000.0)
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1070.0)
        # Recovered, and quiet for longer than resolve_after_seconds.
        engine.evaluate(self._latest(20.0), {"r1": "web"}, now=1400.0)
        assert store.get_alert("HighCpuUtilization:r1")["status"] == "resolved"

    def test_the_fingerprint_is_stable(self, engine, store):
        for ts in (1000.0, 1010.0, 1020.0, 1080.0):
            engine.evaluate(self._latest(99.0), {"r1": "web"}, now=ts)
        rows = [a for a in store.list_alerts() if a["rule"] == "HighCpuUtilization"]
        assert len(rows) == 1, "one problem must be one row, not one row per tick"

    def test_counter_rule_uses_growth_not_absolute_value(self, engine, store):
        """The bug this guards against: an alert that can never resolve.

        restart_count only ever increases, so comparing it directly latches
        forever. The rule is configured with aggregation: increase.
        """
        now = 10_000.0
        latest = {"r1": {"restart_count": (42.0, now)}}
        # The counter is high, but it has not moved in the window.
        flat = {"r1": {"restart_count": [(now - 500, 42.0), (now, 42.0)]}}
        engine.evaluate(latest, {"r1": "worker"}, now=now, window=flat)
        assert store.get_alert("ContainerRestartLoop:r1") is None

        # Same absolute value, but it grew by 5 inside the window.
        climbing = {"r1": {"restart_count": [(now - 500, 37.0), (now, 42.0)]}}
        engine.evaluate(latest, {"r1": "worker"}, now=now, window=climbing)
        assert store.get_alert("ContainerRestartLoop:r1")["status"] == "firing"

    def test_a_broken_sink_cannot_break_the_loop(self, store, rules_config):
        from app.engine.alerts import AlertEngine, load_rules

        def explode(alert, event):
            raise RuntimeError("pager is on fire")

        rules, resolve_after = load_rules(rules_config)
        engine = AlertEngine(store, rules, resolve_after, sinks=[explode])
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1000.0)
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1070.0)
        assert store.get_alert("HighCpuUtilization:r1")["status"] == "firing"

    def test_acknowledgement_is_recorded(self, engine, store):
        engine.evaluate(self._latest(99.0), {"r1": "web"}, now=1000.0)
        assert store.acknowledge_alert("HighCpuUtilization:r1", "alice", 1100.0) is True
        alert = store.get_alert("HighCpuUtilization:r1")
        assert alert["acked_by"] == "alice"
        assert store.acknowledge_alert("NoSuchAlert:r9", "alice", 1100.0) is False

    def test_rules_all_reference_a_runbook(self, rules_config):
        from app.engine.alerts import load_rules

        rules, _ = load_rules(rules_config)
        assert rules, "no alert rules loaded"
        for rule in rules:
            assert rule.runbook, f"{rule.name} has no runbook - unactionable at 3am"
            assert rule.severity in ("critical", "warning", "info")


# ------------------------------------------------------------ recommendations
class TestRecommendationEngine:
    @pytest.fixture
    def engine(self, rules_config, cost_model):
        from app.engine.recommendations import RecommendationEngine

        return RecommendationEngine(rules_config.get("recommendations", {}), cost_model)

    @staticmethod
    def _window(inventory, cpu: float = 50.0, mem: float = 55.0, samples: int = 60):
        return {
            r.id: {
                "cpu_utilization": [cpu] * samples,
                "memory_utilization": [mem] * samples,
                "error_rate": [0.001] * samples,
                "requests_per_second": [100.0] * samples,
                "restart_count": [0.0] * samples,
                "availability": [1.0] * samples,
            }
            for r in inventory
        }

    def test_every_required_category_is_produced(self, engine, inventory):
        """The six categories the platform promises, all present on real data.

        Note the two utilisation bands: over_provisioned and under_utilised are
        mutually exclusive by design (an idle box gets the stronger "retire it"
        finding, not "shrink it"), so a fleet pinned at one utilisation can only
        ever produce one of them.
        """
        window = self._window(inventory, 25.0, 30.0)      # over-provisioned band
        window["azure-vm-batch-02"]["cpu_utilization"] = [5.0] * 60
        window["azure-vm-batch-02"]["memory_utilization"] = [9.0] * 60
        findings = engine.analyse(inventory.resources, window, {})
        categories = {f["category"] for f in findings}
        for required in (
            "over_provisioned",
            "under_utilised",
            "suspicious_configuration",
            "missing_health_check",
        ):
            assert required in categories, f"missing {required}"

    def test_unhealthy_and_error_findings_appear(self, engine, inventory):
        window = self._window(inventory)
        window["svc-checkout-api"]["error_rate"] = [0.22] * 60
        window["svc-checkout-api"]["restart_count"] = [float(i) for i in range(60)]
        health = {"svc-checkout-api": {"status": "unhealthy", "score": 12, "reasons": ["errors"]}}
        findings = engine.analyse(inventory.resources, window, health)
        categories = {f["category"] for f in findings}
        assert "high_error_rate" in categories
        assert "unhealthy_container" in categories

    def test_it_refuses_to_advise_without_evidence(self, engine, inventory):
        """Three data points is not a basis for halving a production database."""
        thin = {r.id: {"cpu_utilization": [4.0, 4.1, 3.9]} for r in inventory}
        findings = engine.analyse(inventory.resources, thin, {})
        sizing = [f for f in findings if f["category"] in ("over_provisioned", "under_utilised")]
        assert sizing == []

    def test_every_finding_carries_evidence_and_an_action(self, engine, inventory):
        findings = engine.analyse(inventory.resources, self._window(inventory, 15.0, 18.0), {})
        assert findings
        for f in findings:
            assert f["evidence"], f"{f['id']} has no evidence"
            assert len(f["action"]) > 30, f"{f['id']} action is not specific"
            assert f["severity"] in ("critical", "high", "medium", "low", "info")
            assert f["confidence"] in ("high", "medium", "low")

    def test_savings_are_never_negative(self, engine, inventory):
        findings = engine.analyse(inventory.resources, self._window(inventory, 12.0, 15.0), {})
        for f in findings:
            assert f["monthly_saving"] >= 0
            assert f["annual_saving"] == pytest.approx(f["monthly_saving"] * 12, rel=0.001)

    def test_a_busy_prod_box_is_not_told_to_shrink(self, engine, inventory):
        window = self._window(inventory, 78.0, 74.0)
        findings = engine.analyse(inventory.resources, window, {})
        shrink = [f for f in findings if f["category"] == "over_provisioned"]
        assert shrink == [], "a well-utilised box must not be flagged for rightsizing"

    def test_orphans_are_found(self, engine, inventory):
        findings = engine.analyse(inventory.resources, self._window(inventory), {})
        orphans = {f["resource_id"] for f in findings if f["category"] == "orphaned_resource"}
        assert "azure-disk-orphan-01" in orphans
        assert "aws-eip-orphan-01" in orphans

    def test_public_database_is_a_critical_finding(self, engine, inventory):
        findings = engine.analyse(inventory.resources, self._window(inventory), {})
        rds = [
            f for f in findings
            if f["resource_id"] == "aws-rds-analytics" and f["category"] == "suspicious_configuration"
        ]
        assert rds and rds[0]["severity"] == "critical"

    def test_commitment_is_not_recommended_for_idle_resources(self, engine, inventory):
        """Committing to an idle box locks in the waste for a year."""
        findings = engine.analyse(inventory.resources, self._window(inventory, 5.0, 8.0), {})
        commitments = [f for f in findings if f["category"] == "commitment_discount"]
        assert commitments == []

    def test_summary_totals_reconcile(self, engine, inventory):
        from app.engine.recommendations import summarise

        findings = engine.analyse(inventory.resources, self._window(inventory, 20.0, 22.0), {})
        summary = summarise(findings)
        assert summary["total"] == len(findings)
        assert sum(summary["by_category"].values()) == len(findings)
        assert summary["monthly_saving"] == pytest.approx(
            sum(f["monthly_saving"] for f in findings), rel=0.001
        )


# --------------------------------------------------------------------- store
class TestStore:
    def test_samples_round_trip(self, store):
        store.insert_samples([(100.0, "r1", "cpu_utilization", 50.0)])
        assert store.recent_values("r1", "cpu_utilization", 0) == [50.0]

    def test_latest_returns_the_newest_per_series(self, store):
        store.insert_samples([
            (100.0, "r1", "cpu_utilization", 10.0),
            (200.0, "r1", "cpu_utilization", 20.0),
            (150.0, "r1", "memory_utilization", 60.0),
        ])
        latest = store.latest_samples()
        assert latest["r1"]["cpu_utilization"] == (20.0, 200.0)
        assert latest["r1"]["memory_utilization"] == (60.0, 150.0)

    def test_window_samples_are_ordered_oldest_first(self, store):
        store.insert_samples([
            (300.0, "r1", "cpu_utilization", 3.0),
            (100.0, "r1", "cpu_utilization", 1.0),
            (200.0, "r1", "cpu_utilization", 2.0),
        ])
        series = store.window_samples(0)["r1"]["cpu_utilization"]
        assert [v for _, v in series] == [1.0, 2.0, 3.0]

    def test_pruning_enforces_retention(self, store):
        store.insert_samples([
            (100.0, "r1", "cpu_utilization", 1.0),
            (900.0, "r1", "cpu_utilization", 2.0),
        ])
        store.prune(500.0)
        assert store.recent_values("r1", "cpu_utilization", 0) == [2.0]

    def test_log_search_filters(self, store):
        store.insert_logs([
            (100.0, "r1", "svc", "ERROR", "database timeout", {"a": 1}),
            (101.0, "r1", "svc", "INFO", "heartbeat", {}),
            (102.0, "r2", "svc", "ERROR", "disk full", {}),
        ])
        assert len(store.search_logs(0, level="ERROR")) == 2
        assert len(store.search_logs(0, resource_id="r2")) == 1
        assert len(store.search_logs(0, contains="timeout")) == 1
        assert store.log_counts_by_level(0) == {"ERROR": 2, "INFO": 1}

    def test_log_context_round_trips_as_json(self, store):
        store.insert_logs([(100.0, "r1", "svc", "WARN", "slow", {"duration_ms": 812})])
        assert store.search_logs(0)[0]["context"]["duration_ms"] == 812

    def test_incidents_expire(self, store):
        store.insert_incident({
            "id": "inc-1", "scenario": "cpu_spike", "resource_id": "r1",
            "started_at": 100.0, "ends_at": 200.0, "status": "active",
        })
        assert len(store.active_incidents(150.0)) == 1
        store.expire_incidents(300.0)
        assert store.active_incidents(300.0) == []


# ------------------------------------------------------------- text parsing
class TestPrometheusParser:
    def test_parses_values_and_skips_comments(self):
        from app.engine.collector import parse_prometheus_text

        body = (
            "# HELP demo_requests_total Requests\n"
            "# TYPE demo_requests_total counter\n"
            "demo_requests_total 42\n"
            "demo_cpu_utilization_percent 83.27\n"
        )
        parsed = parse_prometheus_text(body)
        assert parsed["demo_requests_total"] == 42.0
        assert parsed["demo_cpu_utilization_percent"] == 83.27

    def test_strips_labels_and_survives_junk(self):
        from app.engine.collector import parse_prometheus_text

        parsed = parse_prometheus_text(
            'http_requests_total{method="GET"} 7\nbroken_line\nbad_value NaNish\n\n'
        )
        assert parsed["http_requests_total"] == 7.0
        assert "broken_line" not in parsed
        assert "bad_value" not in parsed
