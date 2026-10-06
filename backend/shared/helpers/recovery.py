"""Retry-then-escalate policy for operations that can recover on their own.

A failing call is retried. Each failed attempt is logged as a WARNING. When
every attempt fails, the failed call is logged as a WARNING while failures
stay below ESCALATE_AFTER within WINDOW_S, and as an ERROR after that. The
exception is re-raised, so callers behave as before.

An ERROR from this decorator is marked ``alert_now`` (it has already been
escalated), so the alert handler sends it without waiting for more repeats.

Use it on operations that can recover on retry, such as broker REST calls,
feed fetches, and database writes::

    @recoverable("kite.orders", attempts=3)
    def fetch_orders(): ...
"""
import asyncio
import functools
import logging
import threading
import time
from collections import deque

from backend.shared.helpers.ramboq_logger import get_logger

WINDOW_S = 900
ESCALATE_AFTER = 3

_lock = threading.Lock()
_failed_calls: dict[str, deque] = {}


def _record_failed_call(name: str, now: float) -> int:
    """Record one failed call for `name` and return how many fall in the window."""
    with _lock:
        hits = _failed_calls.setdefault(name, deque())
        hits.append(now)
        while hits and now - hits[0] > WINDOW_S:
            hits.popleft()
        return len(hits)


def _reset_for_tests() -> None:
    with _lock:
        _failed_calls.clear()


def _log_failed_call(logger, name: str, attempts: int, exc: Exception, count: int) -> None:
    if count > ESCALATE_AFTER:
        logger.error(
            f"{name}: failed after {attempts} attempts ({count} failed calls in "
            f"{WINDOW_S // 60} min): {exc}",
            extra={"alert_now": True},
        )
    else:
        logger.warning(
            f"{name}: failed after {attempts} attempts ({count} of "
            f"{ESCALATE_AFTER} before error): {exc}"
        )
    try:
        exc._recovery_logged = True
    except Exception:
        pass


def already_logged(exc: BaseException) -> bool:
    """True when a `recoverable` wrapper has already logged this exception."""
    return bool(getattr(exc, "_recovery_logged", False))


def recoverable(name: str, attempts: int = 3, backoff_s: float = 1.0,
                retry_on: tuple = (Exception,)):
    """Retry the wrapped call, logging a warning per failed attempt, then escalate."""

    def decorator(fn):
        logger = get_logger(fn.__module__)

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                for attempt in range(1, attempts + 1):
                    try:
                        return await fn(*args, **kwargs)
                    except retry_on as exc:
                        if attempt < attempts:
                            logger.warning(f"{name}: attempt {attempt}/{attempts} failed, retrying: {exc}")
                            await asyncio.sleep(backoff_s * attempt)
                        else:
                            count = _record_failed_call(name, time.monotonic())
                            _log_failed_call(logger, name, attempts, exc, count)
                            raise
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except retry_on as exc:
                    if attempt < attempts:
                        logger.warning(f"{name}: attempt {attempt}/{attempts} failed, retrying: {exc}")
                        time.sleep(backoff_s * attempt)
                    else:
                        count = _record_failed_call(name, time.monotonic())
                        _log_failed_call(logger, name, attempts, exc, count)
                        raise
        return wrapper

    return decorator
