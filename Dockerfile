FROM python:3.12-slim AS base

WORKDIR /app

# Runtime deps: bash for entrypoint, curl for HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends bash curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Layer 1 — resolve deps. Only invalidated when pyproject or lock changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# Layer 2 — install Playwright chromium (sources with ``render: true``
# require this, and the production image must carry it). Invalidated
# together with the deps layer since playwright ships with uv.
RUN uv run playwright install chromium --with-deps

# Layer 3 — application code. Source changes only invalidate this layer.
COPY src/ src/
COPY configs/ configs/
COPY alembic/ alembic/
COPY alembic.ini ./alembic.ini
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Bake commit SHA for /version.
ARG COMMIT_SHA=unknown
ENV COMMIT_SHA=${COMMIT_SHA}

ENV APP_ENV=production
ENV PORT=8000

# Non-root user for the running process. Playwright's browsers were
# installed under root above; copy them into the user's cache dir so the
# unprivileged runtime can launch chromium.
RUN useradd -m -u 1000 magpie \
    && mkdir -p /home/magpie/.cache \
    && cp -r /root/.cache/ms-playwright /home/magpie/.cache/ms-playwright \
    && chown -R magpie:magpie /app /home/magpie/.cache
USER magpie

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "magpie.main:app", "--host", "0.0.0.0", "--port", "8000"]
