# Multi-stage build for gigs_bot. Use uv for fast, deterministic installs.

# ── Stage 1: install dependencies ─────────────────────────────────────────────
FROM python:3.11-slim AS builder

# uv (Astral) — pulled from its official image, no network/installer needed.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# curl for HEALTHCHECK; ca-certificates for outbound TLS (Telegram, Google, OpenAI).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN groupadd -r app && useradd -r -g app -d /app -s /usr/sbin/nologin app

WORKDIR /app

# Bring in the prebuilt venv from the builder.
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# App code.
COPY --chown=app:app . /app

# Persistent data dir for SQLite (mount a volume to /data in production).
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "main.py"]
