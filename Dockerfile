FROM python:3.12-slim AS base

WORKDIR /app

# Install uv + minimal runtime deps. bash is required by docker-entrypoint.sh.
RUN apt-get update && apt-get install -y --no-install-recommends bash \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Copy project files required for uv sync + runtime.
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src/ src/
COPY configs/ configs/
COPY alembic/ alembic/
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Install frozen dependencies (no dev group in production).
RUN uv sync --frozen --no-dev

# Bake commit SHA at build time for /version.
ARG COMMIT_SHA=unknown
ENV COMMIT_SHA=${COMMIT_SHA}

ENV APP_ENV=production
ENV PORT=8000

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "magpie.main:app", "--host", "0.0.0.0", "--port", "8000"]
