"""Runtime configuration.

Every knob is an environment variable with a safe default, so the service starts
with no configuration at all and is fully tunable in a container without a
rebuild. Nothing secret is ever defaulted to a real value - see SECRET_KEY and
ALERT_WEBHOOK_URL below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env_str(key, "true" if default else "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@dataclass(frozen=True)
class Settings:
    """Immutable view of the process configuration."""

    # --- identity -------------------------------------------------------
    service_name: str = field(
        default_factory=lambda: _env_str("SERVICE_NAME", "cloudops-control-plane")
    )
    environment: str = field(default_factory=lambda: _env_str("ENVIRONMENT", "local"))
    version: str = field(default_factory=lambda: _env_str("APP_VERSION", "1.0.0"))

    # --- server ---------------------------------------------------------
    host: str = field(default_factory=lambda: _env_str("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO").upper())
    log_format: str = field(default_factory=lambda: _env_str("LOG_FORMAT", "json").lower())

    # --- paths ----------------------------------------------------------
    config_dir: Path = field(
        default_factory=lambda: Path(_env_str("CONFIG_DIR", "/app/config")).resolve()
    )
    data_dir: Path = field(default_factory=lambda: Path(_env_str("DATA_DIR", "/data")).resolve())

    # --- collection -----------------------------------------------------
    collect_interval_seconds: int = field(
        default_factory=lambda: _env_int("COLLECT_INTERVAL_SECONDS", 10)
    )
    scrape_timeout_seconds: float = field(
        default_factory=lambda: _env_float("SCRAPE_TIMEOUT_SECONDS", 3.0)
    )
    retention_hours: int = field(default_factory=lambda: _env_int("RETENTION_HOURS", 24))
    # On first boot, backfill this many hours of synthetic history so the
    # dashboard, cost view and anomaly detector have something to work with
    # immediately instead of an empty screen.
    backfill_hours: int = field(default_factory=lambda: _env_int("BACKFILL_HOURS", 6))
    simulation_seed: int = field(default_factory=lambda: _env_int("SIMULATION_SEED", 20240917))

    # --- analysis -------------------------------------------------------
    anomaly_window: int = field(default_factory=lambda: _env_int("ANOMALY_WINDOW", 60))
    anomaly_z_threshold: float = field(
        default_factory=lambda: _env_float("ANOMALY_Z_THRESHOLD", 3.5)
    )
    anomaly_min_samples: int = field(default_factory=lambda: _env_int("ANOMALY_MIN_SAMPLES", 12))

    # --- security -------------------------------------------------------
    # Optional bearer token. When unset the API is open, which is correct for a
    # laptop demo and wrong for anything else - the K8s manifests wire a real
    # value in from a Secret. Never give this a real default.
    api_token: str = field(default_factory=lambda: _env_str("CLOUDOPS_API_TOKEN", ""))
    # Write endpoints (incident injection, alert acks) can be locked down even
    # when reads are open.
    require_token_for_writes: bool = field(
        default_factory=lambda: _env_bool("REQUIRE_TOKEN_FOR_WRITES", False)
    )
    cors_origins: str = field(default_factory=lambda: _env_str("CORS_ORIGINS", "*"))

    # --- alerting sinks -------------------------------------------------
    # Empty by default: an unconfigured webhook must never fall back to some
    # baked-in endpoint.
    alert_webhook_url: str = field(default_factory=lambda: _env_str("ALERT_WEBHOOK_URL", ""))
    alert_log_enabled: bool = field(default_factory=lambda: _env_bool("ALERT_LOG_ENABLED", True))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cloudops.db"

    @property
    def inventory_path(self) -> Path:
        return self.config_dir / "inventory.yaml"

    @property
    def pricing_path(self) -> Path:
        return self.config_dir / "pricing.yaml"

    @property
    def rules_path(self) -> Path:
        return self.config_dir / "rules.yaml"

    def redacted(self) -> dict:
        """Config safe to expose on /api/v1/system - secrets become booleans."""
        return {
            "service_name": self.service_name,
            "environment": self.environment,
            "version": self.version,
            "collect_interval_seconds": self.collect_interval_seconds,
            "retention_hours": self.retention_hours,
            "backfill_hours": self.backfill_hours,
            "anomaly_window": self.anomaly_window,
            "anomaly_z_threshold": self.anomaly_z_threshold,
            "auth_enabled": bool(self.api_token),
            "require_token_for_writes": self.require_token_for_writes,
            "alert_webhook_configured": bool(self.alert_webhook_url),
        }


settings = Settings()
