# Kinesis API image. Build from the repo root:
#   docker build -f infra/docker/backend.Dockerfile -t kinesis-api .
FROM python:3.12-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock .python-version ./
COPY backend/pyproject.toml backend/pyproject.toml
COPY scripts/ scripts/
RUN mkdir -p backend/src/kinesis && touch backend/src/kinesis/__init__.py \
 && uv sync --frozen --no-dev --no-install-workspace --package kinesis

# Application code.
COPY backend/ backend/
RUN uv sync --frozen --no-dev --package kinesis

# Run as an unprivileged user; only /data is writable.
RUN useradd --system --uid 10001 --home-dir /nonexistent kinesis \
 && mkdir -p /data && chown kinesis /data
USER kinesis

ENV PATH="/app/.venv/bin:$PATH" \
    KINESIS_DATA_DIR=/data

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/health').status==200 else 1)"

CMD ["uvicorn", "kinesis.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
