"""Anomaly detection, health scoring and cost estimation."""

from __future__ import annotations

import pytest


class TestAnomalyDetector:
    @pytest.fixture
    def detector(self):
        from app.engine.anomaly import AnomalyDetector

        return AnomalyDetector(z_threshold=3.5, min_samples=12)

    def test_stays_quiet_on_normal_variation(self, detector):
        history = [40 + (i % 5) - 2 for i in range(60)]
        assert detector.evaluate("r", "cpu_utilization", history, 41.0, 0) is None

    def test_catches_a_level_shift(self, detector):
        history = [40 + (i % 5) - 2 for i in range(60)]
        found = detector.evaluate("r", "cpu_utilization", history, 97.0, 0)
        assert found is not None
        assert found["direction"] == "up"
        assert found["method"] == "robust_z"
        assert found["severity"] in ("warning", "critical")

    def test_refuses_to_guess_without_enough_history(self, detector):
        assert detector.evaluate("r", "cpu_utilization", [40, 41, 39], 99.0, 0) is None

    def test_ignores_improvements_on_upward_only_metrics(self, detector):
        """Latency falling is good news and must never page anyone."""
        history = [500 + (i % 7) for i in range(60)]
        assert detector.evaluate("r", "latency_p95_ms", history, 12.0, 0) is None

    def test_flat_series_does_not_explode(self, detector):
        """MAD is 0 on a perfectly flat series - a naive z-score divides by zero.

        The absolute-delta floor is what stops 0.2% -> 0.4% error rate from
        being reported as an infinite-sigma event.
        """
        history = [0.002] * 60
        assert detector.evaluate("r", "error_rate", history, 0.004, 0) is None
        # A genuinely large move on the same flat series still registers.
        assert detector.evaluate("r", "error_rate", history, 0.35, 0) is not None

    def test_single_outlier_does_not_blind_the_detector(self, detector):
        """The reason for median+MAD instead of mean+stdev.

        One enormous spike inflates a standard deviation so much that the next
        genuine anomaly falls inside it. A robust estimator shrugs it off.
        """
        history = [40.0] * 59 + [5000.0]
        found = detector.evaluate("r", "cpu_utilization", history, 95.0, 0)
        assert found is not None

    def test_catches_a_slow_ramp_via_ewma(self, detector):
        """A leak never looks sharp at any single sample, so z stays low."""
        history = [30 + i * 0.6 for i in range(80)]
        found = detector.evaluate("r", "memory_utilization", history, 95.0, 0)
        assert found is not None

    def test_scan_skips_series_without_history(self, detector):
        latest = {"r1": {"cpu_utilization": (99.0, 100.0)}}
        assert detector.scan(latest, {}, 100.0) == []


class TestHealth:
    def test_a_clean_resource_scores_full_marks(self, inventory):
        from app.engine.health import evaluate_resource

        resource = inventory.get("azure-vm-web-01")
        latest = {
            "cpu_utilization": (35.0, 1000.0),
            "memory_utilization": (50.0, 1000.0),
            "error_rate": (0.001, 1000.0),
            "availability": (1.0, 1000.0),
            "latency_p95_ms": (120.0, 1000.0),
        }
        result = evaluate_resource(resource, latest, 1000.0)
        assert result["status"] == "healthy"
        assert result["score"] == 100
        assert result["reasons"] == []

    def test_a_failing_probe_dominates_the_score(self, inventory):
        from app.engine.health import evaluate_resource

        resource = inventory.get("azure-vm-web-01")
        latest = {"availability": (0.0, 1000.0), "cpu_utilization": (5.0, 1000.0)}
        result = evaluate_resource(resource, latest, 1000.0)
        assert result["status"] == "unhealthy"
        assert "health probe failing" in result["reasons"]

    def test_one_metric_is_penalised_once(self, inventory):
        """97% CPU must not be charged for breaching both 85 and 95."""
        from app.engine.health import evaluate_resource

        resource = inventory.get("azure-vm-web-01")
        result = evaluate_resource(
            resource, {"cpu_utilization": (97.0, 1000.0), "availability": (1.0, 1000.0)}, 1000.0
        )
        cpu_reasons = [r for r in result["reasons"] if "CPU" in r]
        assert len(cpu_reasons) == 1

    def test_stale_telemetry_is_a_finding_not_a_pass(self, inventory):
        from app.engine.health import evaluate_resource

        resource = inventory.get("azure-vm-web-01")
        # Sample is 10 minutes old.
        result = evaluate_resource(resource, {"cpu_utilization": (10.0, 400.0)}, 1000.0)
        assert result["stale"] is True
        assert any("stale" in r for r in result["reasons"])

    def test_metricless_resource_is_not_counted_as_unknown(self, inventory):
        """An unattached elastic IP emits nothing. That is not a blind spot."""
        from app.engine.health import evaluate_resource

        resource = inventory.get("aws-eip-orphan-01")
        result = evaluate_resource(resource, {}, 1000.0)
        assert result["status"] == "not_monitored"

    def test_fleet_status_follows_the_worst_resource(self, inventory):
        from app.engine.health import evaluate_fleet

        latest = {
            r.id: {"availability": (1.0, 1000.0), "cpu_utilization": (20.0, 1000.0)}
            for r in inventory
        }
        assert evaluate_fleet(inventory.resources, latest, 1000.0)["status"] == "healthy"
        latest["azure-vm-web-01"]["availability"] = (0.0, 1000.0)
        assert evaluate_fleet(inventory.resources, latest, 1000.0)["status"] == "unhealthy"


class TestCostModel:
    def test_vm_cost_matches_the_rate_card(self, cost_model, inventory):
        resource = inventory.get("azure-vm-web-01")   # 8 vCPU, 32 GiB, 256 GB
        estimate = cost_model.estimate(resource)
        expected = (
            8 * 0.0415 * 730       # vCPU
            + 32 * 0.0056 * 730    # memory
            + 256 * 0.088          # disk
        )
        assert estimate["monthly_cost"] == pytest.approx(expected, rel=0.001)
        assert estimate["hourly_cost"] == pytest.approx(expected / 730, rel=0.01)

    def test_region_multiplier_is_applied(self, cost_model, inventory):
        westeurope = inventory.get("azure-vm-legacy-jump")
        assert cost_model.region_multiplier(westeurope.region) == 1.12
        assert cost_model.estimate(westeurope)["region_multiplier"] == 1.12

    def test_serverless_free_tier_is_deducted(self, cost_model, inventory):
        # 2.4M invocations with a 1M free tier bills for 1.4M, not 2.4M.
        resource = inventory.get("aws-lambda-thumbs")
        estimate = cost_model.estimate(resource)
        assert estimate["components"]["invocations"] == pytest.approx(1.4 * 0.20, rel=0.01)

    def test_idle_resource_shows_waste(self, cost_model, inventory):
        resource = inventory.get("aws-rds-analytics")
        result = cost_model.efficiency(
            resource, {"cpu_utilization": 9.0, "memory_utilization": 21.0}
        )
        assert result["waste_monthly"] > 0
        assert result["efficiency"] < 0.5

    def test_a_busy_resource_is_not_called_wasteful(self, cost_model, inventory):
        """80% utilisation is a well-sized box, not a 20% waste."""
        resource = inventory.get("azure-vm-web-01")
        result = cost_model.efficiency(
            resource, {"cpu_utilization": 82.0, "memory_utilization": 70.0}
        )
        assert result["efficiency"] == 1.0
        assert result["waste_monthly"] == 0.0

    def test_consumption_billed_types_never_show_waste(self, cost_model, inventory):
        """You cannot over-provision something billed per request."""
        resource = inventory.get("aws-s3-backups")
        assert cost_model.efficiency(resource, {})["waste_monthly"] == 0.0

    def test_detached_disk_is_100_percent_waste(self, cost_model, inventory):
        resource = inventory.get("azure-disk-orphan-01")
        result = cost_model.efficiency(resource, {})
        assert result["efficiency"] == 0.0
        assert result["waste_monthly"] == result["monthly_cost"]

    def test_resize_saves_money(self, cost_model, inventory):
        resource = inventory.get("azure-vm-batch-02")   # 16 vCPU / 128 GiB
        delta = cost_model.resize_saving(resource, 4, 32)
        assert delta["monthly_saving"] > 0
        assert delta["proposed_monthly"] < delta["current_monthly"]
        assert delta["annual_saving"] == pytest.approx(delta["monthly_saving"] * 12)

    def test_resize_does_not_mutate_the_original(self, cost_model, inventory):
        resource = inventory.get("azure-vm-batch-02")
        before = dict(resource.spec)
        cost_model.resize_saving(resource, 2, 8)
        assert resource.spec == before

    def test_fleet_rollup_reconciles(self, cost_model, inventory):
        utilization = {r.id: {"cpu_utilization": 50.0, "memory_utilization": 50.0} for r in inventory}
        fleet = cost_model.fleet(inventory.resources, utilization)
        assert fleet["total_monthly"] == pytest.approx(
            sum(r["monthly_cost"] for r in fleet["resources"]), rel=0.001
        )
        assert sum(fleet["by_provider"].values()) == pytest.approx(fleet["total_monthly"], rel=0.001)
        assert fleet["total_annual"] == pytest.approx(fleet["total_monthly"] * 12, rel=0.001)
