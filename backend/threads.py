"""Thread-pool helpers that carry the caller's context variables into worker threads.

A plain ``pool.submit`` runs the function in a thread whose context is empty, so anything kept in a
``contextvars.ContextVar`` (the request's crypto scope, from Phase B on) would be missing there.
``submit`` copies the caller's context and runs the function inside it. Phase B's ``backend/crypto.py``
re-exports ``submit`` and ``run_in_context`` from here instead of defining its own.
"""
from __future__ import annotations

import contextvars
from concurrent.futures import Executor, Future
from typing import Any, Callable


def run_in_context(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``fn`` wrapped so it runs in a copy of the context of the thread that called this."""
    ctx = contextvars.copy_context()
    return lambda *args, **kwargs: ctx.run(fn, *args, **kwargs)


def submit(pool: Executor, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """``pool.submit(fn, ...)`` with the caller's context. One copy per call: a context can only be entered once at a time."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)
