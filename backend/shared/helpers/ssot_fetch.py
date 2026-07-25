"""
ssot_fetch — in-flight deduplication / serialisation decorator.

mode='coalesce'  : concurrent callers for the same key share one in-flight
                   Task (async) or threading.Event (sync).  Zero wasted work.
                   Non-None results are cached: sequential callers reuse the
                   last result until force_refresh=True.  None (void) returns
                   are never cached so side-effect functions run every call.
mode='serialize' : callers queue one at a time; each gets a fresh result.
mode='parallel'  : no restriction (default; equivalent to no decorator).

key param: None → use positional args as key tuple str
           callable → key(*args, **kwargs) → str
           str constant → same key for all calls

force_refresh kwarg (injected into every wrapped function):
           force_refresh=True → evict cached result and re-run the function.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from functools import wraps
from typing import Any, Callable, Literal

_SENTINEL = object()


def ssot_fetch(
    mode: Literal["coalesce", "serialize", "parallel"] = "parallel",
    key: Callable[..., str] | str | None = None,
) -> Callable:
    def _make_key(args: tuple, kwargs: dict) -> str:
        if key is None:
            return str(args)
        if callable(key):
            return key(*args, **kwargs)
        return key

    def decorator(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):
            # ── async paths ───────────────────────────────────────────────
            # _inflight: key → running Task (cleared when task completes)
            # _result_cache: key → cached result value (kept across calls)
            _inflight: dict[str, asyncio.Task] = {}
            _result_cache: dict[str, Any] = {}
            _ser_locks: dict[str, asyncio.Lock] = {}

            @wraps(fn)
            async def async_wrapper(*args: Any, force_refresh: bool = False, **kwargs: Any) -> Any:
                if mode == "parallel":
                    return await fn(*args, **kwargs)

                k = _make_key(args, kwargs)

                if mode == "coalesce":
                    if force_refresh:
                        # Evict both in-flight task and cached result.
                        _inflight.pop(k, None)
                        _result_cache.pop(k, None)
                    elif k in _result_cache:
                        # Sequential fast-path: return cached result.
                        return _result_cache[k]
                    elif k in _inflight:
                        # Concurrent fast-path: join the in-flight task.
                        return await _inflight[k]

                    def _on_done(task: asyncio.Task) -> None:
                        _inflight.pop(k, None)
                        if not task.cancelled() and task.exception() is None:
                            result = task.result()
                            if result is not None:
                                _result_cache[k] = result

                    task = asyncio.create_task(fn(*args, **kwargs))
                    _inflight[k] = task
                    task.add_done_callback(_on_done)
                    return await task

                # serialize
                if k not in _ser_locks:
                    _ser_locks[k] = asyncio.Lock()
                async with _ser_locks[k]:
                    return await fn(*args, **kwargs)

            return async_wrapper

        else:
            # ── sync / threading paths ────────────────────────────────────
            # _map_lock:     guards both _inflight and _result_cache mutations.
            # _inflight:     key → (Event, slot) while a call is running.
            #                Cleared after completion (event.set() + pop).
            # _result_cache: key → cached result value, kept across calls.
            # _ser_locks_sync: key → threading.Lock for serialize mode.
            _map_lock = threading.Lock()
            _inflight: dict[str, tuple[threading.Event, list]] = {}
            _result_cache: dict[str, Any] = {}
            _ser_locks_sync: dict[str, threading.Lock] = {}

            @wraps(fn)
            def sync_wrapper(*args: Any, force_refresh: bool = False, **kwargs: Any) -> Any:
                if mode == "parallel":
                    return fn(*args, **kwargs)

                k = _make_key(args, kwargs)

                if mode == "coalesce":
                    with _map_lock:
                        if force_refresh:
                            # Evict cached result; let any in-flight call finish
                            # (waiters on the old event still get the old result).
                            _result_cache.pop(k, None)
                            _inflight.pop(k, None)
                            is_first = True
                            ev = threading.Event()
                            slot: list = []
                            _inflight[k] = (ev, slot)
                        elif k in _result_cache:
                            # Sequential fast-path.
                            return _result_cache[k]
                        elif k in _inflight:
                            # Concurrent: join the in-flight call.
                            ev, slot = _inflight[k]
                            is_first = False
                        else:
                            # First caller.
                            ev = threading.Event()
                            slot = []
                            _inflight[k] = (ev, slot)
                            is_first = True

                    if not is_first:
                        ev.wait()
                        if slot and slot[0] is _SENTINEL:
                            raise slot[1]
                        return slot[0]

                    try:
                        val = fn(*args, **kwargs)
                        slot.append(val)
                        if val is not None:
                            with _map_lock:
                                _result_cache[k] = val
                        return val
                    except Exception as exc:
                        slot.extend([_SENTINEL, exc])
                        raise
                    finally:
                        ev.set()
                        with _map_lock:
                            _inflight.pop(k, None)

                # serialize
                with _map_lock:
                    if k not in _ser_locks_sync:
                        _ser_locks_sync[k] = threading.Lock()
                    lock = _ser_locks_sync[k]
                with lock:
                    return fn(*args, **kwargs)

            return sync_wrapper

    return decorator
