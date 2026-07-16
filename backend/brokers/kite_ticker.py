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


def _emit_conn_event(
    account: str,
    broker_id: str,
    event_type: str,
    detail: dict | None = None,
) -> None:
    """Lazy-import shim so kite_ticker.py can emit connection events without
    a hard import on conn_events (which owns the DB session factory and
    must only be imported inside the conn_service process)."""
    try:
        # lazy import to avoid circular dependency — conn_events → event_queue → database
        from backend.brokers.service.conn_events import _emit_conn_event as _fire
        _fire(account, broker_id, event_type, detail)
    except Exception:
        pass


class TickerManager:
    """
    Singleton-safe wrapper around KiteTicker. Owns the WebSocket
    lifecycle and exposes a lock-guarded in-memory tick map that
    asyncio route handlers can read without blocking the event loop.
    """

    def __init__(self) -> None:
        self._kws = None                          # KiteTicker instance
        self._tick_map: dict[int, float] = {}     # token → last_price
        # Per-token last-tick wall-clock timestamp (unix seconds). Parallel
        # to _tick_map; updated atomically in _on_ticks. Used by /api/admin/health
        # to surface "which subscribed symbols have stale tick data" — distinguishes
        # market-closed (expected staleness) from subscribe-failure (unexpected).
        self._tick_age: dict[int, float] = {}     # token → unix ts of last tick
        # Optional shared-memory tick buffer — when set, every tick gets
        # mirrored to mmap so the main API process can read LTPs at
        # mmap-byte speed instead of paying a UDS round-trip per call.
        # Set via attach_tick_buffer() at construction in conn_service;
        # left None in the main API process (which has no writer role).
        self._tick_buffer = None                  # TickBufferWriter | None
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
        # Auto-failover state machine bookkeeping (June 2026).
        #
        # `_consecutive_unhealthy` counts how many watchdog cycles in a row
        # judged the CURRENT account unhealthy (via _is_active_ticker_healthy).
        # The watchdog swaps to the next failover account only after the
        # count crosses `unhealthy_threshold` — a single blip is not enough,
        # otherwise a 30 s network hiccup would burn a 5 min swap cool-off.
        # Reset to 0 on any healthy tick.
        self._consecutive_unhealthy: int = 0
        # Set True when stop() catches ReactorNotRunning — the Twisted
        # reactor stopped on its own and cannot be restarted in this process.
        # The watchdog reads this flag and exits so systemd (Restart=always)
        # spawns a fresh process with a clean reactor state.
        self._reactor_dead: bool = False
        # Rolling in-memory history of swap timestamps (unix seconds). The
        # `status()` payload derives `swaps_last_hour` from this list so
        # the health surface can distinguish "auto-failover fired once
        # today" from "we are ping-ponging between accounts". Bounded at
        # 128 entries; older ones dropped on append.
        self._swap_history: list[float] = []
        # Instant when this process's watchdog started supervising — used
        # by conn_service to defer failover during the boot grace period
        # so a swap can never fire while rebuild_from_db is still minting
        # Kite tokens.
        self._supervisor_started_at: float = 0.0
        # Operator-force-unhealthy deadline (unix ts). When set to a
        # future timestamp, `is_active_ticker_healthy()` returns False
        # regardless of actual WS state — the watchdog then progresses
        # through bump_unhealthy → threshold → swap. Auto-expires so a
        # forgotten force-unhealthy doesn't leave the ticker permanently
        # broken. Set via POST /internal/ticker/force-unhealthy.
        self._force_unhealthy_until: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────

    def attach_tick_buffer(self, buffer) -> None:
        """Attach a TickBufferWriter so every tick is mirrored to mmap.

        Called from conn_service startup. The main API process leaves
        this unset — its TickerManager dict view is local-only (and will
        be replaced by an mmap reader entirely in the consumer phase
        of slice 4).
        """
        self._tick_buffer = buffer

    def is_reactor_dead(self) -> bool:
        """True when the Twisted reactor stopped independently and this
        process can no longer host a KiteTicker WebSocket."""
        return self._reactor_dead

    def start(self, api_key: str, access_token: str, account: str = "") -> None:
        """
        Connect to wss://ws.kite.trade for the given Kite credentials.

        Idempotent — if already started (even after a reconnect cycle)
        this is a no-op. The Twisted reactor's built-in reconnect logic
        handles drops; we only ever call connect() once.
        """
        if self._reactor_dead:
            # Twisted singleton: reactor.run() will raise ReactorNotRestartable.
            # Don't attempt — the connect thread would die silently, leaving
            # _started=True but _connected=False forever. Let the watchdog exit.
            logger.critical(
                "KiteTicker: start() called but reactor is dead — skipping "
                "(watchdog should exit and let systemd restart the process)"
            )
            return
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

        Defensive: filters out any non-positive `lp` even though the
        post-fix `_on_ticks` zero-guard already prevents 0 from entering
        `_tick_map`. Belt + suspenders for the LTP-flicker fix — if a
        future code change re-introduces a 0 write path, the snapshot
        still won't propagate it to new SSE clients.
        """
        with self._lock:
            return {
                tok: {"ltp": lp, "sym": self._token_to_sym.get(tok, "")}
                for tok, lp in self._tick_map.items()
                if isinstance(lp, (int, float)) and lp > 0
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
                    # Prune _tick_age so unsubscribed tokens don't
                    # accumulate in memory indefinitely (monotonic
                    # growth in a long-running process).
                    for tok in drop:
                        self._tick_age.pop(tok, None)
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

    def get_ltp_by_sym(self, sym: str) -> float | None:
        """
        Return the latest streamed last_price for one tradingsymbol via
        the reverse `_sym_to_token` map. None when the sym hasn't been
        subscribed yet, hasn't ticked since open, or the ticker is down.
        Lets callers override stale Kite-REST last_price values without
        having to track instrument tokens themselves.
        """
        with self._lock:
            tok = self._sym_to_token.get(str(sym or '').upper())
            if tok is None:
                return None
            return self._tick_map.get(int(tok))

    def get_ltp_batch(self, tokens: Iterable[int]) -> dict[int, float]:
        """
        Return {token: ltp} for each token that has a live tick.
        Missing tokens are omitted silently — callers must handle that
        by falling back to broker.ltp().
        """
        with self._lock:
            return {int(t): self._tick_map[int(t)]
                    for t in tokens if int(t) in self._tick_map}

    def status(self, stale_threshold_sec: int = 60, stale_top_n: int = 20) -> dict:
        """
        Snapshot for /api/admin/health — non-blocking, always returns.

        `stale_threshold_sec` (default 60) defines what counts as a stale
        symbol — any subscribed token whose last tick is older than this
        is reported in `stale_count`. The `stale_top` list carries the
        N worst offenders (oldest first) as "SYMBOL@<age_seconds>s"
        strings so the operator can see WHICH symbols are missing ticks,
        not just how many.

        NB: subscribed-but-never-ticked tokens (e.g. a watchlist symbol
        added pre-open before any trade fires) ARE counted as stale once
        the threshold passes — they show up in `stale_top` as
        "SYMBOL@never". This distinguishes "subscribe call landed but
        Kite emitted no tick" from "subscribe call never landed".
        """
        now = time.time()
        # Snapshot the minimal state under the lock, then do all iteration
        # and formatting outside. _on_ticks acquires this same lock on every
        # tick frame (90+/sec); a 300-iteration loop inside the lock would
        # block tick ingestion for the duration of the /api/admin/health call.
        with self._lock:
            subscribed_copy = set(self._subscribed)
            tick_map_size = len(self._tick_map)
            started = self._started
            connected = self._connected
            age_snapshot = {tok: self._tick_age.get(tok) for tok in subscribed_copy}
            sym_snapshot = {tok: self._token_to_sym.get(tok, f"tok:{tok}") for tok in subscribed_copy}
        # Build the ages list outside the lock — no shared state read here.
        ages: list[tuple[str, float | None]] = []
        for tok in subscribed_copy:
            sym = sym_snapshot[tok]
            last_ts = age_snapshot[tok]
            age = (now - last_ts) if last_ts is not None else None
            ages.append((sym, age))
        # Sort: never-ticked first (None age), then oldest-tick descending.
        ages.sort(key=lambda x: (0, 0) if x[1] is None else (1, -x[1]))
        stale = [
            (sym, age) for sym, age in ages
            if age is None or age >= stale_threshold_sec
        ]
        # Compose top-N as printable "sym@age" strings — easier to read
        # from a JSON dump than a list of [sym, float] pairs.
        stale_top = [
            f"{sym}@{'never' if age is None else f'{int(age)}s'}"
            for sym, age in stale[:stale_top_n]
        ]
        # Max age across ANY token that has ever ticked. None-aged tokens
        # are reflected in stale_count, not here.
        max_age = max(
            (age for _, age in ages if age is not None),
            default=0.0,
        )
        # Failover state — safe to read outside the lock (small ints /
        # floats / lists all backed by Python's atomic assignment on
        # CPython; `list(...)` snapshot is defensive).
        with self._lock:
            active_account = self._current_account
            consecutive_unhealthy = self._consecutive_unhealthy
            swap_history_snap = list(self._swap_history)
        cutoff_1h = now - 3600.0
        swaps_last_hour = sum(1 for ts in swap_history_snap if ts >= cutoff_1h)
        last_swap = swap_history_snap[-1] if swap_history_snap else 0.0
        return {
            "started":          started,
            "connected":        connected,
            "subscribed_count": len(subscribed_copy),
            "ticks_held":       tick_map_size,
            "stale_count":      len(stale),
            "max_age_seconds":  float(max_age),
            "stale_top":        stale_top,
            # Failover surface — same keys published by conn_service
            # `/ticker/status` and mirrored into main API `/api/admin/health`.
            "active_account":       active_account,
            "consecutive_unhealthy": int(consecutive_unhealthy),
            "swaps_last_hour":       int(swaps_last_hour),
            "last_swap_at":          float(last_swap),
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
            except Exception as _stop_exc:
                _exc_name = type(_stop_exc).__name__
                if "ReactorNotRunning" in _exc_name or "ReactorNotRunning" in str(_stop_exc):
                    # Reactor already stopped on its own (network failure /
                    # Kite closed WS and Twisted's reconnect gave up).
                    # Twisted's reactor is a process-level singleton — once
                    # stopped, reactor.run() raises ReactorNotRestartable, so
                    # every future connect(threaded=True) will silently fail.
                    # Mark as dead so the watchdog can exit and let systemd
                    # (Restart=always) spawn a fresh process.
                    self._reactor_dead = True
                    logger.critical(
                        "KiteTicker: Twisted reactor stopped independently "
                        "(ReactorNotRunning) — process restart required for recovery. "
                        "Watchdog will trigger exit."
                    )
                else:
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

    def mark_supervisor_started(self) -> None:
        """Record when this process's watchdog began supervising.

        The conn_service watchdog reads `supervisor_uptime_seconds()`
        to gate swap decisions during boot — a Kite token that hasn't
        yet been minted by `rebuild_from_db()` should not trigger a
        failover on a cold start. Called once from the watchdog's
        first iteration.
        """
        if not self._supervisor_started_at:
            self._supervisor_started_at = time.time()

    def supervisor_uptime_seconds(self) -> float:
        """0 when the supervisor has never started; else elapsed seconds."""
        return (
            time.time() - self._supervisor_started_at
            if self._supervisor_started_at else 0.0
        )

    def is_active_ticker_healthy(self, tick_heartbeat_s: float = 60.0) -> bool:
        """Composite health check invoked once per watchdog cycle.

        Healthy when:
          • start() has been called (`_started`) AND
          • the WebSocket is open (`_connected`) AND
          • at least one tick has landed within the last `tick_heartbeat_s`
            (falls back to _last_connected_at when no tick has ever fired —
            covers the first-connect grace window) AND
          • no operator force-unhealthy window is active.

        Market-closed hours: the watchdog (Phase 2b in service/app.py)
        applies a market-hours gate BEFORE calling this helper, so
        during closed hours this helper is not invoked and its return
        value doesn't matter. When called during open hours only.
        """
        now = time.time()
        with self._lock:
            # Operator-forced unhealthy window (verification path). Trumps
            # every other signal; auto-clears past the deadline.
            if 0 < self._force_unhealthy_until and now < self._force_unhealthy_until:
                return False
            if not self._started or not self._connected:
                return False
            # Take the max of last tick + last connect so a freshly-
            # connected socket that hasn't seen its first tick yet
            # doesn't get flagged unhealthy immediately.
            newest_tick_ts = max(self._tick_age.values(), default=0.0)
            newest = max(newest_tick_ts, self._last_connected_at)
        if not newest:
            return False
        return (now - newest) <= tick_heartbeat_s

    def force_unhealthy(self, duration_s: float = 120.0) -> float:
        """Operator escape hatch — mark the ticker unhealthy for
        `duration_s` seconds so the watchdog progresses through
        bump_unhealthy → threshold → swap. Returns the deadline
        (unix ts). Called by POST /internal/ticker/force-unhealthy.

        Auto-clears past the deadline so a forgotten call doesn't
        permanently break the ticker.
        """
        deadline = time.time() + max(1.0, float(duration_s))
        with self._lock:
            self._force_unhealthy_until = deadline
        logger.warning(
            "KiteTicker: operator-forced unhealthy for %.0f s "
            "(deadline unix=%.0f) — watchdog will fire failover path",
            duration_s, deadline,
        )
        return deadline

    def clear_force_unhealthy(self) -> None:
        """Cancel an in-flight force_unhealthy window."""
        with self._lock:
            self._force_unhealthy_until = 0.0

    def bump_unhealthy(self) -> int:
        """Watchdog reports one more consecutive unhealthy cycle.
        Returns the current count. Reset via `reset_unhealthy()`."""
        with self._lock:
            self._consecutive_unhealthy += 1
            return self._consecutive_unhealthy

    def reset_unhealthy(self) -> None:
        """Watchdog: mark active account healthy this cycle. Zeroes the
        `_consecutive_unhealthy` counter so a subsequent blip has to
        cross `unhealthy_threshold` on its own."""
        with self._lock:
            self._consecutive_unhealthy = 0

    def record_swap(self, prev_account: str, next_account: str) -> None:
        """Append a swap event to `_swap_history`. Called from
        restart_with_account() so `swaps_last_hour` in status() reflects
        every failover cycle without an external tracker."""
        now = time.time()
        with self._lock:
            self._swap_history.append(now)
            # Cap growth — 128 swaps is 42 hours of ping-pong at the
            # 5-min cool-off floor. Well past the point where the
            # operator would have intervened.
            if len(self._swap_history) > 128:
                self._swap_history = self._swap_history[-128:]
        # Log-side signal — historian + tail-grep friendly.
        logger.info(
            "KiteTicker: recorded auto-failover swap %s → %s (total_swaps=%d)",
            prev_account or "?", next_account, len(self._swap_history),
        )

    def swaps_since(self, seconds: float) -> int:
        """Count swap events within the last `seconds` window. Watchdog
        uses this for its cooldown ("no swap allowed within N minutes
        of the last") — cheaper than tracking a separate `last_swap_at`
        and correct across restart_with_account failure/retry cycles.
        """
        cutoff = time.time() - seconds
        with self._lock:
            return sum(1 for ts in self._swap_history if ts >= cutoff)

    def last_swap_at(self) -> float:
        """0 when no swap has fired yet; else unix ts of most recent."""
        with self._lock:
            return self._swap_history[-1] if self._swap_history else 0.0

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

    def recycle(self) -> bool:
        """Hard-reset the ticker on the CURRENT account.

        Tears down the active WebSocket, wipes _tick_map + _tick_age so
        the in-memory state rebuilds from scratch, then re-subscribes
        every previously-known token. Different from restart_with_account
        in that the credentials don't change — this is for refreshing
        stale in-memory ticker state (operator's HARD refresh mode),
        not for handling a failover.

        Brief LTP gap (~2-3s reconnect + on_connect flush) but the
        frontend SSE clients auto-reconnect and grids serve cached
        values during the gap, so functionality continues uninterrupted.
        """
        import time
        prev_account = self._current_account
        if not prev_account:
            logger.warning("KiteTicker: recycle() called with no current account — skipping")
            return False
        prev_subs = set(self._subscribed) | set(self._pending)
        # Resolve credentials before stopping so we can reconnect against
        # the same account in one move.
        #
        # Invariant: recycle() is only called on a Kite-bound ticker so
        # `conn` is always a KiteConnection with a `.kite` KiteConnect
        # SDK handle. Guard against a non-Kite connection landing here
        # (would only happen if the operator rebound the ticker to a
        # non-Kite account, which is not currently possible via any code
        # path). Returns False with a clear log line rather than raising
        # AttributeError inside a stop()+start() cycle.
        try:
            from backend.brokers.connections import Connections
            conn = Connections().conn.get(prev_account)
            if conn is None:
                logger.warning(f"KiteTicker: recycle() — no Connections handle for {prev_account}")
                return False
            if not hasattr(conn, "kite"):
                logger.warning(
                    f"KiteTicker: recycle() — {prev_account!r} connection has no "
                    "kite SDK handle (non-Kite account bound to ticker?); skipping"
                )
                return False
            api_key      = conn.kite.api_key
            access_token = conn.kite.access_token
        except Exception as exc:
            logger.warning(f"KiteTicker: recycle() — failed to resolve credentials: {exc}")
            return False

        logger.warning(
            f"KiteTicker: recycling on {prev_account} "
            f"(re-subscribing {len(prev_subs)} token(s)) — HARD refresh"
        )
        # Wipe BOTH _tick_map AND _tick_age — operator wants a fresh
        # build; stale ticks from before the recycle shouldn't
        # contaminate the freshly-rebuilt state. The token↔sym maps
        # are wiped too: on instrument roll / expiry change the post-
        # recycle token may map to a different sym; preserving the old
        # entry would let has_sym() return True and silently skip the
        # re-subscribe in subscribe_with_sym(), so get_ltp_by_sym()
        # would return None for symbols whose ticks are actually landing
        # under a different token.
        #
        # Race-window note: do this BEFORE stop() so an in-flight
        # _on_ticks fired on the Twisted reactor thread can't slip a
        # post-clear write into _tick_map between stop() and the clear.
        # stop() sets _connected=False outside the lock, leaving a ~ms
        # window where ticks can still land. Clearing under the lock
        # before teardown closes that window (any in-flight tick
        # arriving while we hold the lock blocks on the lock and
        # finds _started=False by the time it lands, since stop()
        # below also clears _started).
        with self._lock:
            self._tick_map.clear()
            self._tick_age.clear()
            self._token_to_sym.clear()
            self._sym_to_token.clear()
        self.stop()
        self._subscribed = set()
        self._pending    = set(prev_subs)
        self.start(api_key, access_token, account=prev_account)
        return self._started


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
        # Record the swap BEFORE tearing down so status() reflects the
        # attempt even when the new start() fails (audit trail wants
        # every attempt, not just successes). reset the unhealthy
        # counter so the next watchdog cycle starts fresh against the
        # incoming account.
        self.record_swap(prev_account, account)
        self.reset_unhealthy()
        # Clean shutdown of the old socket (idempotent if already closed).
        self.stop()
        # Re-init state (stop() resets _started; reuse __init__-style defaults).
        self._subscribed = set()
        self._pending = set(prev_subs)
        # _tick_map intentionally preserved — operator's UI keeps showing
        # the last known LTPs until fresh ticks roll in from the new account.
        # _tick_age is intentionally reset — tokens may differ across
        # accounts, and stale timestamps from the old account would cause
        # status() to report stale_count=0 falsely during the brief
        # window between restart and on_connect.
        # _token_to_sym + _sym_to_token also reset for the same reason:
        # tokens may differ across accounts, and a stale entry would let
        # has_sym() short-circuit subscribe_with_sym() on the new account.
        with self._lock:
            self._tick_age = {}
            self._token_to_sym.clear()
            self._sym_to_token.clear()
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

        Zero-LTP guard (Sleep audit Jun 2026 — LTP flicker definitive fix):
        Kite occasionally sends `last_price: 0` for a freshly-subscribed
        instrument before the first real trade lands (especially on
        illiquid MCX contracts and right at market open). Publishing those
        zeros to the SSE bus poisons the frontend symbolStore — the
        ltp_ts arbitration there then refuses the next positive poll
        (`incomingTs(0) < storedTs(NOW)` rejects the write) and the cell
        stays at 0 until another live tick lands, which on an illiquid
        contract can be minutes. Filtering at the source (lp > 0) means
        no zero ever lands in `_tick_map`, the SSE bus, or `/dev/shm`.
        """
        to_publish: list[dict] = []
        ts = int(time.time())
        ts_ns = time.time_ns()
        with self._lock:
            for t in ticks:
                tok = t.get("instrument_token")
                lp  = t.get("last_price")
                # Skip ticks with no token, no price, or zero/negative
                # price (cold-subscription artefact — see docstring).
                if tok is None or lp is None:
                    continue
                try:
                    lp_f = float(lp)
                except (TypeError, ValueError):
                    continue
                if not (lp_f > 0):
                    continue
                tok = int(tok)
                self._tick_map[tok] = lp_f
                self._tick_age[tok] = ts
                to_publish.append({
                    "tok": tok,
                    "sym": self._token_to_sym.get(tok, ""),
                    "ltp": lp_f,
                    "ts":  ts,
                })
        # Mirror to the shared-memory buffer outside the lock — the
        # writer's only state is mmap byte positions; safe to call
        # concurrently with reads from other processes (we're the
        # single writer in this process). Cheap: each upsert is one
        # hash + one struct.pack_into.
        if self._tick_buffer is not None:
            for payload in to_publish:
                try:
                    self._tick_buffer.upsert(
                        payload["tok"],
                        payload["ltp"],
                        ts_ns=ts_ns,
                    )
                except Exception:
                    # Don't let a buffer write blow up the tick path.
                    # Worst case: readers see stale data — fine.
                    pass
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
        _emit_conn_event(
            self._current_account or "", "kite", "ticker_close", {"code": code}
        )

    def _on_error(self, _ws, code, reason) -> None:
        logger.error(f"KiteTicker: error — code={code} reason={reason!r}")
        _emit_conn_event(
            self._current_account or "", "kite", "ticker_error",
            {"error": str(reason)},
        )

    def _on_reconnect(self, _ws, attempts_count) -> None:
        logger.warning(f"KiteTicker: reconnecting — attempt {attempts_count}")
        _emit_conn_event(
            self._current_account or "", "kite", "ticker_reconnect",
            {"attempt": attempts_count},
        )


# ── Module-level singleton ────────────────────────────────────────────────────

_ticker = TickerManager()


def get_ticker():
    """Return the per-process ticker handle.

    Two flavours depending on RAMBOQ_USE_CONN_SERVICE:

      • Unset (legacy / conn_service): the in-process `TickerManager`
        that owns the KiteTicker WebSocket and tick map. conn_service
        runs with this flag UNSET, so its own get_ticker() returns
        the real TickerManager.

      • Set (main API after slice 4): a `MmapTickReader` that reads
        ticks from /dev/shm/ramboq_ticks and forwards subscribes to
        conn_service over UDS. Same external API; routes/quote.py
        etc. don't need to know which flavour they got.
    """
    import os
    if os.environ.get("RAMBOQ_USE_CONN_SERVICE", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        from backend.brokers.mmap_ticker import get_mmap_reader
        return get_mmap_reader()
    return _ticker
