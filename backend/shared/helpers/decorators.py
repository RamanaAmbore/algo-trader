import functools
import inspect
import logging
import threading
import time
from functools import wraps
from inspect import iscoroutinefunction

from backend.shared.helpers.auth_error import is_auth_error_str
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def singleton_init_guard(init_func):
    @wraps(init_func)
    def wrapper(self, *args, **kwargs):
        if getattr(self, '_singleton_initialized', False):
            logger.debug(f"Instance for {self.__class__.__name__} already initialized.")
            return
        init_func(self, *args, **kwargs)
        self._singleton_initialized = True

    return wrapper


def retry_kite_conn(max_attempts):
    """
    Decorator to retry a function on failure.

    `max_attempts` accepts either an `int` (frozen at decoration time —
    legacy behaviour) or a zero-arg callable (looked up on every call,
    so live `connections.retry_count` changes from /admin/settings take
    effect on the next attempt without a restart).

    If the decorated function declares a `test_conn` parameter in its
    signature, the decorator will set `test_conn=True` starting from the
    second attempt.
    """

    def decorator(func):
        sig = inspect.signature(func)
        has_test_conn = "test_conn" in sig.parameters

        @wraps(func)
        def wrapper(*args, **kwargs):
            n = max_attempts() if callable(max_attempts) else max_attempts
            for attempt in range(n):
                try:
                    # Only from 2nd attempt onwards, add/overwrite test_conn
                    if attempt >= 1 and has_test_conn:
                        kwargs["test_conn"] = True

                    return func(*args, **kwargs)

                except Exception as e:
                    logger.debug(
                        f"{func.__name__}: Attempt {attempt + 1} of {n} failed: {e}..."
                    )
                    if attempt == n - 1:
                        logger.error(
                            f"{func.__name__}: Operation failed after {n} attempts."
                        )
                        raise
                    # Exponential backoff between retries
                    is_rate_limit = "too many" in str(e).lower() or "429" in str(e)
                    delay = 30 if is_rate_limit else min(2 ** attempt, 30)
                    logger.debug(f"{func.__name__}: waiting {delay}s before retry {attempt + 2}")
                    time.sleep(delay)

        return wrapper

    return decorator


def track_it():
    def decorator(func):
        if iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                start_time = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    return result
                except Exception as e:
                    raise e
                finally:
                    elapsed = time.perf_counter() - start_time
                    logger.info(f"Async function {func.__name__} executed in {elapsed:.4f} seconds")

            return async_wrapper

        else:
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                start_time = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    return result
                except Exception as e:
                    raise e
                finally:
                    elapsed = time.perf_counter() - start_time
                    logger.info(f"Function {func.__name__} executed in {elapsed:.4f} seconds")

            return sync_wrapper

    return decorator


def lock_it_for_update(method):
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


def update_lock(method):
    """
    Decorator that ensures method execution is thread-safe using global and per-element locks.
    The element key is assumed to be the first positional argument.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        key = args[0] if args else None  # get key if passed

        with self.lock:
            if key:
                if key not in self.element_locks:
                    self.element_locks[key] = threading.Lock()
                lock = self.element_locks[key]
            else:
                lock = self.lock

        with lock:
            return method(self, *args, **kwargs)

    return wrapper


def for_all_accounts(func):
    """
    Iterate over every configured broker account and invoke `func`
    with the account-scoped handles injected as kwargs.

    The wrapped function gets three kwargs to choose from:

      * `broker` — a `backend.brokers.Broker` adapter. Prefer
        this in new code; it's vendor-agnostic and keeps callers
        from importing KiteConnect SDK directly.
      * `kite`   — the underlying KiteConnect SDK handle. Legacy,
        kept for backwards compat with callers that still use
        `kite.place_order(...)` etc. New code should reach for
        `broker` instead.
      * `account` — the RamboQuant account code (string).

    Adding a new broker ⇒ implement `Broker` under
    `backend/brokers/<vendor>.py`, register it in
    `registry.py`, and callers that use `broker=` keep working
    without change.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Call once with defaults → this gives us connections object
        bound_func = func.__wrapped__ if hasattr(func, "__wrapped__") else func

        # Use inspect to get defaults but don’t override
        import inspect
        sig = inspect.signature(bound_func)
        # Only pass `broker=...` into functions that accept it (either as
        # a named param or via **kwargs). Existing functions that were
        # written before the Broker abstraction landed accept only
        # `kite=...` and would TypeError otherwise.
        accepts_broker = (
            "broker" in sig.parameters
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        )
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()

        connections = bound.arguments["connections"]()
        account = bound.arguments.get("account", None)
        conn = bound.arguments.get("conn", None)
        results = []

        from backend.brokers import get_broker

        # Resolve the per-account `kite` kwarg lazily. For Kite accounts
        # this returns the live KiteConnect SDK handle (with test_conn
        # triggering a re-login if the token is stale). For Dhan / Groww
        # accounts the connection object lacks `get_kite_conn` — in that
        # case we pass None so callers that USE `kite` skip cleanly
        # while callers that USE `broker` keep working via the registry.
        def _kite_or_none(acc):
            conn_obj = connections.conn[acc]
            getter = getattr(conn_obj, "get_kite_conn", None)
            if getter is None:
                return None
            try:
                return getter(test_conn=True)
            except Exception:
                return None

        # Case 1: Single account
        if account:
            if not conn:
                kwargs["kite"] = _kite_or_none(account)
                if accepts_broker:
                    kwargs["broker"] = get_broker(account)
                result = func(*args, **kwargs)
                results.append(result)
            return results

        # Case 2: All accounts → run func concurrently across accounts.
        # Each `func` call is a blocking broker HTTP round-trip (~300-
        # 600 ms via Kite). Serial was costing N × per_account_latency
        # on every cached miss; the ThreadPoolExecutor fans the N
        # calls out simultaneously so wall-clock latency stays flat
        # as accounts are added. Results preserve the connections.conn
        # iteration order for any caller that downstream-joins by
        # position.
        accs = list(connections.conn.keys())
        if len(accs) <= 1:
            # Single account on the box — skip the pool overhead.
            for acc in accs:
                new_kwargs = kwargs.copy()
                new_kwargs["account"] = acc
                new_kwargs["kite"] = _kite_or_none(acc)
                if accepts_broker:
                    new_kwargs["broker"] = get_broker(acc)
                results.append(func(*args, **new_kwargs))
            return results

        def _try_renew(acc: str, connections) -> dict:
            """Attempt to refresh the broker token for `acc` and return fresh
            handle kwargs.  Uses lazy imports inside each branch to avoid the
            connections ↔ decorators circular-import (connections.py already
            imports from decorators.py via retry_kite_conn).

            Returns a dict with the refreshed handle key(s) that should be
            merged into the call kwargs before the retry.  An empty dict means
            renewal was either not possible or failed — caller should re-raise.
            """
            conn_obj = connections.conn.get(acc)
            if conn_obj is None:
                return {}
            try:
                from backend.brokers.connections import KiteConnection
                if isinstance(conn_obj, KiteConnection):
                    logger.info("[TOKEN-RENEW] %s (kite): auth error — renewing token", acc)
                    new_kite = conn_obj.get_kite_conn(test_conn=True)
                    if new_kite is not None:
                        return {"kite": new_kite}
                    return {}
            except Exception as _ke:
                logger.warning("[TOKEN-RENEW] %s (kite): renewal failed: %s", acc, _ke)
            try:
                from backend.brokers.connections import DhanConnection
                if isinstance(conn_obj, DhanConnection):
                    logger.info("[TOKEN-RENEW] %s (dhan): auth error — renewing token", acc)
                    conn_obj.get_dhan_conn(test_conn=True)
                    # Dhan's broker handle is obtained via get_broker — rebuilding
                    # the broker object after the token refresh picks up the new
                    # _dhan client that was populated inside get_dhan_conn.
                    new_broker = get_broker(acc)
                    if new_broker is not None:
                        return {"broker": new_broker}
                    return {}
            except Exception as _de:
                logger.warning("[TOKEN-RENEW] %s (dhan): renewal failed: %s", acc, _de)
            try:
                from backend.brokers.connections import GrowwConnection
                if isinstance(conn_obj, GrowwConnection):
                    logger.info("[TOKEN-RENEW] %s (groww): auth error — renewing token", acc)
                    conn_obj.refresh()
                    new_broker = get_broker(acc)
                    if new_broker is not None:
                        return {"broker": new_broker}
            except Exception as _ge:
                logger.warning("[TOKEN-RENEW] %s (groww): renewal failed: %s", acc, _ge)
            return {}

        def _per_account(acc):
            new_kwargs = kwargs.copy()
            new_kwargs["account"] = acc
            new_kwargs["kite"] = _kite_or_none(acc)
            if accepts_broker:
                new_kwargs["broker"] = get_broker(acc)
            try:
                return func(*args, **new_kwargs)
            except Exception as exc:
                if is_auth_error_str(str(exc)):
                    fresh = _try_renew(acc, connections)
                    if fresh:
                        retry_kwargs = {**new_kwargs, **fresh}
                        return func(*args, **retry_kwargs)  # raises on 2nd failure → propagates
                raise

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(len(accs), 2)) as pool:
            # executor.map preserves input order so the returned list
            # lines up with accs[] — important for any caller that
            # later pd.concat()s the results.
            results = list(pool.map(_per_account, accs))
        return results

    return wrapper


def with_guard(fn):
    """Decorator: skip/drop concurrent invocations of an async function.

    If *fn* is already executing, the second call returns ``None`` immediately
    without queuing.  Correct for background tasks where a stale concurrent
    run wastes resources.  For API routes prefer ``@ssot_fetch(mode='coalesce')``
    so callers share the in-flight result instead of being silently dropped.

    Thread-safety note: the ``_running`` flag is a plain Python bool protected
    only by the GIL.  This is sufficient for asyncio tasks on a single-threaded
    event loop (the only consumer), but do NOT use this decorator on functions
    called from ``ThreadPoolExecutor`` workers.
    """
    _running = False

    @functools.wraps(fn)
    async def _guarded(*args, **kwargs):
        nonlocal _running
        if _running:
            logger.debug("%s: skipped — already in-flight", fn.__qualname__)
            return None
        _running = True
        try:
            return await fn(*args, **kwargs)
        finally:
            _running = False

    return _guarded


def debug_wrapper(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        logging.debug(f'{function.__name__} started')  # Log function start
        result = function(*args, **kwargs)
        logging.debug(f'{function.__name__} ended')  # Log function end
        return result

    return wrapper
