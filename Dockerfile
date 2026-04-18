FROM python:3.12-slim AS base

WORKDIR /app

# Runtime deps: bash for entrypoint, curl for HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends bash curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Layer 1 — resolve + cache third-party deps without building the project
# itself. ``--no-install-project`` skips the magpie package so we can defer
# that to after src/ is copied, while still hitting the layer cache for
# everything on the wire.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2 — application code. Source changes only invalidate from here down.
COPY src/ src/
COPY configs/ configs/
COPY alembic/ alembic/
COPY alembic.ini ./alembic.ini
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Layer 3 — install magpie itself. Small + cheap now that deps are cached.
RUN uv sync --frozen --no-dev

# Layer 4 — Playwright chromium + system deps. Runs after ``uv sync`` so
# ``uv run`` doesn't try to re-sync against a half-built project.
RUN uv run playwright install chromium --with-deps

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
