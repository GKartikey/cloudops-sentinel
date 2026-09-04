"""Demo service runtime: real resource measurement, a log buffer, and chaos.

The measurement here is genuine. CPU and memory are read from the container's
own cgroup, which is the same source `docker stats` and the kubelet use, and the
percentages are expressed against the container's *limit* rather than the host's
capacity - so a service capped at 0.5 CPU reports 100% when it saturates its
half-core, exactly as Kubernetes would see it. That distinction is the whole
reason a container can be throttled to a standstill on a host that looks idle.

Layout differences between cgroup v2 (modern), cgroup v1 (older Docker) and no
cgroup at all (running the service directly on a laptop for tests) are handled
by falling back in that order.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

CGROUP_V2 = Path("/sys/fs/cgroup/cpu.stat")
CGROUP_V1_CPU = Path("/sys/fs/cgroup/cpu/cpuacct.usage")


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
class ResourceProbe:
    """CPU and memory utilisation of THIS container, as a percentage of limit."""

    def __init__(self) -> None:
        self._last_cpu_ns: float | None = None
        self._last_ts: float | None = None
        self._cpu_percent = 0.0
        self.cpu_limit = self._detect_cpu_limit()
        self.memory_limit_bytes = self._detect_memory_limit()
        self.source = self._detect_source()

    # ------------------------------------------------------------- discovery
    @staticmethod
    def _detect_source() -> str:
        if CGROUP_V2.exists():
            return "cgroup_v2"
        if CGROUP_V1_CPU.exists():
            return "cgroup_v1"
        return "proc"

    @staticmethod
    def _detect_cpu_limit() -> float:
        # cgroup v2: "<quota|max> <period>" in microseconds.
        raw = _read("/sys/fs/cgroup/cpu.max")
        if raw:
            parts = raw.split()
            if len(parts) == 2 and parts[0] != "max":
                try:
                    return float(parts[0]) / float(parts[1])
                except (ValueError, ZeroDivisionError):
                    pass
        # cgroup v1: quota and period in separate files.
        quota = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota and period:
            try:
                q, p = float(quota), float(period)
                if q > 0 and p > 0:
                    return q / p
            except ValueError:
                pass
        return float(os.cpu_count() or 1)

    @staticmethod
    def _detect_memory_limit() -> float:
        for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            raw = _read(path)
            if raw and raw != "max":
                try:
                    value = float(raw)
                    # cgroup v1 reports an absurd sentinel when unlimited.
                    if 0 < value < (1 << 62):
                        return value
                except ValueError:
                    continue
        meminfo = _read("/proc/meminfo")
        if meminfo:
            for line in meminfo.splitlines():
                if line.startswith("MemTotal:"):
                    try:
                        return float(line.split()[1]) * 1024
                    except (ValueError, IndexError):
                        break
        return 512 * 1024 * 1024

    # ------------------------------------------------------------ sampling
    def _cpu_usage_ns(self) -> float | None:
        raw = _read("/sys/fs/cgroup/cpu.stat")
        if raw:
            for line in raw.splitlines():
                if line.startswith("usage_usec"):
                    try:
                        return float(line.split()[1]) * 1000.0
                    except (ValueError, IndexError):
                        break
        raw = _read(CGROUP_V1_CPU)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        # Fallback: this process only, from /proc/self/stat clock ticks.
        stat = _read("/proc/self/stat")
        if stat:
            try:
                fields = stat.rsplit(") ", 1)[1].split()
                ticks = float(fields[11]) + float(fields[12])  # utime + stime
                hz = os.sysconf("SC_CLK_TCK")
                return (ticks / hz) * 1e9
            except (ValueError, IndexError, OSError, AttributeError):
                pass
        return None

    def cpu_percent(self) -> float:
        now = time.monotonic()
        usage = self._cpu_usage_ns()
        if usage is None:
            return self._cpu_percent
        if self._last_cpu_ns is not None and self._last_ts is not None:
            elapsed = now - self._last_ts
            if elapsed > 0.05:
                delta_seconds = (usage - self._last_cpu_ns) / 1e9
                pct = 100.0 * delta_seconds / (elapsed * max(self.cpu_limit, 0.01))
                # Smooth it: a single 200ms window is far too twitchy to alert on.
                self._cpu_percent = 0.6 * self._cpu_percent + 0.4 * max(0.0, min(pct, 100.0))
                self._last_cpu_ns, self._last_ts = usage, now
        else:
            self._last_cpu_ns, self._last_ts = usage, now
        return round(self._cpu_percent, 2)

    def memory_bytes(self) -> float:
        for path in (
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ):
            raw = _read(path)
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    continue
        status = _read("/proc/self/status")
        if status:
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    try:
                        return float(line.split()[1]) * 1024
                    except (ValueError, IndexError):
                        break
        return 0.0

    def memory_percent(self) -> float:
        used = self.memory_bytes()
        if self.memory_limit_bytes <= 0:
            return 0.0
        return round(min(100.0, 100.0 * used / self.memory_limit_bytes), 2)

    def info(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "cpu_limit_cores": round(self.cpu_limit, 3),
            "memory_limit_mb": round(self.memory_limit_bytes / (1024 * 1024), 1),
        }


# --------------------------------------------------------------------------
class LogBuffer:
    """Bounded in-memory ring of structured log records.

    The service writes every line to stdout as JSON (the real log pipeline) AND
    keeps the last N here so the control plane can pull them over HTTP without a
    log aggregator being deployed. A bounded deque is the important part: an
    unbounded buffer in a container with a memory limit is an OOM kill waiting
    for a busy afternoon.
    """

    def __init__(self, capacity: int = 500) -> None:
        self._entries: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, level: str, message: str, service: str, **context: Any) -> dict:
        entry = {
            "ts": time.time(),
            "level": level.upper(),
            "message": message,
            "service": service,
            "context": context,
        }
        with self._lock:
            self._entries.append(entry)
        # stdout is the contract; the buffer is the convenience.
        print(json.dumps(entry, default=str), flush=True)
        return entry

    def since(self, ts: float, limit: int = 200) -> list[dict]:
        with self._lock:
            return [e for e in self._entries if e["ts"] > ts][-limit:]


# --------------------------------------------------------------------------
class ChaosController:
    """Induces real failure in this container.

    State is persisted to a file inside the container's writable layer so that a
    crash-loop scenario survives the process exiting. A Docker restart policy
    restarts the same container, so the file is still there when the new process
    starts, reads it, and crashes again - producing a genuine restart loop that
    the collector discovers by watching the process start time change.
    """

    MODES = ("none", "cpu_burn", "memory_leak", "latency", "errors", "crash", "outage", "load")

    def __init__(self, state_path: Path, logs: LogBuffer, service: str) -> None:
        self.state_path = state_path
        self.logs = logs
        self.service = service
        self.mode = "none"
        self.until = 0.0
        self.intensity = 0.0
        self.restart_count = 0
        self._ballast: list[bytearray] = []
        self._burn_thread: threading.Thread | None = None
        self._burn_stop = threading.Event()
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        raw = _read(self.state_path)
        if not raw:
            return
        try:
            state = json.loads(raw)
        except ValueError:
            return
        self.restart_count = int(state.get("restart_count", 0))
        if float(state.get("until", 0)) > time.time():
            self.mode = state.get("mode", "none")
            self.until = float(state.get("until", 0))
            self.intensity = float(state.get("intensity", 0))
            self.logs.add(
                "WARN",
                f"resuming chaos mode {self.mode} after restart",
                self.service,
                mode=self.mode,
                restart_count=self.restart_count,
                remaining_seconds=round(self.until - time.time()),
            )

    def _save(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(
                    {
                        "mode": self.mode,
                        "until": self.until,
                        "intensity": self.intensity,
                        "restart_count": self.restart_count,
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            self.logs.add("WARN", "could not persist chaos state", self.service, error=str(exc))

    def record_start(self) -> None:
        """Called once at boot. A boot with existing state means a restart."""
        if _read(self.state_path) is not None:
            self.restart_count += 1
        self._save()

    # --------------------------------------------------------------- control
    def set(self, mode: str, duration_seconds: int, intensity: float) -> dict:
        if mode not in self.MODES:
            raise ValueError(f"unknown chaos mode: {mode}")
        self.clear_effects()
        if mode == "none":
            self.mode, self.until, self.intensity = "none", 0.0, 0.0
            self._save()
            self.logs.add("INFO", "chaos cleared", self.service)
            return self.status()

        self.mode = mode
        self.intensity = max(0.0, min(float(intensity), 1.0))
        self.until = time.time() + max(5, int(duration_seconds))
        self._save()
        self.logs.add(
            "WARN",
            f"chaos engaged: {mode}",
            self.service,
            mode=mode,
            intensity=self.intensity,
            duration_seconds=duration_seconds,
        )
        if mode == "cpu_burn":
            self._start_burn()
        return self.status()

    def clear_effects(self) -> None:
        self._burn_stop.set()
        thread = self._burn_thread
        # Never join from inside the burn thread itself. `active()` is the burn
        # loop's own condition and used to call this on expiry, which made the
        # thread try to join itself - RuntimeError, an ugly traceback in the
        # container logs, and the cleanup below silently skipped.
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
            self._burn_thread = None
        elif thread is not threading.current_thread():
            self._burn_thread = None
        self._ballast.clear()

    def _expired(self) -> bool:
        """Pure predicate - safe to call from any thread, mutates nothing."""
        return self.mode == "none" or time.time() >= self.until

    def active(self) -> bool:
        """Predicate with expiry side effects. Do not call from the burn thread."""
        if self.mode == "none":
            return False
        if time.time() >= self.until:
            self.logs.add("INFO", f"chaos mode {self.mode} expired", self.service)
            self.mode, self.until, self.intensity = "none", 0.0, 0.0
            self.clear_effects()
            self._save()
            return False
        return True

    def status(self) -> dict[str, Any]:
        active = self.active()
        return {
            "mode": self.mode if active else "none",
            "intensity": self.intensity if active else 0.0,
            "remaining_seconds": max(0, round(self.until - time.time())) if active else 0,
            "restart_count": self.restart_count,
            "ballast_mb": round(sum(len(b) for b in self._ballast) / (1024 * 1024), 1),
        }

    # --------------------------------------------------------------- effects
    def _start_burn(self) -> None:
        """Burn CPU with a duty cycle, in a thread.

        The GIL is not an obstacle: this is arithmetic in a tight loop, so the
        thread genuinely consumes the container's CPU quota. Duty cycling rather
        than spinning flat out is what lets `intensity` mean something.
        """
        self._burn_stop = threading.Event()

        def burn() -> None:
            # _expired(), not active(): active() mutates state and cleans up
            # threads, which must never happen from inside this thread.
            while not self._burn_stop.is_set() and not self._expired():
                slice_start = time.monotonic()
                busy_for = 0.05 * max(self.intensity, 0.05)
                x = 0.0
                while time.monotonic() - slice_start < busy_for:
                    x += 1.000001**2
                idle = max(0.0, 0.05 - busy_for)
                if idle:
                    time.sleep(idle)
            self._burn_stop.set()

        self._burn_thread = threading.Thread(target=burn, name="chaos-cpu-burn", daemon=True)
        self._burn_thread.start()

    def leak_memory(self) -> None:
        """Allocate on every request while a leak is active."""
        if not self.active() or self.mode != "memory_leak":
            return
        chunk = int(2 * 1024 * 1024 * max(self.intensity, 0.1))
        # Cap the leak so the demo degrades visibly instead of getting OOM-killed
        # instantly, which would look like a crash rather than a leak.
        if sum(len(b) for b in self._ballast) < 220 * 1024 * 1024:
            self._ballast.append(bytearray(chunk))

    def should_fail(self, roll: float) -> bool:
        return self.active() and self.mode == "errors" and roll < self.intensity

    def extra_latency_seconds(self) -> float:
        if not self.active() or self.mode != "latency":
            return 0.0
        return 0.15 + 2.5 * self.intensity

    def is_outage(self) -> bool:
        return self.active() and self.mode == "outage"

    def should_crash(self) -> bool:
        return self.active() and self.mode == "crash"

    def crash(self) -> None:
        # The count is NOT incremented here. Ownership sits with record_start(),
        # which runs on the next boot - so a crash that somehow fails to restart
        # is not counted as a restart that never happened.
        self._save()
        self.logs.add(
            "ERROR",
            "process exiting: simulated crash",
            self.service,
            restart_count=self.restart_count,
            exit_code=1,
        )
        # os._exit bypasses cleanup handlers on purpose: a real crash does not
        # get to run its shutdown hooks either, and the restart policy is what
        # we are demonstrating.
        os._exit(1)
