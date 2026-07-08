"""MmapTickReader — drop-in replacement for TickerManager on the
main API side when the broker WebSocket lives in conn_service.

Implements the subset of TickerManager's public surface that callers
actually use, sourcing tick data from the shared-memory buffer at
/dev/shm/ramboq_ticks. The full TickerManager (which owns the
KiteTicker WebSocket, BroadcastBus producer, watchdog state) runs in
conn_service. This class is a thin reader + a UDS forwarder.

Replaced surface:
  • get_ltp / get_ltp_batch / get_ltp_by_sym  — read mmap directly
  • subscribe_with_sym / subscribe            — POST to conn_service
  • has_sym / snapshot                        — local cache + mmap scan
  • bus()                                     — local BroadcastBus fed
                                                by an mmap-polling task
  • set_loop / start / stop                   — lifecycle for the poller
  • status / current_account / recycle        — proxy to conn_service
  • seconds_since_connect / seconds_since_disconnect — local stub

The polling task tails the buffer's monotonic version word every
50ms; on bump it scans active slots, diffs against the previous
snapshot, and publishes each changed (token, ltp) to the local
BroadcastBus so SSE clients keep working without any wire change.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Throttled log helpers — emit at most once per (key, 60s) to avoid
# log spam when the sym→token registry gap is widespread at boot.
# ---------------------------------------------------------------------------
_MMAP_LOG_TTL_S = 60.0
_mmap_missing_sym_last: dict[int, float] = {}  # token → last_log_monotonic

from backend.brokers.tick_buffer import (
    DEFAULT_PATH,
    TickBufferReader,
)

logger = logging.getLogger(__name__)

# Same BroadcastBus implementation TickerManager uses; importing the
# class avoids duplicating its put_nowait + threadsafe scheduling.
from backend.brokers.kite_ticker import BroadcastBus  # noqa: E402

_POLL_INTERVAL_S = 0.05  # 50ms — well under one tick cycle


class MmapTickReader:
    """Local-process tick reader backed by /dev/shm/ramboq_ticks.

    Singleton-friendly (instantiated once at app startup); all methods
    are safe to call from any thread or coroutine."""

    def __init__(self, path: str = DEFAULT_PATH):
        self._path = path
        self._reader: Optional[TickBufferReader] = None
        self._reader_lock = threading.Lock()
        self._bus = BroadcastBus()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Local token↔sym caches — populated by subscribe_with_sym so
        # has_sym() and SSE payloads can resolve sym → token without
        # an extra UDS hop. These mirror what TickerManager keeps.
        self._token_to_sym: dict[int, str] = {}
        self._sym_to_token: dict[str, int] = {}

        # Poller state — last seen version + last seen LTP per token
        # so we can diff and only emit changes.
        self._poll_task: Optional[asyncio.Task] = None
        self._last_version = 0
        self._last_ltp: dict[int, float] = {}

        # Failover diagnostics — when the underlying WS is owned by
        # another process we don't know connect/disconnect times
        # locally, so we surface "always connected" stubs unless the
        # /internal/ticker/status proxy says otherwise.
        self._stub_connected_at = time.time()

    # ── reader handle ──────────────────────────────────────────────────

    def _open_reader(self) -> Optional[TickBufferReader]:
        """Lazy-open the mmap. Returns None when the writer hasn't
        created the file yet; caller retries on next access."""
        if self._reader is not None:
            return self._reader
        with self._reader_lock:
            if self._reader is not None:
                return self._reader
            try:
                self._reader = TickBufferReader(self._path)
            except FileNotFoundError:
                return None
            except Exception:
                logger.exception("MmapTickReader: failed to open %s", self._path)
                return None
        return self._reader

    # ── BroadcastBus passthrough (SSE) ─────────────────────────────────

    def bus(self) -> BroadcastBus:
        return self._bus

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._bus.set_loop(loop)

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self, api_key: str = "", access_token: str = "", account: str = "") -> None:
        """No WebSocket lifecycle here — conn_service owns it. We just
        start the poller that feeds the local BroadcastBus."""
        if self._poll_task is not None:
            return
        if self._loop is None:
            # Prefer the running-loop reference (Python 3.10+ correct call
            # inside a coroutine; get_event_loop() emits DeprecationWarning
            # under 3.12+). If we're called from sync context with no loop,
            # try get_event_loop() as legacy fallback before giving up.
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    self._loop = asyncio.get_event_loop()
                except RuntimeError:
                    return
        self._poll_task = self._loop.create_task(self._poll_loop())
        logger.info("MmapTickReader: poller started · path=%s", self._path)

    def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    def ensure_started(self, api_key: str = "", access_token: str = "", account: str = "") -> bool:
        if self._poll_task is None:
            self.start(api_key, access_token, account)
        return self._poll_task is not None

    # ── read API (hot path) ────────────────────────────────────────────

    def get_ltp(self, token: int) -> float | None:
        r = self._open_reader()
        if r is None:
            return None
        return r.get_ltp(int(token))

    def get_ltp_by_sym(self, sym: str) -> float | None:
        tok = self._sym_to_token.get(str(sym or "").upper())
        if tok is None:
            return None
        return self.get_ltp(tok)

    def get_ltp_batch(self, tokens: Iterable[int]) -> dict[int, float]:
        r = self._open_reader()
        if r is None:
            return {}
        return r.get_ltp_batch([int(t) for t in tokens])

    def has_sym(self, sym: str) -> bool:
        return str(sym or "").upper() in self._sym_to_token

    def snapshot(self) -> dict[int, dict]:
        """Return {token: {ltp, sym}} for every slot that currently
        carries a tick. Used by SSE initial-snapshot event."""
        r = self._open_reader()
        if r is None:
            return {}
        return {
            tok: {"ltp": lp, "sym": self._token_to_sym.get(tok, "")}
            for tok, lp, _pc, _av, _ts in r.iter_active()
        }

    # ── subscribe (UDS forward) ────────────────────────────────────────

    def subscribe(self, tokens: Iterable[int]) -> None:
        # Symbol-less form — push tokens with empty syms; conn_service
        # accepts both shapes.
        self.subscribe_with_sym([(int(t), "") for t in tokens])

    def subscribe_with_sym(self, token_sym_pairs: Iterable[tuple[int, str]]) -> None:
        pairs = [(int(t), str(s or "")) for t, s in token_sym_pairs]
        if not pairs:
            return
        # Update local sym caches first so has_sym() works immediately
        # even before the UDS hop returns.
        for tok, sym in pairs:
            if sym:
                self._token_to_sym[tok] = sym
                self._sym_to_token[sym.upper()] = tok
        # Fire-and-forget — failures here surface in the next tick
        # blackout and are visible via /internal/ticker/status.
        try:
            from backend.brokers.client.sync import _get_client
            _get_client().post(
                "/internal/ticker/subscribe",
                json={"pairs": [[t, s] for t, s in pairs]},
            )
        except Exception as e:
            logger.warning("MmapTickReader: subscribe forward failed: %s", e)

    # ── status / diagnostics (UDS forward) ─────────────────────────────

    def status(self, stale_threshold_sec: int = 60, stale_top_n: int = 20) -> dict:
        try:
            from backend.brokers.client.sync import _get_client
            resp = _get_client().get("/internal/ticker/status")
            body = resp.json() or {}
            if body.get("ok"):
                return body.get("status") or {}
        except Exception:
            pass
        # Fallback when conn_service is unreachable — surface what we
        # can see locally from the buffer header.
        r = self._open_reader()
        if r is None:
            return {"started": False, "connected": False, "subscribed_count": 0}
        version, slot_count, _max, last_write_ns, _schema = r.header()
        age_s = (time.time_ns() - last_write_ns) / 1e9 if last_write_ns else None
        return {
            "started": True,
            "connected": last_write_ns > 0,
            "subscribed_count": slot_count,
            "version": version,
            "last_write_age_seconds": age_s,
        }

    def current_account(self) -> str:
        try:
            s = self.status() or {}
            # `active_account` is the canonical key (TickerManager.status
            # from Jun 2026 onwards). Accept the legacy `current_account`
            # as fallback for one deploy cycle so a rolling restart of
            # ramboq_conn + ramboq_api can't briefly report ""
            # depending on which end deployed first.
            return s.get("active_account") or s.get("current_account", "")
        except Exception:
            return ""

    def seconds_since_connect(self) -> float:
        return time.time() - self._stub_connected_at

    def seconds_since_disconnect(self) -> float:
        return 0.0

    def is_account_in_failover_cooloff(self, account: str, cool_seconds: float = 300.0) -> bool:
        return False

    def recycle(self) -> bool:
        # Recycle is a conn_service lifecycle concern; expose via /internal
        # if needed. Today main API's watchdog won't fire because the WS
        # state lives there too — return False to signal nothing happened.
        return False

    # ── tick-buffer poller (writes to local bus) ───────────────────────

    async def _poll_loop(self) -> None:
        """Tail the mmap version word; on change, diff active slots and
        publish each updated (token, ltp) to the local BroadcastBus.

        Zero-LTP guard — the mmap slot may transiently carry ``lp <= 0``
        during a torn-read window (see ``TickBufferReader.get_ltp`` and
        ``iter_active``). Publishing that to the local BroadcastBus would
        poison ``symbolStore``: the frontend arbitration then refuses the
        next positive poll (``incomingTs(0) < storedTs(NOW)`` rejects the
        write) and the cell freezes at 0 until another live tick lands.
        Filter here (belt + suspenders with the ``kite_ticker._on_ticks``
        writer-side guard).

        Empty-sym skip — when ``_token_to_sym`` has no entry for a token
        (registration gap between ticker start and
        ``_task_sparkline_warm._register_universe_with_ticker`` landing),
        the previous code shipped ``sym: ""`` to the local BroadcastBus.
        ``quoteStream.js`` drops falsy-sym ticks anyway, so the cell fell
        back to the polled REST ``row.last_price`` (which equals
        ``close_price`` in thin-tick windows) → visible LTP/close flicker
        between poll cycles.

        Fix (Jul 2026): skip the publish entirely for unregistered
        tokens. The ``[MMAP-MISSING-SYM]`` warning still fires (throttled
        to once per token per 60s) so the operator sees the gap. Also,
        ``_last_ltp[tok]`` is updated ONLY after the sym check so the
        NEXT tick after a mid-cycle sym registration lands still fires
        the publish (otherwise the first tick after registration would
        be diffed against the value we would have suppressed, silently
        skipping it).
        """
        while True:
            try:
                r = self._open_reader()
                if r is None:
                    await asyncio.sleep(0.25)
                    continue
                v = r.version()
                if v != self._last_version:
                    self._last_version = v
                    ts = int(time.time())
                    for tok, lp, _pc, _av, _ts_ns in r.iter_active():
                        # Zero-LTP guard — never publish a torn-read
                        # zero or a stale-empty slot double. Matches the
                        # writer-side guard in kite_ticker._on_ticks.
                        if not (lp > 0):
                            continue
                        prev = self._last_ltp.get(tok)
                        if prev == lp:
                            continue
                        sym_str = self._token_to_sym.get(tok, "")
                        if not sym_str:
                            # Throttled: log at most once per token per 60s
                            _now = time.monotonic()
                            if _now - _mmap_missing_sym_last.get(tok, 0.0) > _MMAP_LOG_TTL_S:
                                _mmap_missing_sym_last[tok] = _now
                                logger.warning(
                                    "[MMAP-MISSING-SYM] token=%d "
                                    "reason=local_token_not_registered",
                                    tok,
                                )
                            # Do NOT update _last_ltp — leave it so the
                            # NEXT tick after a mid-cycle registration
                            # fires as a fresh delta.
                            continue
                        self._last_ltp[tok] = lp
                        self._bus.publish({
                            "tok": tok,
                            "sym": sym_str,
                            "ltp": lp,
                            "ts":  ts,
                        })
                await asyncio.sleep(_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("MmapTickReader: poller error")
                await asyncio.sleep(1.0)


_singleton: Optional[MmapTickReader] = None


def get_mmap_reader() -> MmapTickReader:
    """Module-level singleton — analogous to kite_ticker.get_ticker()."""
    global _singleton
    if _singleton is None:
        _singleton = MmapTickReader()
    return _singleton
