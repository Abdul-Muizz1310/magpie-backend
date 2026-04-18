"""Procrastinate-backed task queue.

Importing this package triggers registration of all tasks with the shared
``queue_app`` via ``import_paths=["magpie.queue.tasks"]``.
"""

from magpie.queue.app import queue_app

__all__ = ["queue_app"]
