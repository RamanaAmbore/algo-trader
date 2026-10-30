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


# ── Repeat-failure dedup (2026-10-30) ────────────────────────────────────
#
# `recoverable()` above only escalation-gates exceptions matched by its own
# `retry_on` tuple — by design, a non-retryable exception (e.g. Kite's
# TokenException for a stale/not-yet-refreshed daily access_token) is never
# worth retrying, so it bypasses the decorator's except clause entirely and
# propagates straight to the caller. Several call sites (orders_helpers.py,
# orders_gtt.py) catch it themselves and log a bare `logger.error(...)` —
# live incident, 2026-10-30: while that day's Kite token hadn't refreshed
# yet, the `/api/orders` route's own 15s cache TTL meant the frontend's poll
# re-hit the broker every ~15-35s, and EVERY poll logged its own fresh,
# unsuppressed ERROR — one real condition (a stale token, self-resolving
# once the day's login completes) fanned out into 18 separate ERROR_AGENT
# alerts over a few minutes.
#
# `dedup_log_error()` is a narrow, opt-in gate for exactly this shape: a
# call site that already catches its own exception and wants "one alert
# per distinct failure, not one per repeat within a cooldown window" —
# deliberately NOT a change to `recoverable()` itself, which would alter
# behaviour for every one of its existing call sites (positions, holdings,
# margins, ltp, quote, ...) rather than just the two noisy ones this fixes.
_dedup_lock = threading.Lock()
_dedup_last_logged: dict[str, float] = {}
DEDUP_COOLDOWN_S = 300  # 5 minutes


def dedup_log_error(logger, key: str, message: str, cooldown_s: float = DEDUP_COOLDOWN_S,
                     **log_kwargs) -> bool:
    """Log `message` at ERROR, unless the same `key` already logged within
    `cooldown_s`seconds — then log it at DEBUG instead (so the record still
    exists for troubleshooting, but doesn't reach the alert pipeline again
    until the window clears). Returns True if it logged at ERROR, False if
    suppressed to DEBUG.

    `key` should identify the FAILURE CLASS (e.g. account + exception type),
    not the verbatim message — a message that embeds a changing detail
    (a timestamp, a request id) would otherwise defeat the dedup by never
    matching itself twice.
    """
    now = time.monotonic()
    with _dedup_lock:
        last = _dedup_last_logged.get(key)
        if last is not None and (now - last) < cooldown_s:
            logger.debug(f"[DEDUP] repeat suppressed (key={key}, cooldown={cooldown_s:.0f}s): {message}")
            return False
        _dedup_last_logged[key] = now
    logger.error(message, **log_kwargs)
    return True


def _reset_dedup_for_tests() -> None:
    with _dedup_lock:
        _dedup_last_logged.clear()


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
