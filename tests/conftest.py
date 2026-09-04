"""Shared fixtures.

The control plane is importable without a server, a container or a network,
which is what makes the analysis engines cheap to test. Every fixture here is
in-process and either in-memory or in tmp_path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE = ROOT / "services" / "control-plane"
if str(CONTROL_PLANE) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE))

CONFIG_DIR = ROOT / "config"


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture(scope="session")
def inventory():
    from app.engine.inventory import load_inventory

    return load_inventory(CONFIG_DIR / "inventory.yaml")


@pytest.fixture(scope="session")
def pricing() -> dict:
    from app.engine.inventory import load_yaml

    return load_yaml(CONFIG_DIR / "pricing.yaml")


@pytest.fixture(scope="session")
def rules_config() -> dict:
    from app.engine.inventory import load_yaml

    return load_yaml(CONFIG_DIR / "rules.yaml")


@pytest.fixture
def store(tmp_path):
    from app.core.store import Store

    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def cost_model(pricing):
    from app.engine.cost import CostModel

    return CostModel(pricing)


@pytest.fixture
def simulator():
    from app.engine.simulator import TelemetrySimulator

    return TelemetrySimulator(seed=1234)
