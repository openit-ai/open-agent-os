"""Bounded, non-blocking background queue for profile/archive work."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_MAXSIZE = 256
_queue: asyncio.Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] | None = None
_worker: asyncio.Task[Any] | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _ensure_loop(loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
    """Keep asyncio primitives bound to the current service event loop."""
    global _queue, _worker, _loop
    if _loop is not loop:
        if _worker is not None and not _worker.done():
            _worker.cancel()
        _queue = asyncio.Queue(maxsize=_MAXSIZE)
        _worker = None
        _loop = loop
    return _queue


def _get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_MAXSIZE)
    return _queue


async def _run() -> None:
    queue = _get_queue()
    while True:
        fn, args, kwargs = await queue.get()
        try:
            # File/DB compatibility functions are synchronous in the current
            # codebase; isolate them from the event loop. Async jobs remain
            # awaitable without a thread hop.
            if asyncio.iscoroutinefunction(fn):
                await fn(*args, **kwargs)
            else:
                await asyncio.to_thread(fn, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("profile background job failed")
        finally:
            queue.task_done()


def enqueue(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    """Enqueue without waiting; returns False only when queue is full."""
    global _worker
    try:
        loop = asyncio.get_running_loop()
        queue = _ensure_loop(loop)
        if _worker is None or _worker.done():
            _worker = loop.create_task(_run(), name="adaptive-profile-worker")
        queue.put_nowait((fn, args, kwargs))
        return True
    except (asyncio.QueueFull, RuntimeError):
        return False


def reset_for_tests() -> None:
    global _queue, _worker, _loop
    if _worker is not None and not _worker.done():
        _worker.cancel()
    _worker = None
    _queue = None
    _loop = None
