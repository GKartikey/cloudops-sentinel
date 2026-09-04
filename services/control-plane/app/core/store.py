"""Persistence layer.

SQLite on a mounted volume. The choice is deliberate: the whole platform has to
run on a laptop with no managed database, and SQLite in WAL mode comfortably
handles the write rate here (a few hundred rows every collection tick). The
access pattern - append-only time series plus small mutable alert/incident
tables - is the same shape you would put in Timescale or Azure Monitor, so the
queries port upward without redesign.

All SQL lives in this module. Nothing above it knows the storage engine.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts          REAL NOT NULL,
    resource_id TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_lookup ON samples (resource_id, metric, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts     ON samples (ts);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    resource_id TEXT NOT NULL,
    service     TEXT NOT NULL,
    level       TEXT NOT NULL,
    message     TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_logs_ts    ON logs (ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_res   ON logs (resource_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs (level, ts DESC);

CREATE TABLE IF NOT EXISTS alerts (
    fingerprint TEXT PRIMARY KEY,
    rule        TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    metric      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending | firing | resolved
    value       REAL NOT NULL,
    threshold   REAL NOT NULL,
    summary     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    runbook     TEXT NOT NULL DEFAULT '',
    first_seen  REAL NOT NULL,
    started_at  REAL,                   -- when it transitioned to firing
    last_seen   REAL NOT NULL,
    resolved_at REAL,
    acked_at    REAL,
    acked_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status, severity);

CREATE TABLE IF NOT EXISTS alert_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    rule        TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    severity    TEXT NOT NULL,
    event       TEXT NOT NULL,          -- fired | resolved | acknowledged
    value       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_history_ts ON alert_history (ts DESC);

CREATE TABLE IF NOT EXISTS anomalies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    resource_id TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    baseline    REAL NOT NULL,
    deviation   REAL NOT NULL,
    score       REAL NOT NULL,
    method      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    direction   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_ts  ON anomalies (ts DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_res ON anomalies (resource_id, ts DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    scenario    TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    started_at  REAL NOT NULL,
    ends_at     REAL NOT NULL,
    status      TEXT NOT NULL,          -- active | completed | cancelled
    magnitude   REAL NOT NULL DEFAULT 1.0,
    params      TEXT NOT NULL DEFAULT '{}',
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status, started_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """Thin, thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets the collector write while the API reads without blocking.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ core
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    # -------------------------------------------------------------- samples
    def insert_samples(self, rows: Iterable[tuple[float, str, str, float]]) -> int:
        batch = list(rows)
        if not batch:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples (ts, resource_id, metric, value) VALUES (?, ?, ?, ?)",
                batch,
            )
            self._conn.commit()
        return len(batch)

    def latest_samples(self) -> dict[str, dict[str, tuple[float, float]]]:
        """{resource_id: {metric: (value, ts)}} for the newest row per series."""
        rows = self.query(
            """
            SELECT s.resource_id, s.metric, s.value, s.ts
            FROM samples s
            JOIN (
                SELECT resource_id, metric, MAX(ts) AS mts
                FROM samples GROUP BY resource_id, metric
            ) m
              ON m.resource_id = s.resource_id
             AND m.metric = s.metric
             AND m.mts = s.ts
            """
        )
        out: dict[str, dict[str, tuple[float, float]]] = {}
        for r in rows:
            out.setdefault(r["resource_id"], {})[r["metric"]] = (r["value"], r["ts"])
        return out

    def series(self, resource_id: str, metric: str, since: float, limit: int = 5000) -> list[dict]:
        return self.query(
            "SELECT ts, value FROM samples "
            "WHERE resource_id = ? AND metric = ? AND ts >= ? "
            "ORDER BY ts ASC LIMIT ?",
            (resource_id, metric, since, limit),
        )

    def recent_values(self, resource_id: str, metric: str, since: float) -> list[float]:
        return [
            r["value"]
            for r in self.query(
                "SELECT value FROM samples "
                "WHERE resource_id = ? AND metric = ? AND ts >= ? ORDER BY ts ASC",
                (resource_id, metric, since),
            )
        ]

    def window_samples(self, since: float) -> dict[str, dict[str, list[tuple[float, float]]]]:
        """All samples newer than `since` as {resource: {metric: [(ts, value)]}}.

        One query instead of N-per-resource: the analysers all need the same
        window, so fetching it once per tick keeps the tick cheap. Timestamps
        are kept because windowed alert rules have to re-slice to their own,
        shorter window.
        """
        rows = self.query(
            "SELECT resource_id, metric, value, ts FROM samples WHERE ts >= ? ORDER BY ts ASC",
            (since,),
        )
        out: dict[str, dict[str, list[tuple[float, float]]]] = {}
        for r in rows:
            out.setdefault(r["resource_id"], {}).setdefault(r["metric"], []).append(
                (r["ts"], r["value"])
            )
        return out

    def window_values(self, since: float) -> dict[str, dict[str, list[float]]]:
        """window_samples with the timestamps dropped, for value-only analysers."""
        return {
            rid: {metric: [v for _, v in series] for metric, series in metrics.items()}
            for rid, metrics in self.window_samples(since).items()
        }

    def prune(self, older_than_ts: float) -> dict[str, int]:
        """Retention. Without this the volume grows without bound."""
        deleted = {}
        for table, column in (
            ("samples", "ts"),
            ("logs", "ts"),
            ("anomalies", "ts"),
            ("alert_history", "ts"),
        ):
            cur = self.execute(f"DELETE FROM {table} WHERE {column} < ?", (older_than_ts,))
            deleted[table] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self.execute(
            "DELETE FROM alerts WHERE status = 'resolved' AND resolved_at < ?",
            (older_than_ts,),
        )
        return deleted

    # ----------------------------------------------------------------- logs
    def insert_logs(self, rows: Iterable[tuple[float, str, str, str, str, dict]]) -> int:
        batch = [
            (ts, rid, svc, level, msg, json.dumps(ctx, default=str))
            for ts, rid, svc, level, msg, ctx in rows
        ]
        if not batch:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO logs (ts, resource_id, service, level, message, context) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )
            self._conn.commit()
        return len(batch)

    def search_logs(
        self,
        since: float,
        level: str | None = None,
        resource_id: str | None = None,
        contains: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = ["SELECT * FROM logs WHERE ts >= ?"]
        params: list[Any] = [since]
        if level:
            sql.append("AND level = ?")
            params.append(level.upper())
        if resource_id:
            sql.append("AND resource_id = ?")
            params.append(resource_id)
        if contains:
            sql.append("AND message LIKE ?")
            params.append(f"%{contains}%")
        sql.append("ORDER BY ts DESC LIMIT ?")
        params.append(min(limit, 1000))
        rows = self.query(" ".join(sql), params)
        for r in rows:
            try:
                r["context"] = json.loads(r["context"])
            except (TypeError, ValueError):
                r["context"] = {}
        return rows

    def log_counts_by_level(self, since: float) -> dict[str, int]:
        rows = self.query(
            "SELECT level, COUNT(*) AS n FROM logs WHERE ts >= ? GROUP BY level",
            (since,),
        )
        return {r["level"]: r["n"] for r in rows}

    # ------------------------------------------------------------ anomalies
    def insert_anomaly(self, record: dict) -> None:
        self.execute(
            "INSERT INTO anomalies "
            "(ts, resource_id, metric, value, baseline, deviation, score, method, severity, direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["ts"],
                record["resource_id"],
                record["metric"],
                record["value"],
                record["baseline"],
                record["deviation"],
                record["score"],
                record["method"],
                record["severity"],
                record["direction"],
            ),
        )

    def recent_anomalies(self, since: float, limit: int = 100) -> list[dict]:
        return self.query(
            "SELECT * FROM anomalies WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (since, limit),
        )

    def anomaly_exists_recently(self, resource_id: str, metric: str, since: float) -> bool:
        row = self.query_one(
            "SELECT 1 AS hit FROM anomalies "
            "WHERE resource_id = ? AND metric = ? AND ts >= ? LIMIT 1",
            (resource_id, metric, since),
        )
        return row is not None

    # --------------------------------------------------------------- alerts
    def upsert_alert(self, alert: dict) -> None:
        self.execute(
            """
            INSERT INTO alerts (fingerprint, rule, resource_id, metric, severity, status,
                                value, threshold, summary, description, runbook,
                                first_seen, started_at, last_seen, resolved_at)
            VALUES (:fingerprint, :rule, :resource_id, :metric, :severity, :status,
                    :value, :threshold, :summary, :description, :runbook,
                    :first_seen, :started_at, :last_seen, NULL)
            ON CONFLICT(fingerprint) DO UPDATE SET
                status      = excluded.status,
                severity    = excluded.severity,
                value       = excluded.value,
                summary     = excluded.summary,
                started_at  = COALESCE(alerts.started_at, excluded.started_at),
                last_seen   = excluded.last_seen,
                resolved_at = NULL
            """,
            alert,
        )

    def get_alert(self, fingerprint: str) -> dict | None:
        return self.query_one("SELECT * FROM alerts WHERE fingerprint = ?", (fingerprint,))

    def list_alerts(self, status: str | None = None, limit: int = 200) -> list[dict]:
        if status:
            return self.query(
                "SELECT * FROM alerts WHERE status = ? "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
                "last_seen DESC LIMIT ?",
                (status, limit),
            )
        return self.query("SELECT * FROM alerts ORDER BY last_seen DESC LIMIT ?", (limit,))

    def resolve_stale_alerts(self, cutoff: float, now: float) -> list[dict]:
        stale = self.query(
            "SELECT * FROM alerts WHERE status IN ('firing','pending') AND last_seen < ?",
            (cutoff,),
        )
        for alert in stale:
            self.execute(
                "UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE fingerprint = ?",
                (now, alert["fingerprint"]),
            )
        return stale

    def record_alert_event(self, alert: dict, event: str, ts: float) -> None:
        self.execute(
            "INSERT INTO alert_history (ts, fingerprint, rule, resource_id, severity, event, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                alert["fingerprint"],
                alert["rule"],
                alert["resource_id"],
                alert["severity"],
                event,
                alert.get("value", 0.0),
            ),
        )

    def acknowledge_alert(self, fingerprint: str, who: str, ts: float) -> bool:
        cur = self.execute(
            "UPDATE alerts SET acked_at = ?, acked_by = ? WHERE fingerprint = ?",
            (ts, who, fingerprint),
        )
        return bool(cur.rowcount)

    def alert_history(self, since: float, limit: int = 100) -> list[dict]:
        return self.query(
            "SELECT * FROM alert_history WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (since, limit),
        )

    # ------------------------------------------------------------ incidents
    def insert_incident(self, incident: dict) -> None:
        self.execute(
            "INSERT INTO incidents (id, scenario, resource_id, started_at, ends_at, "
            "status, magnitude, params, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                incident["id"],
                incident["scenario"],
                incident["resource_id"],
                incident["started_at"],
                incident["ends_at"],
                incident["status"],
                incident.get("magnitude", 1.0),
                json.dumps(incident.get("params", {})),
                incident.get("note", ""),
            ),
        )

    def active_incidents(self, now: float) -> list[dict]:
        rows = self.query(
            "SELECT * FROM incidents WHERE status = 'active' AND ends_at > ? "
            "ORDER BY started_at DESC",
            (now,),
        )
        return [self._decode_incident(r) for r in rows]

    def list_incidents(self, limit: int = 50) -> list[dict]:
        rows = self.query("SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,))
        return [self._decode_incident(r) for r in rows]

    def expire_incidents(self, now: float) -> int:
        cur = self.execute(
            "UPDATE incidents SET status = 'completed' " "WHERE status = 'active' AND ends_at <= ?",
            (now,),
        )
        return cur.rowcount or 0

    def cancel_incident(self, incident_id: str) -> bool:
        cur = self.execute(
            "UPDATE incidents SET status = 'cancelled' WHERE id = ? AND status = 'active'",
            (incident_id,),
        )
        return bool(cur.rowcount)

    def cancel_all_incidents(self) -> int:
        cur = self.execute("UPDATE incidents SET status = 'cancelled' WHERE status = 'active'")
        return cur.rowcount or 0

    @staticmethod
    def _decode_incident(row: dict) -> dict:
        try:
            row["params"] = json.loads(row["params"])
        except (TypeError, ValueError):
            row["params"] = {}
        return row

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        def count(table: str) -> int:
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            return int(row["n"]) if row else 0

        oldest = self.query_one("SELECT MIN(ts) AS t FROM samples")
        newest = self.query_one("SELECT MAX(ts) AS t FROM samples")
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "samples": count("samples"),
            "logs": count("logs"),
            "anomalies": count("anomalies"),
            "alerts": count("alerts"),
            "incidents": count("incidents"),
            "oldest_sample_ts": (oldest or {}).get("t"),
            "newest_sample_ts": (newest or {}).get("t"),
            "db_size_bytes": size,
            "now": time.time(),
        }
