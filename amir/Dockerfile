# Amir Beckett, as a container. Build the image where it runs (scripts/deploy.sh builds it on the
# server). Never bake in a secret: the token comes from DISCORD_TOKEN at run time, the config is
# mounted at /app/config/amir.toml.

FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app
# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# Then Amir himself, installed into the venv as a regular package (the source is not needed
# at run time).
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# /data is the volume for the database and the heartbeat. /app/data points at it, so the
# config's relative "data/..." paths and absolute "/data/..." paths are the same files.
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin amir \
    && mkdir -p /app/config /data \
    && chown amir:amir /data \
    && ln -s /data /app/data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY assets/greetings ./assets/greetings
COPY config/amir.example.toml ./config/amir.example.toml
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AMIR_CONFIG=/app/config/amir.toml
USER amir
VOLUME /data
# Healthy = the process is up and its Discord client is ready (heartbeat at most 120 s old).
# The heartbeat starts only after Discord's READY, hence the start period.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python","-m","amir.health"]
CMD ["python","-m","amir"]
