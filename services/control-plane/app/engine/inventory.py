"""Cloud resource inventory.

Loads config/inventory.yaml into typed records. In a real deployment this module
is the only thing that changes: swap `load_inventory` for a function that calls
the Azure Resource Graph or the AWS Resource Groups Tagging API and every
analyser downstream keeps working, because they only ever see `Resource`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Metric names are a closed vocabulary. Every producer (simulator, live scraper)
# emits these names and every consumer (alerts, anomalies, cost, dashboard,
# Prometheus exposition) reads them. Keeping the list here stops the two halves
# from drifting apart.
METRICS = (
    "cpu_utilization",       # percent 0-100
    "memory_utilization",    # percent 0-100
    "disk_utilization",      # percent 0-100
    "restart_count",         # monotonic-ish counter of process restarts
    "latency_p95_ms",        # request latency, 95th percentile
    "error_rate",            # ratio 0-1
    "requests_per_second",   # throughput
    "availability",          # 1 = probe succeeded, 0 = probe failed
)

# Which metrics are meaningful for which resource type. A storage account has no
# CPU; reporting a fabricated 0% would pollute every fleet average.
TYPE_METRICS: dict[str, tuple[str, ...]] = {
    "virtual_machine": ("cpu_utilization", "memory_utilization", "disk_utilization",
                        "restart_count", "latency_p95_ms", "error_rate",
                        "requests_per_second", "availability"),
    "kubernetes_node": ("cpu_utilization", "memory_utilization", "disk_utilization",
                        "restart_count", "latency_p95_ms", "error_rate",
                        "requests_per_second", "availability"),
    "container_workload": ("cpu_utilization", "memory_utilization", "restart_count",
                           "latency_p95_ms", "error_rate", "requests_per_second",
                           "availability"),
    "managed_database": ("cpu_utilization", "memory_utilization", "disk_utilization",
                         "latency_p95_ms", "error_rate", "requests_per_second",
                         "availability"),
    "serverless_function": ("cpu_utilization", "memory_utilization", "latency_p95_ms",
                            "error_rate", "requests_per_second", "availability"),
    "object_storage": ("latency_p95_ms", "error_rate", "requests_per_second",
                       "availability"),
    "load_balancer": ("latency_p95_ms", "error_rate", "requests_per_second",
                      "availability"),
    "managed_disk": ("disk_utilization",),
    "public_ip": (),
}

# Types whose bill scales with provisioned compute, so rightsizing applies.
COMPUTE_TYPES = frozenset(
    {"virtual_machine", "kubernetes_node", "container_workload", "managed_database"}
)


@dataclass
class Resource:
    id: str
    name: str
    provider: str
    subscription: str
    resource_group: str
    type: str
    sku: str
    region: str
    environment: str
    owner: str
    source: str = "simulated"
    endpoint: str = ""
    tags: dict[str, Any] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.source == "live" and bool(self.endpoint)

    @property
    def metrics(self) -> tuple[str, ...]:
        return TYPE_METRICS.get(self.type, METRICS)

    @property
    def vcpu(self) -> float:
        return float(self.spec.get("vcpu", 0) or 0)

    @property
    def memory_gb(self) -> float:
        return float(self.spec.get("memory_gb", 0) or 0)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "provider": self.provider,
            "subscription": self.subscription, "resource_group": self.resource_group,
            "type": self.type, "sku": self.sku, "region": self.region,
            "environment": self.environment, "owner": self.owner,
            "source": self.source, "endpoint": self.endpoint, "tags": self.tags,
            "spec": self.spec, "config": self.config,
        }


@dataclass
class Inventory:
    subscriptions: list[dict]
    resources: list[Resource]

    def __post_init__(self) -> None:
        self._by_id = {r.id: r for r in self.resources}

    def get(self, resource_id: str) -> Resource | None:
        return self._by_id.get(resource_id)

    def __iter__(self):
        return iter(self.resources)

    def __len__(self) -> int:
        return len(self.resources)

    @property
    def live(self) -> list[Resource]:
        return [r for r in self.resources if r.is_live]

    @property
    def simulated(self) -> list[Resource]:
        return [r for r in self.resources if not r.is_live]

    def summary(self) -> dict:
        by_provider: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_environment: dict[str, int] = {}
        for r in self.resources:
            by_provider[r.provider] = by_provider.get(r.provider, 0) + 1
            by_type[r.type] = by_type.get(r.type, 0) + 1
            by_environment[r.environment] = by_environment.get(r.environment, 0) + 1
        return {
            "total": len(self.resources),
            "by_provider": by_provider,
            "by_type": by_type,
            "by_environment": by_environment,
            "live": len(self.live),
            "simulated": len(self.simulated),
            "subscriptions": self.subscriptions,
        }


class InventoryError(RuntimeError):
    pass


_REQUIRED = ("id", "name", "provider", "type")


def load_inventory(path: Path) -> Inventory:
    if not path.exists():
        raise InventoryError(f"inventory file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("resources") or []
    if not entries:
        raise InventoryError(f"inventory file has no resources: {path}")

    resources: list[Resource] = []
    seen: set[str] = set()
    for entry in entries:
        missing = [k for k in _REQUIRED if not entry.get(k)]
        if missing:
            raise InventoryError(
                f"resource {entry.get('id', '<unnamed>')} missing fields: {missing}"
            )
        if entry["id"] in seen:
            raise InventoryError(f"duplicate resource id: {entry['id']}")
        seen.add(entry["id"])
        resources.append(
            Resource(
                id=entry["id"],
                name=entry["name"],
                provider=entry["provider"],
                subscription=entry.get("subscription", "unknown"),
                resource_group=entry.get("resource_group", "default"),
                type=entry["type"],
                sku=str(entry.get("sku", "")),
                region=entry.get("region", "unknown"),
                environment=entry.get("environment", "unknown"),
                owner=entry.get("owner", "unassigned"),
                source=entry.get("source", "simulated"),
                endpoint=entry.get("endpoint", "") or "",
                tags=entry.get("tags") or {},
                spec=entry.get("spec") or {},
                config=entry.get("config") or {},
                profile=entry.get("profile") or {},
            )
        )

    return Inventory(subscriptions=raw.get("subscriptions") or [], resources=resources)


def load_yaml(path: Path) -> dict:
    """Shared loader for pricing.yaml / rules.yaml."""
    if not path.exists():
        raise InventoryError(f"config file not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
