# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    XDG_CACHE_HOME=/tmp/cache \
    DENO_DIR=/tmp/deno

WORKDIR /app

RUN useradd --create-home --uid 10001 --user-group app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN install -d -m 0700 -o app -g app /data

FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python

# Cache dependencies separately from the library's source code.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM base AS web-builder

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --only-group browser --no-install-project

FROM base AS web

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       chromium xvfb fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
COPY --from=web-builder /app/.venv /app/.venv
COPY src/ /app/src/
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src
USER app
HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]
ENTRYPOINT ["python", "-m", "telegram_stream.web"]

FROM builder AS development

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
RUN chown -R app:app /app

ENV PATH="/app/.venv/bin:$PATH"

USER app
ENTRYPOINT ["telegram-stream"]
CMD ["run"]

FROM base AS runtime

# The non-editable installation does not need the source tree or build tools.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

USER app
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD ["telegram-stream", "healthcheck"]
ENTRYPOINT ["telegram-stream"]
CMD ["run"]
