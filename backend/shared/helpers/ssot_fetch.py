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
            # 2026-09-27 audit fix: bumped by the exposed `_invalidate(key)`
            # hook (an EXTERNAL cache-clear, e.g. broker_apis.py's
            # _raw_cache_invalidate, distinct from this decorator's own
            # force_refresh param) so an already-in-flight task that
            # started before the invalidation can't write its now-stale
            # result back into _result_cache after the fact.
            _generation: dict[str, int] = {}

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

                    gen_before = _generation.get(k, 0)

                    def _on_done(task: asyncio.Task) -> None:
                        # 2026-09-27 audit fix: a force_refresh call racing
                        # this same task's completion can have already
                        # replaced _inflight[k] with a NEWER task (see the
                        # force_refresh branch above, which pops+recreates
                        # without cancelling whatever was already running).
                        # This callback must only clear/overwrite shared
                        # state if IT is still the current task for this
                        # key — otherwise a slow, now-superseded task can
                        # (a) pop the newer task's _inflight entry out from
                        # under it, breaking coalescing for any caller that
                        # arrives in between, and (b) write ITS OWN stale
                        # result into _result_cache after the newer,
                        # fresher fetch has already started, which a
                        # sequential caller could then read instead of
                        # waiting for the fresh one. The generation check
                        # covers the SEPARATE case of an external
                        # `_invalidate(key)` call (no new task created —
                        # just a cache clear) landing while this task was
                        # still running.
                        is_current = _inflight.get(k) is task
                        if is_current:
                            _inflight.pop(k, None)
                        if (is_current and not task.cancelled() and task.exception() is None
                                and _generation.get(k, 0) == gen_before):
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

            def _invalidate(key: str) -> None:
                """External cache-clear hook (e.g. broker_apis.py's
                _raw_cache_invalidate) — distinct from this decorator's own
                force_refresh param. Bumps the generation counter so an
                already-in-flight call for `key` can't write its
                now-stale result back after this invalidation."""
                _generation[key] = _generation.get(key, 0) + 1
                _result_cache.pop(key, None)

            async_wrapper._result_cache = _result_cache
            async_wrapper._invalidate = _invalidate
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
            # 2026-09-27 audit fix — same rationale as the async path above.
            _generation: dict[str, int] = {}

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

                    gen_before = _generation.get(k, 0)
                    try:
                        val = fn(*args, **kwargs)
                        slot.append(val)
                        if val is not None:
                            with _map_lock:
                                # 2026-09-27 audit fix: only cache if THIS
                                # call's (ev, slot) entry is still the
                                # current one for this key (a concurrent
                                # force_refresh call pops+replaces
                                # _inflight[k] with a NEW event without
                                # cancelling this call) AND no external
                                # `_invalidate(key)` call (e.g.
                                # broker_apis.py's _raw_cache_invalidate)
                                # landed while this call was in flight —
                                # either case means this call's result is
                                # stale relative to something that has
                                # already superseded it.
                                cur = _inflight.get(k)
                                if (cur is not None and cur[0] is ev
                                        and _generation.get(k, 0) == gen_before):
                                    _result_cache[k] = val
                        return val
                    except Exception as exc:
                        slot.extend([_SENTINEL, exc])
                        raise
                    finally:
                        ev.set()
                        with _map_lock:
                            # Same identity check as above — never pop a
                            # newer, still-legitimate in-flight entry that
                            # a racing force_refresh call installed.
                            cur = _inflight.get(k)
                            if cur is not None and cur[0] is ev:
                                _inflight.pop(k, None)

                # serialize
                with _map_lock:
                    if k not in _ser_locks_sync:
                        _ser_locks_sync[k] = threading.Lock()
                    lock = _ser_locks_sync[k]
                with lock:
                    return fn(*args, **kwargs)

            def _invalidate_sync(key: str) -> None:
                """External cache-clear hook — see async _invalidate's
                docstring above for the full rationale."""
                with _map_lock:
                    _generation[key] = _generation.get(key, 0) + 1
                    _result_cache.pop(key, None)

            sync_wrapper._result_cache = _result_cache
            sync_wrapper._invalidate = _invalidate_sync
            return sync_wrapper

    return decorator
