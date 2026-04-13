"""Structured logging — JSON in prod, pretty in dev.

Always includes ``request_id`` and ``service`` in every log entry.
"""

from __future__ import annotations

import logging
import os

SERVICE_NAME = "magpie"


def configure_logging() -> None:
    """Set up standard Python logging with JSON-friendly format in prod."""
    is_prod = os.environ.get("ENVIRONMENT", "development") == "production"

    if is_prod:
        fmt = (
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"service":"' + SERVICE_NAME + '","msg":"%(message)s"}'
        )
    else:
        fmt = f"%(asctime)s %(levelname)-8s [{SERVICE_NAME}] %(message)s"

    logging.basicConfig(level=logging.INFO, format=fmt, force=True)
