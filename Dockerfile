FROM python:3.14-slim-bookworm AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable --extra docker --no-install-project
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --extra docker

FROM base AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno
COPY --chmod=755 docker/start-vsummary.sh /usr/local/bin/start-vsummary

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DENO_DIR=/tmp/vsummary-deno \
    NOTEBOOKLM_HOME=/var/lib/vsummary/notebooklm \
    NOTEBOOKLM_PROFILE=default

USER 10001:10001
STOPSIGNAL SIGINT
CMD ["/usr/local/bin/start-vsummary"]
