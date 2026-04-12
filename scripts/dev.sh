#!/usr/bin/env bash
set -euo pipefail

# Local dev startup for magpie-backend
echo "Starting magpie-backend development server..."
uv run uvicorn magpie.main:app --reload --port 8000
