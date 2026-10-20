# ---------- Stage 1: build the virtualenv with uv ----------
FROM python:3.13-slim AS builder

# Take the uv binary from its official image instead of pip-installing it.
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

WORKDIR /app

# Compile .pyc at install time (faster startup) and copy files instead of
# hard-linking (the cache mount below is a different filesystem).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependencies first: this layer is only rebuilt when the lockfile changes,
# not on every code edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------- Stage 2: small runtime image ----------
FROM python:3.13-slim

# Unprivileged user so a compromised app can't act as root in the container.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

# Only the finished virtualenv comes across; uv and its cache stay behind.
COPY --from=builder /app/.venv /app/.venv
COPY supabase-root-2021-ca.crt ./
COPY app ./app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8000

# 0.0.0.0 so the port is reachable from outside the container.
CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]
