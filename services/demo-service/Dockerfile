# CloudOps Sentinel demo workload
# ---------------------------------------------------------------------------
# One image, three roles. SERVICE_ROLE at runtime decides whether this container
# behaves as the checkout API, the inventory API or the report worker - the same
# artefact promoted with different configuration, which is what you want in a
# real pipeline: the thing you tested is the thing you shipped.

FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="CloudOps Sentinel demo workload" \
      org.opencontainers.image.description="Instrumented demo service with real chaos injection"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    SERVICE_ROLE=api \
    PORT=8080 \
    CHAOS_STATE_PATH=/state/chaos.json

RUN groupadd --system --gid 10001 demo \
 && useradd --system --uid 10001 --gid demo --no-create-home --shell /usr/sbin/nologin demo

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=demo:demo app ./app

# Chaos state lives here so a crash-loop scenario survives the process exiting
# and the container being restarted by its restart policy.
RUN mkdir -p /state && chown -R demo:demo /state

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
