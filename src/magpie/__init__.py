"""Top-level ``magpie`` package marker.

The CLI entrypoint lives at :mod:`magpie.cli` (see ``pyproject.toml``). We
deliberately do **not** re-export it at the package level — the name would
collide with the :mod:`magpie.main` FastAPI module.
"""
