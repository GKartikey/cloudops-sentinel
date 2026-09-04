"""Inventory loading and telemetry generation."""

from __future__ import annotations

import time

import pytest


class TestInventory:
    def test_loads_every_resource(self, inventory):
        assert len(inventory) >= 15
        assert inventory.get("azure-vm-web-01") is not None

    def test_ids_are_unique(self, inventory):
        ids = [r.id for r in inventory]
        assert len(ids) == len(set(ids))

    def test_live_resources_declare_an_endpoint(self, inventory):
        # A resource marked live without an endpoint would silently never be
        # scraped, and would look healthy forever.
        for resource in inventory.live:
            assert resource.endpoint, f"{resource.id} is live but has no endpoint"
            assert resource.endpoint.startswith("http")

    def test_summary_totals_reconcile(self, inventory):
        summary = inventory.summary()
        assert summary["total"] == len(inventory)
        assert sum(summary["by_provider"].values()) == summary["total"]
        assert summary["live"] + summary["simulated"] == summary["total"]

    def test_rejects_duplicate_ids(self, tmp_path):
        from app.engine.inventory import InventoryError, load_inventory

        path = tmp_path / "dupe.yaml"
        path.write_text(
            "resources:\n"
            "  - {id: a, name: a, provider: azure, type: virtual_machine}\n"
            "  - {id: a, name: b, provider: azure, type: virtual_machine}\n",
            encoding="utf-8",
        )
        with pytest.raises(InventoryError, match="duplicate"):
            load_inventory(path)

    def test_rejects_missing_required_fields(self, tmp_path):
        from app.engine.inventory import InventoryError, load_inventory

        path = tmp_path / "bad.yaml"
        path.write_text("resources:\n  - {id: a, name: a}\n", encoding="utf-8")
        with pytest.raises(InventoryError, match="missing fields"):
            load_inventory(path)

    def test_metric_vocabulary_is_type_appropriate(self, inventory):
        # A storage account has no CPU. Reporting a fabricated 0% would drag
        # every fleet average down and make rightsizing maths meaningless.
        blob = inventory.get("azure-blob-archive")
        assert "cpu_utilization" not in blob.metrics
        vm = inventory.get("azure-vm-web-01")
        assert "cpu_utilization" in vm.metrics


class TestSimulator:
    def test_is_deterministic(self, simulator, inventory):
        """The property the six-hour backfill depends on."""
        resource = inventory.get("azure-vm-web-01")
        ts = 1_700_000_000.0
        first = simulator.collect(resource, ts)
        second = simulator.collect(resource, ts)
        assert first == second

    def test_different_seeds_diverge(self, inventory):
        from app.engine.simulator import TelemetrySimulator

        resource = inventory.get("azure-vm-web-01")
        ts = 1_700_000_000.0
        a = TelemetrySimulator(1).collect(resource, ts)
        b = TelemetrySimulator(2).collect(resource, ts)
        assert a["cpu_utilization"] != b["cpu_utilization"]

    def test_values_stay_in_range(self, simulator, inventory):
        start = time.time() - 7200
        for resource in inventory.simulated:
            for step in range(0, 7200, 300):
                for metric, value in simulator.collect(resource, start + step).items():
                    if metric.endswith("_utilization"):
                        assert 0.0 <= value <= 100.0, f"{resource.id}.{metric}={value}"
                    elif metric in ("error_rate", "availability"):
                        assert 0.0 <= value <= 1.0, f"{resource.id}.{metric}={value}"
                    else:
                        assert value >= 0.0, f"{resource.id}.{metric}={value}"

    def test_series_is_autocorrelated_not_white_noise(self, simulator, inventory):
        """Consecutive samples must be related, or z-score detection is a lie.

        With white noise every detector looks brilliant, because every point is
        independent. Real telemetry wanders, so the generator has to as well.
        """
        resource = inventory.get("aws-ec2-api-01")
        start = 1_700_000_000.0
        series = [simulator.sample(resource, "cpu_utilization", start + i * 10) for i in range(200)]
        steps = [abs(series[i + 1] - series[i]) for i in range(len(series) - 1)]
        spread = max(series) - min(series)
        # Average step between adjacent samples must be far smaller than the
        # total range covered - that is what autocorrelation looks like.
        assert sum(steps) / len(steps) < spread * 0.35

    def test_idle_resources_stay_idle(self, simulator, inventory):
        idle = inventory.get("azure-vm-batch-02")
        start = 1_700_000_000.0
        values = [simulator.sample(idle, "cpu_utilization", start + i * 60) for i in range(120)]
        assert max(values) < 25, "an idle box should never look busy"

    def test_incident_raises_the_metric_it_targets(self, simulator, inventory):
        resource = inventory.get("aws-ec2-api-01")
        now = 1_700_000_000.0
        incident = {
            "id": "inc-test",
            "scenario": "cpu_spike",
            "resource_id": resource.id,
            "started_at": now,
            "ends_at": now + 300,
            "magnitude": 1.0,
        }
        baseline = simulator.sample(resource, "cpu_utilization", now + 60)
        during = simulator.sample(resource, "cpu_utilization", now + 60, [incident])
        assert during > baseline + 20

    def test_incident_does_not_touch_other_resources(self, simulator, inventory):
        target = inventory.get("aws-ec2-api-01")
        bystander = inventory.get("azure-vm-web-01")
        now = 1_700_000_000.0
        incident = {
            "id": "inc-test",
            "scenario": "cpu_spike",
            "resource_id": target.id,
            "started_at": now,
            "ends_at": now + 300,
            "magnitude": 1.0,
        }
        assert simulator.sample(
            bystander, "cpu_utilization", now + 60, [incident]
        ) == pytest.approx(simulator.sample(bystander, "cpu_utilization", now + 60))

    def test_ramped_incident_grows_over_time(self, simulator, inventory):
        """A memory leak must look like a leak, not a step change."""
        resource = inventory.get("aws-ec2-api-01")
        now = 1_700_000_000.0
        incident = {
            "id": "inc-leak",
            "scenario": "memory_leak",
            "resource_id": resource.id,
            "started_at": now,
            "ends_at": now + 600,
            "magnitude": 1.0,
        }
        early = simulator.sample(resource, "memory_utilization", now + 30, [incident])
        late = simulator.sample(resource, "memory_utilization", now + 570, [incident])
        assert late > early + 10

    def test_outage_zeroes_availability(self, simulator, inventory):
        resource = inventory.get("aws-ec2-api-01")
        now = 1_700_000_000.0
        incident = {
            "id": "inc-out",
            "scenario": "outage",
            "resource_id": resource.id,
            "started_at": now,
            "ends_at": now + 240,
            "magnitude": 1.0,
        }
        assert simulator.sample(resource, "availability", now + 60, [incident]) == 0.0

    def test_every_scenario_is_well_formed(self):
        from app.engine.simulator import SCENARIOS, scenario_catalog

        catalog = scenario_catalog()
        assert len(catalog) == len(SCENARIOS)
        for entry in catalog:
            assert entry["label"] and entry["description"]
            assert entry["default_duration_seconds"] >= 30
            assert entry["affects"]
