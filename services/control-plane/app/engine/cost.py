"""Cost estimation.

The core idea a FinOps conversation turns on: **you are billed for what you
provisioned, not for what you used.** So every resource gets two numbers:

    provisioned_monthly  what the rate card charges for the capacity allocated
    effective_monthly    what that capacity would cost if it were sized to the
                         observed p95 utilisation

The gap between them is waste, and it is what the recommendation engine turns
into concrete actions. Utilisation is read from the same metric store the
alerting path uses, so the cost view and the reliability view can never
disagree about what a box was doing.

Serverless and storage are billed on consumption rather than allocation, so for
those types provisioned == effective and the waste signal comes from tiering and
right-memory-sizing instead.
"""

from __future__ import annotations

from typing import Any

from .inventory import COMPUTE_TYPES, Resource


class CostModel:
    def __init__(self, pricing: dict) -> None:
        self.pricing = pricing
        self.rates: dict[str, dict] = pricing.get("rates", {})
        self.regions: dict[str, float] = pricing.get("region_multipliers", {})
        self.hours_per_month: float = float(pricing.get("hours_per_month", 730))
        self.commitments: dict[str, float] = pricing.get("commitments", {})
        self.currency: str = pricing.get("currency", "USD")

    # ---------------------------------------------------------------- helpers
    def region_multiplier(self, region: str) -> float:
        return float(self.regions.get(region, 1.0))

    def _rate(self, resource_type: str, key: str, default: float = 0.0) -> float:
        return float(self.rates.get(resource_type, {}).get(key, default))

    # ------------------------------------------------------------ estimation
    def estimate(self, resource: Resource) -> dict[str, Any]:
        """Provisioned cost for one resource, itemised by component."""
        rtype = resource.type
        spec = resource.spec or {}
        mult = self.region_multiplier(resource.region)
        hours = self.hours_per_month
        components: dict[str, float] = {}

        if rtype in (
            "virtual_machine",
            "kubernetes_node",
            "container_workload",
            "managed_database",
        ):
            vcpu = float(spec.get("vcpu", 0) or 0)
            ram = float(spec.get("memory_gb", 0) or 0)
            disk = float(spec.get("disk_gb", 0) or 0)
            components["compute_vcpu"] = vcpu * self._rate(rtype, "per_vcpu_hour") * hours
            components["compute_memory"] = ram * self._rate(rtype, "per_gb_ram_hour") * hours
            if disk:
                components["storage"] = disk * self._rate(rtype, "per_gb_disk_month")
            if rtype == "kubernetes_node":
                components["control_plane"] = self._rate(rtype, "per_node_hour") * hours
            if rtype == "managed_database":
                premium = self._rate(rtype, "service_premium", 1.0)
                for key in list(components):
                    components[key] *= premium

        elif rtype == "object_storage":
            gb = float(spec.get("storage_gb", 0) or 0)
            components["storage"] = gb * self._rate(rtype, "per_gb_month")

        elif rtype == "serverless_function":
            invocations = float(spec.get("invocations_per_month", 0) or 0)
            mem_gb = float(spec.get("memory_gb", 0) or 0)
            duration_s = float(spec.get("avg_duration_ms", 0) or 0) / 1000.0
            free_inv = self._rate(rtype, "free_invocations_per_month")
            free_gbs = self._rate(rtype, "free_gb_seconds_per_month")
            billable_inv = max(invocations - free_inv, 0.0)
            gb_seconds = max(invocations * mem_gb * duration_s - free_gbs, 0.0)
            components["invocations"] = (billable_inv / 1_000_000.0) * self._rate(
                rtype, "per_million_invocations"
            )
            components["duration"] = gb_seconds * self._rate(rtype, "per_gb_second")

        elif rtype == "load_balancer":
            components["fixed"] = self._rate(rtype, "per_hour") * hours
            components["data_processing"] = float(
                spec.get("processed_gb_per_month", 0) or 0
            ) * self._rate(rtype, "per_gb_processed")

        elif rtype == "managed_disk":
            gb = float(spec.get("disk_gb", 0) or 0)
            components["storage"] = gb * self._rate(rtype, "per_gb_month")

        elif rtype == "public_ip":
            components["fixed"] = self._rate(rtype, "per_hour") * hours

        components = {k: round(v * mult, 4) for k, v in components.items() if v}
        monthly = round(sum(components.values()), 2)

        return {
            "resource_id": resource.id,
            "name": resource.name,
            "provider": resource.provider,
            "type": rtype,
            "region": resource.region,
            "environment": resource.environment,
            "owner": resource.owner,
            "sku": resource.sku,
            "currency": self.currency,
            "region_multiplier": mult,
            "components": components,
            "monthly_cost": monthly,
            "hourly_cost": round(monthly / hours, 5) if hours else 0.0,
            "daily_cost": round(monthly / 30.42, 3),
        }

    # ------------------------------------------------------------ efficiency
    def efficiency(self, resource: Resource, utilization: dict[str, float]) -> dict[str, Any]:
        """Split a resource's bill into 'earning its keep' and 'waste'.

        Efficiency is driven by the *dominant* dimension: a box at 80% memory and
        5% CPU is not 42% efficient, it is 80% efficient, because you cannot
        shrink it past the dimension that is actually full.
        """
        estimate = self.estimate(resource)
        monthly = estimate["monthly_cost"]

        if resource.type in COMPUTE_TYPES:
            cpu = utilization.get("cpu_utilization")
            mem = utilization.get("memory_utilization")
            observed = [v for v in (cpu, mem) if v is not None]
            if observed:
                # Headroom is not waste: an 80%-utilised box is fully justified.
                util = max(observed)
                efficiency = min(util / 80.0, 1.0)
            else:
                util, efficiency = 0.0, 1.0
        elif resource.type in ("managed_disk", "public_ip"):
            attached = bool(resource.spec.get("attached", True))
            util = 100.0 if attached else 0.0
            efficiency = 1.0 if attached else 0.0
        else:
            # Consumption-billed: you only pay for what ran.
            util, efficiency = 100.0, 1.0

        effective = round(monthly * efficiency, 2)
        return {
            **estimate,
            "utilization_pct": round(util, 2),
            "efficiency": round(efficiency, 4),
            "effective_monthly_cost": effective,
            "waste_monthly": round(monthly - effective, 2),
        }

    # --------------------------------------------------------------- rollups
    def fleet(
        self, resources: list[Resource], utilization: dict[str, dict[str, float]]
    ) -> dict[str, Any]:
        rows = [self.efficiency(r, utilization.get(r.id, {})) for r in resources]
        total = round(sum(r["monthly_cost"] for r in rows), 2)
        waste = round(sum(r["waste_monthly"] for r in rows), 2)

        def group(field: str) -> dict[str, float]:
            out: dict[str, float] = {}
            for row in rows:
                out[row[field]] = round(out.get(row[field], 0.0) + row["monthly_cost"], 2)
            return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))

        return {
            "currency": self.currency,
            "total_monthly": total,
            "total_daily": round(total / 30.42, 2),
            "total_hourly": round(total / self.hours_per_month, 4),
            "total_annual": round(total * 12, 2),
            "waste_monthly": waste,
            "waste_annual": round(waste * 12, 2),
            "waste_pct": round(100.0 * waste / total, 2) if total else 0.0,
            "by_provider": group("provider"),
            "by_type": group("type"),
            "by_environment": group("environment"),
            "by_owner": group("owner"),
            "by_region": group("region"),
            "top_spenders": sorted(rows, key=lambda r: r["monthly_cost"], reverse=True)[:10],
            "top_waste": [
                r
                for r in sorted(rows, key=lambda r: r["waste_monthly"], reverse=True)
                if r["waste_monthly"] > 0.5
            ][:10],
            "resources": rows,
        }

    # ---------------------------------------------------------- what-if maths
    def resize_saving(
        self, resource: Resource, new_vcpu: float, new_memory_gb: float
    ) -> dict[str, Any]:
        """Cost delta if this resource were re-provisioned at a smaller size."""
        current = self.estimate(resource)
        shrunk = Resource(**{**resource.__dict__})
        shrunk.spec = {**resource.spec, "vcpu": new_vcpu, "memory_gb": new_memory_gb}
        proposed = self.estimate(shrunk)
        saving = round(current["monthly_cost"] - proposed["monthly_cost"], 2)
        return {
            "current_monthly": current["monthly_cost"],
            "proposed_monthly": proposed["monthly_cost"],
            "monthly_saving": saving,
            "annual_saving": round(saving * 12, 2),
            "proposed_vcpu": new_vcpu,
            "proposed_memory_gb": new_memory_gb,
        }

    def commitment_saving(self, monthly: float, kind: str = "reserved_1yr") -> float:
        discount = float(self.commitments.get(f"{kind}_discount", 0.0))
        return round(monthly * discount, 2)

    def schedule_saving(self, monthly: float) -> float:
        """Saving from stopping a non-prod resource outside business hours."""
        retention = float(self.commitments.get("business_hours_retention", 1.0))
        return round(monthly * (1.0 - retention), 2)

    def tiering_saving(self, gb: float, region: str) -> float:
        rate = self._rate("object_storage", "per_gb_month")
        cool = self._rate("object_storage", "cool_per_gb_month")
        return round(gb * (rate - cool) * self.region_multiplier(region), 2)
