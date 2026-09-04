# CloudOps Sentinel control plane
# ---------------------------------------------------------------------------
# Multi-stage build. The builder installs dependencies into a virtualenv; the
# runtime stage copies only that venv and the source. Build tools, pip caches
# and package indexes never reach the shipped image, which keeps it small and
# removes an entire class of things an attacker could use if they got a shell.

# ----------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Dependencies are copied and installed BEFORE the application source, so that
# editing a .py file reuses the cached dependency layer instead of reinstalling
# everything. This is the single biggest lever on rebuild time in CI.
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ----------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="CloudOps Sentinel control plane" \
      org.opencontainers.image.description="Cloud monitoring, reliability and cost optimisation control plane" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/example/cloudops-sentinel"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    CONFIG_DIR=/app/config \
    DATA_DIR=/data

# A dedicated unprivileged account. Running as root inside a container means a
# container-escape bug is a root-on-host bug; a fixed non-zero UID also lets the
# Kubernetes securityContext pin runAsUser to a known value.
RUN groupadd --system --gid 10001 cloudops \
 && useradd --system --uid 10001 --gid cloudops --no-create-home --shell /usr/sbin/nologin cloudops

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=cloudops:cloudops app ./app

# /data holds the SQLite database. It is declared as a volume so the container
# filesystem itself can be mounted read-only in production.
RUN mkdir -p /data && chown -R cloudops:cloudops /data
VOLUME ["/data"]

USER 10001:10001

EXPOSE 8000

# Health check uses the liveness probe, not the API, and stays inside the image
# (no curl needed). --start-period covers the first-boot backfill so a slow
# start is not mistaken for a broken container.
HEALTHCHECK --interval=15s --timeout=4s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

# Exec form: uvicorn becomes PID 1 and receives SIGTERM directly, so the
# lifespan shutdown hook runs and the collector stops cleanly. The shell form
# would leave /bin/sh as PID 1 swallowing the signal until Docker's grace
# period expired and killed the process outright.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
