"""Placeholder smoke tests — package imports + CLI entrypoint is callable."""

from __future__ import annotations

import pytest


def test_package_imports() -> None:
    import magpie  # noqa: F401


def test_cli_entrypoint_exits_with_usage() -> None:
    """``magpie`` CLI with no args prints help and argparse exits with code 2."""
    from magpie.cli import main as magpie_main

    with pytest.raises(SystemExit) as exc_info:
        magpie_main([])
    assert exc_info.value.code == 2
