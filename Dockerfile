FROM python:3.12-slim AS base

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy everything needed for install
COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY configs/ configs/

# Install dependencies (frozen from lock file)
RUN uv sync --frozen --no-dev

# Bake in the commit SHA at build time
ARG COMMIT_SHA=unknown
ENV COMMIT_SHA=${COMMIT_SHA}

# Default env
ENV APP_ENV=production
ENV PORT=8000

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "magpie.main:app", "--host", "0.0.0.0", "--port", "8000"]
