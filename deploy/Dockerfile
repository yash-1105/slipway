# Build context is the repository root, so config/ and prompts/ are reachable.
FROM python:3.11-slim AS base

# uv, pinned. A floating installer is a build that changes under you.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so a source change does not reinstall the world.
COPY orchestrator/pyproject.toml orchestrator/uv.lock ./
RUN uv venv /opt/venv && uv sync --frozen --no-dev --no-install-project

COPY orchestrator/app ./app
COPY orchestrator/migrations ./migrations

RUN useradd --create-home --uid 10001 slipway \
    && mkdir -p /var/lib/slipway/artifacts \
    && chown -R slipway /var/lib/slipway /app
USER slipway

CMD ["python", "-m", "app.main"]
