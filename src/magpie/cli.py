"""``python -m magpie.cli`` — one-shot commands for CI and local ops.

Subcommands
-----------
* ``migrate`` — apply Alembic + Procrastinate schema. Idempotent. Used by
  the Docker entrypoint and local bootstrapping.
* ``run <source>`` — execute a single scrape synchronously against Postgres.
  Used by the nightly GitHub Action.
* ``run-all`` — execute every file-origin source in sequence.
* ``sync`` — mirror ``configs/*.yaml`` into the ``sources`` table (same
  routine the FastAPI lifespan runs at startup).

The CLI deliberately executes scrapes synchronously — not via the queue —
so CI and ad-hoc invocations don't depend on a running worker.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from magpie.lifespan import _sync_file_sources_to_db
from magpie.paths import configs_dir
from magpie.services.scrape_service import (
    ScrapeExecutionError,
    UnknownSourceError,
    scrape_once,
)
from magpie.storage.db import get_session_factory

log = logging.getLogger("magpie.cli")


# ── Subcommands ──────────────────────────────────────────────────────────────


def _migrate() -> int:
    """Apply Alembic head; Procrastinate schema is applied via its own CLI."""
    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        command.upgrade(cfg, "head")
    finally:
        os.chdir(cwd)
    return 0


async def _sync() -> int:
    count = await _sync_file_sources_to_db()
    print(f"synced {count} file-origin source(s)")
    return 0


async def _run_one(source: str, max_items: int) -> int:
    factory = get_session_factory()
    try:
        await _sync_file_sources_to_db()  # ensure the source exists in DB
        result = await scrape_once(
            source=source,
            max_items=max_items,
            session_factory=factory,
        )
    except UnknownSourceError as exc:
        print(f"error: unknown source {exc.source!r}", file=sys.stderr)
        return 2
    except ScrapeExecutionError as exc:
        print(f"error: scrape failed: {exc}", file=sys.stderr)
        return 1
    print(f"ok: source={result.source} run_id={result.run_id} items={len(result.items)}")
    return 0


async def _run_all(max_items: int) -> int:
    cfg_dir = configs_dir()
    if not cfg_dir.is_dir():
        print("no configs/ directory found", file=sys.stderr)
        return 1
    names = sorted(p.stem for p in cfg_dir.glob("*.yaml"))
    if not names:
        print("no YAML configs found")
        return 0
    exit_code = 0
    for name in names:
        rc = await _run_one(name, max_items)
        exit_code = exit_code or rc
    return exit_code


# ── argparse ─────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="magpie")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="Apply Alembic migrations (idempotent)")
    sub.add_parser("sync", help="Mirror configs/*.yaml into the sources table")

    run_parser = sub.add_parser("run", help="Run a single scrape synchronously")
    run_parser.add_argument("source", help="Source name (must be registered)")
    run_parser.add_argument("--max-items", type=int, default=50)

    run_all_parser = sub.add_parser("run-all", help="Run every registered source")
    run_all_parser.add_argument("--max-items", type=int, default=50)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "migrate":
        return _migrate()
    if args.command == "sync":
        return asyncio.run(_sync())
    if args.command == "run":
        return asyncio.run(_run_one(args.source, args.max_items))
    if args.command == "run-all":
        return asyncio.run(_run_all(args.max_items))

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
