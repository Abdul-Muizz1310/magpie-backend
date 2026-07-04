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
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from magpie.config.loader import load_config_from_file
from magpie.healer.detector import should_heal
from magpie.lifespan import _sync_file_sources_to_db
from magpie.paths import configs_dir
from magpie.scheduling import is_due
from magpie.services.scrape_service import (
    ScrapeExecutionError,
    UnknownSourceError,
    scrape_once,
)
from magpie.storage.db import get_session_factory
from magpie.storage.sources_repo import SourcesRepository

log = logging.getLogger("magpie.cli")

# Exit code for a scrape that succeeded (HTTP-wise) but returned fewer items than
# the source's ``health.min_items`` — a likely silent selector drift. Returning
# non-zero fails the nightly-scrape workflow leg, which triggers the
# ``heal-on-failure`` workflow to run the LLM self-heal (MAG-1).
EXIT_UNDERFLOW = 3


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

    # Underflow = likely silent selector drift. Fail loudly so CI escalates to
    # the healer instead of quietly shipping a near-empty scrape (MAG-1).
    async with factory() as session:
        config = await SourcesRepository(session).get_config(source)
    if should_heal(item_count=len(result.items), min_items=config.health.min_items):
        print(
            f"underflow: source={result.source} got {len(result.items)} items, "
            f"need >= {config.health.min_items} (min_items) — flagging for heal",
            file=sys.stderr,
        )
        return EXIT_UNDERFLOW
    return 0


def _due(*, window_seconds: int, run_all: bool) -> int:
    """Print the sources due to run now, honouring each config's ``schedule``.

    Emits a JSON object ``{"sources": [...], "js_sources": [...]}`` to stdout;
    when ``GITHUB_OUTPUT`` is set it also writes ``sources=`` / ``js_sources=``
    lines so the nightly-scrape workflow can build its matrix from the sources
    whose cron actually fires in the current window (not one global cadence).

    ``run_all`` bypasses the cron filter (used by manual ``workflow_dispatch``).
    """
    now = datetime.now(UTC)
    cfg_dir = configs_dir()
    configs = []
    if cfg_dir.is_dir():
        for path in sorted(cfg_dir.glob("*.yaml")):
            try:
                configs.append(load_config_from_file(path))
            except Exception:
                log.exception("skipping invalid config %s", path)
                continue

    due = configs if run_all else [c for c in configs if is_due(c.schedule, now, window_seconds)]

    sources = [c.name for c in due]
    js_sources = [c.name for c in due if c.render]

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            fh.write(f"sources={json.dumps(sources)}\n")
            fh.write(f"js_sources={json.dumps(js_sources)}\n")
    print(json.dumps({"sources": sources, "js_sources": js_sources}))
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

    due_parser = sub.add_parser(
        "due", help="Emit the sources due to run now (per-source cron schedule)"
    )
    due_parser.add_argument(
        "--window-seconds",
        type=int,
        default=3600,
        help="Look-back window; match the scheduler's cadence (default 3600 = hourly)",
    )
    due_parser.add_argument(
        "--all",
        dest="run_all",
        action="store_true",
        help="Ignore the cron filter and emit every source (manual dispatch)",
    )

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
    if args.command == "due":
        return _due(window_seconds=args.window_seconds, run_all=args.run_all)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
