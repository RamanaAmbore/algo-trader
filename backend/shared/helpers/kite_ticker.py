"""
TickerManager — single WebSocket connection per Kite account that
streams live LTP ticks into an in-memory map. Read by
/api/quotes/sparkline and any future endpoint that needs tick freshness
without polling broker.ltp() on every request.

Threading model
---------------
KiteTicker.connect(threaded=True) spawns Twisted's reactor in a daemon
thread. All KiteTicker callbacks (_on_connect, _on_ticks, _on_close,
_on_error, _on_reconnect) fire on that reactor thread. Writes to
_tick_map and _subscribed are guarded by a threading.Lock. Reads from
asyncio handlers go through get_ltp() / get_ltp_batch() which take the
lock briefly — no async deadlock risk because the lock is non-reentrant
and the hold time is O(1) dict-read/write.

Lifecycle
---------
  start(api_key, access_token)
      Instantiate KiteTicker, register callbacks, call
      kws.connect(threaded=True). Idempotent — subsequent calls are
      no-ops.

  subscribe(tokens)
      Add instrument tokens to the live subscription. If the socket is
      already connected, subscribes immediately and sets MODE_LTP.
      If called before on_connect fires, queues tokens in _pending so
      the on_connect handler flushes them.

  get_ltp(token) → float | None
      Return the latest streamed last_price for a single token, or None
      when the token has never been seen (ticker not connected, not yet
      subscribed, or market closed).

  get_ltp_batch(tokens) → dict[int, float]
      Same for a collection; silently omits missing tokens so callers
      never receive None values in the result dict.

  status() → dict
      Snapshot for /api/admin/health: started, connected,
      subscribed_count, ticks_held.

  stop()
      Graceful close. Called from app on_shutdown.

IPv6 note
---------
KiteTicker uses Twisted's WebSocket transport, NOT requests/urllib3, so
the _IPv6SourceAdapter from connections.py does not apply. On the server
each Kite account has a specific whitelisted IPv6. If the ticker socket
fails with "Insufficient permission" in production, we would need to
bind the Twisted TCP factory's source address — but that requires
monkey-patching Twisted's endpoint creation, which is non-trivial.

For Phase 1 we defer that concern: if the WebSocket cannot connect due
to IP restrictions, get_ltp() returns None and the sparkline endpoint
falls back to broker.ltp() transparently. The design is safe to deploy
now and the deeper Twisted patch can follow if the connectivity issue
actually manifests on prod.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Iterable

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── BroadcastBus ──────────────────────────────────────────────────────────────

class BroadcastBus:
    """
    Thread-safe fan-out from the Twisted reactor thread to N asyncio Queues.

    Pattern:
      • One BroadcastBus is shared by the TickerManager singleton.
      • set_loop() is called once at app startup with the running event loop.
      • SSE route handlers call register() on connect, unregister() on
        disconnect.
      • _on_ticks calls bus.publish() for each tick frame. publish() uses
        loop.call_soon_threadsafe() to schedule a put_nowait on every
        registered queue without blocking the Twisted reactor.

    Backpressure: slow consumers whose queue is full silently drop the tick
    (put_nowait raises QueueFull which is caught and discarded). One missed
    tick does not break the SSE stream — the client was just reading too
    slowly and will catch up on the next tick.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Call once at startup from the asyncio event loop thread."""
        self._loop = loop

    def register(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._queues.add(queue)

    def unregister(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(queue)

    def publish(self, payload: dict) -> None:
        """
        Called from the Twisted reactor thread.

        Schedules a put_nowait on every registered asyncio.Queue via the
        main event loop. Uses call_soon_threadsafe so the call is safe to
        invoke from any thread. QueueFull and closed-loop errors are
        swallowed silently to never block the Twisted hot path.
        """
        if not self._loop:
            return
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                self._loop.call_soon_threadsafe(self._put_nowait, q, payload)
            except RuntimeError:
                # Event loop is closed — app shutting down; ignore.
                pass

    @staticmethod
    def _put_nowait(q: asyncio.Queue, payload: dict) -> None:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # slow consumer — drop tick, stream will catch up


class TickerManager:
    """
    Singleton-safe wrapper around KiteTicker. Owns the WebSocket
    lifecycle and exposes a lock-guarded in-memory tick map that
    asyncio route handlers can read without blocking the event loop.
    """

    def __init__(self) -> None:
        self._kws = None                          # KiteTicker instance
        self._tick_map: dict[int, float] = {}     # token → last_price
        self._token_to_sym: dict[int, str] = {}   # token → tradingsymbol (for SSE payload)
        self._sym_to_token: dict[str, int] = {}   # tradingsymbol (upper) → token (for O(1) has_sym)
        self._lock = threading.Lock()
        self._subscribed: set[int] = set()        # tokens live on the socket
        self._pending: set[int] = set()           # tokens queued pre-connect
        self._connected: bool = False
        self._started: bool = False
        self._bus = BroadcastBus()                # fan-out to SSE clients
        # Failover state — used by the watchdog task (background.py)
        self._current_account: str = ""           # account this ticker is currently bound to
        self._last_connected_at: float = 0.0      # unix ts, set in _on_connect
        self._last_disconnected_at: float = 0.0   # unix ts, set in _on_close
        # account → unix ts when last failover-aborted that account.
        # The watchdog skips an account for 5 min after a failed attempt
        # so we never bounce between two simultaneously-broken Kite accounts.
        self._failover_cooloff: dict[str, float] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def start(self, api_key: str, access_token: str, account: str = "") -> None:
        """
        Connect to wss://ws.kite.trade for the given Kite credentials.

        Idempotent — if already started (even after a reconnect cycle)
        this is a no-op. The Twisted reactor's built-in reconnect logic
        handles drops; we only ever call connect() once.
        """
        if self._started:
            return
        self._started = True
        self._current_account = account or self._current_account
        try:
            from kiteconnect import KiteTicker
            self._kws = KiteTicker(api_key, access_token)
            self._kws.on_connect   = self._on_connect
            self._kws.on_ticks     = self._on_ticks
            self._kws.on_close     = self._on_close
            self._kws.on_error     = self._on_error
            self._kws.on_reconnect = self._on_reconnect
            # threaded=True runs Twisted's reactor in a daemon thread so
            # the asyncio event loop is never blocked.
            self._kws.connect(threaded=True)
            logger.info(f"KiteTicker: connect() initiated (account={self._current_account or '?'})")
        except Exception:
            logger.exception("KiteTicker: connect() failed — ticker disabled")
            self._started = False

    @property
    def bus(self) -> BroadcastBus:
        """The SSE broadcast bus — SSE route handlers register their queues here."""
        return self._bus

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Wire the asyncio event loop so the bus can use call_soon_threadsafe."""
        self._bus.set_loop(loop)

    def subscribe_with_sym(self, token_sym_pairs: Iterable[tuple[int, str]]) -> None:
        """
        Like subscribe() but also records the tradingsymbol for each token so
        the SSE tick payload can include `sym` without a reverse-lookup table
        on the client side.

        Also maintains _sym_to_token (upper-cased sym → token) so has_sym()
        can do an O(1) membership check instead of iterating _token_to_sym values.
        """
        pairs = [(int(t), sym) for t, sym in token_sym_pairs]
        with self._lock:
            for tok, sym in pairs:
                self._token_to_sym[tok] = sym
                self._sym_to_token[sym.upper()] = tok
        self.subscribe(tok for tok, _ in pairs)

    def has_sym(self, sym: str) -> bool:
        """Return True if `sym` (case-insensitive) is already subscribed.

        O(1) lookup via the inverted _sym_to_token dict maintained by
        subscribe_with_sym(). Lock-guarded for thread safety.
        """
        with self._lock:
            return sym.upper() in self._sym_to_token

    def snapshot(self) -> dict[int, dict]:
        """
        Return a snapshot of all currently-held ticks as
        {token: {ltp, sym}} for the SSE initial-snapshot event.
        """
        with self._lock:
            return {
                tok: {"ltp": lp, "sym": self._token_to_sym.get(tok, "")}
                for tok, lp in self._tick_map.items()
            }

    def subscribe(self, tokens: Iterable[int]) -> None:
        """
        Register instrument tokens for MODE_LTP streaming.

        Tokens already subscribed are skipped (idempotent). New tokens
        are either sent to the live socket immediately (when connected)
        or queued in _pending for the on_connect flush.
        """
        new = {int(t) for t in tokens} - self._subscribed
        if not new:
            return
        with self._lock:
            if self._connected and self._kws is not None:
                try:
                    token_list = list(new)
                    self._kws.subscribe(token_list)
                    self._kws.set_mode(self._kws.MODE_LTP, token_list)
                    self._subscribed |= new
                    logger.info(
                        f"KiteTicker: subscribed +{len(new)} tokens "
                        f"(total={len(self._subscribed)})"
                    )
                except Exception:
                    logger.exception("KiteTicker: subscribe() failed")
            else:
                self._pending |= new

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        """Remove tokens from the live subscription."""
        drop = {int(t) for t in tokens} & self._subscribed
        if not drop:
            return
        with self._lock:
            if self._connected and self._kws is not None:
                try:
                    self._kws.unsubscribe(list(drop))
                    self._subscribed -= drop
                except Exception:
                    logger.exception("KiteTicker: unsubscribe() failed")

    def get_ltp(self, token: int) -> float | None:
        """
        Return the latest streamed last_price for one token, or None
        when the token has not yet been seen (not subscribed, or market
        closed, or ticker not connected).
        """
        with self._lock:
            return self._tick_map.get(int(token))

    def get_ltp_batch(self, tokens: Iterable[int]) -> dict[int, float]:
        """
        Return {token: ltp} for each token that has a live tick.
        Missing tokens are omitted silently — callers must handle that
        by falling back to broker.ltp().
        """
        with self._lock:
            return {int(t): self._tick_map[int(t)]
                    for t in tokens if int(t) in self._tick_map}

    def status(self) -> dict:
        """
        Snapshot for /api/admin/health — non-blocking, always returns.
        """
        with self._lock:
            return {
                "started":          self._started,
                "connected":        self._connected,
                "subscribed_count": len(self._subscribed),
                "ticks_held":       len(self._tick_map),
            }

    def stop(self) -> None:
        """Graceful shutdown — called from app on_shutdown.

        Sequence is important so Kite sees a clean disconnect and
        doesn't hold the previous session active when the new process
        attempts to reconnect:
          1. stop_retry() — kill the auto-reconnect loop FIRST. Without
             this, the moment we close the socket the Twisted reactor
             would immediately try to dial back in.
          2. close() — send the WebSocket CLOSE frame to Kite so the
             server-side session ends cleanly (rather than waiting on
             its TCP keep-alive timeout to detect a dead client).
          3. ticker.stop() — stop the Twisted reactor so the daemon
             thread can exit. The library exposes this on the
             KiteTicker instance itself (different from this wrapper's
             .stop()).
          4. Brief sleep so the close frame actually leaves the box
             before the process exits.
        """
        import time
        kws = self._kws
        if kws is not None:
            for step in ("stop_retry", "close"):
                fn = getattr(kws, step, None)
                if fn is not None:
                    try:
                        fn()
                    except Exception:
                        logger.exception(f"KiteTicker: {step}() failed during shutdown")
            # Stop the Twisted reactor (different from THIS wrapper's stop).
            try:
                kws_stop = getattr(kws, "stop", None)
                if kws_stop is not None:
                    kws_stop()
            except Exception:
                logger.exception("KiteTicker: ticker.stop() failed during shutdown")
            # Brief grace so the CLOSE frame actually leaves the box.
            try:
                time.sleep(0.5)
            except Exception:
                pass
        self._started   = False
        # CRITICAL: synchronise _last_disconnected_at when this method
        # transitions us out of the connected state. Earlier, stop()
        # set _connected=False WITHOUT touching _last_disconnected_at;
        # when restart_with_account() called stop()→start() but the
        # new start failed to actually connect (network blip, token
        # invalidation), _on_close was never called and the disconnect
        # timestamp stayed at its previous value. seconds_since_disconnect()
        # then reported wildly stale durations (200,000+ s — i.e. 55 h
        # past the LAST genuine _on_close). The watchdog read those as
        # "disconnected forever" and kept failover-thrashing between
        # accounts every 5 minutes (the failover cool-off window).
        if self._connected:
            self._last_disconnected_at = time.time()
        self._connected = False
        self._kws       = None
        logger.info("KiteTicker: stopped (clean)")

    def ensure_started(self, api_key: str, access_token: str, account: str = "") -> bool:
        """Idempotent re-attempt of start() — safe to call from later
        startup phases (e.g. the sparkline-warm task) when the
        access_token wasn't yet available during on_startup. Returns
        True if the ticker is now started (either freshly or already).
        """
        if self._started:
            return True
        if not api_key or not access_token:
            return False
        self.start(api_key, access_token, account=account)
        return self._started

    # ── Failover support ──────────────────────────────────────────────────

    def current_account(self) -> str:
        """Which Kite account this ticker is currently bound to."""
        return self._current_account

    def seconds_since_connect(self) -> float:
        """0 if never connected; else elapsed since last connect."""
        import time
        with self._lock:
            return (time.time() - self._last_connected_at) if self._last_connected_at else 0.0

    def seconds_since_disconnect(self) -> float:
        """0 if currently connected OR never disconnected; else elapsed since last close."""
        import time
        with self._lock:
            if self._connected:
                return 0.0
            return (time.time() - self._last_disconnected_at) if self._last_disconnected_at else 0.0

    def is_account_in_failover_cooloff(self, account: str, cool_seconds: float = 300.0) -> bool:
        """True when this account failed over recently — watchdog will
        skip it for `cool_seconds` so we don't bounce between two
        simultaneously-broken Kite accounts."""
        import time
        with self._lock:
            ts = self._failover_cooloff.get(account, 0.0)
            return ts > 0 and (time.time() - ts) < cool_seconds

    def restart_with_account(
        self, api_key: str, access_token: str, account: str
    ) -> bool:
        """
        Tear down the current ticker + start fresh against a different
        Kite account. Previously-subscribed tokens are re-subscribed on
        the new connection (queued in _pending until on_connect fires).

        Used by the watchdog when the primary account's WebSocket has
        been disconnected longer than the failover threshold.
        """
        import time
        prev_account = self._current_account
        prev_subs = set(self._subscribed) | set(self._pending)
        # Mark the failing account so the watchdog doesn't try it again
        # immediately (5-minute do-not-retry default).
        if prev_account:
            with self._lock:
                self._failover_cooloff[prev_account] = time.time()
        logger.warning(
            f"KiteTicker: failover {prev_account or '?'} → {account} "
            f"(re-subscribing {len(prev_subs)} token(s))"
        )
        # Clean shutdown of the old socket (idempotent if already closed).
        self.stop()
        # Re-init state (stop() resets _started; reuse __init__-style defaults).
        self._subscribed = set()
        self._pending = set(prev_subs)
        # _tick_map intentionally preserved — operator's UI keeps showing
        # the last known LTPs until fresh ticks roll in from the new account.
        self.start(api_key, access_token, account=account)
        return self._started

    # ── Callbacks (fire on the Twisted reactor thread) ────────────────────

    def _on_connect(self, ws, _response) -> None:
        import time
        with self._lock:
            self._connected = True
            self._last_connected_at = time.time()
            # Defensive: reset the stale disconnect timestamp so
            # seconds_since_disconnect() can never report a value
            # from before the current connection. Combined with the
            # stop()-side fix above, this guarantees the watchdog's
            # disconnect math always reflects the CURRENT connection's
            # lifecycle, not a 55-hour-old ghost.
            self._last_disconnected_at = 0.0
            pending = set(self._pending)
            self._pending.clear()

        logger.info(
            f"KiteTicker: connected — flushing {len(pending)} pending "
            f"subscription(s)"
        )
        if pending:
            try:
                token_list = list(pending)
                ws.subscribe(token_list)
                ws.set_mode(ws.MODE_LTP, token_list)
                with self._lock:
                    self._subscribed |= pending
                logger.info(
                    f"KiteTicker: flushed {len(pending)} pending tokens "
                    f"(total={len(self._subscribed)})"
                )
            except Exception:
                logger.exception("KiteTicker: pending flush failed")

    def _on_ticks(self, _ws, ticks) -> None:
        """
        Hot path — fires on every WebSocket tick frame.

        Merges the incoming last_price values into _tick_map under the
        lock, then publishes each tick to the BroadcastBus so SSE
        clients receive near-real-time updates.

        The lock hold-time is proportional to len(ticks), which for
        MODE_LTP frames is a flat list of {instrument_token, last_price}
        dicts — typically 20-200 entries per frame at 5 req/sec cadence.
        Bus.publish() is called outside the lock to minimise hold time —
        it acquires its own internal lock briefly to snapshot the queue
        set.
        """
        to_publish: list[dict] = []
        ts = int(time.time())
        with self._lock:
            for t in ticks:
                tok = t.get("instrument_token")
                lp  = t.get("last_price")
                if tok is not None and lp is not None:
                    tok = int(tok)
                    lp  = float(lp)
                    self._tick_map[tok] = lp
                    to_publish.append({
                        "tok": tok,
                        "sym": self._token_to_sym.get(tok, ""),
                        "ltp": lp,
                        "ts":  ts,
                    })
        # Publish outside the lock so the Twisted reactor is not held
        # while the bus iterates its queue set.
        for payload in to_publish:
            self._bus.publish(payload)

    def _on_close(self, _ws, code, reason) -> None:
        import time
        with self._lock:
            self._connected = False
            self._last_disconnected_at = time.time()
        logger.warning(
            f"KiteTicker: closed — code={code} reason={reason!r} "
            f"account={self._current_account or '?'}"
        )

    def _on_error(self, _ws, code, reason) -> None:
        logger.error(f"KiteTicker: error — code={code} reason={reason!r}")

    def _on_reconnect(self, _ws, attempts_count) -> None:
        logger.warning(f"KiteTicker: reconnecting — attempt {attempts_count}")


# ── Module-level singleton ────────────────────────────────────────────────────

_ticker = TickerManager()


def get_ticker() -> TickerManager:
    """Return the process-wide TickerManager singleton."""
    return _ticker
