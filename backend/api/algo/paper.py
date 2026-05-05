"""
Paper trade engine — simulates the order lifecycle (place → modify →
fill / unfilled) against any `QuoteSource`.

This is the same fill/modify/unfilled state machine that previously
lived inside `SimDriver._chase_open_orders`, lifted out so it can be
shared between two consumers:

  - **Mode 1, simulator** — `SimDriver` constructs a PaperTradeEngine
    fed by `SimQuoteSource`. Each scenario tick applies fabricated
    moves, then calls `engine.step()` to walk the open-order book
    against the new bid/ask.
  - **Mode 2, real-data paper** — a singleton PaperTradeEngine fed by
    `LiveQuoteSource`. A standalone background tick (every ~5 s on
    main during market hours) calls `engine.step()` so paper orders
    fill at realistic live prices without ever hitting Kite's order
    endpoint.

The engine writes terminal state back to `algo_orders` (mode='sim' or
mode='paper' set by the caller) so the existing Order log surface
shows paper rows the same way it shows live and sim rows.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Callable, Optional

from backend.api.algo.quote import QuoteSource
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Per-symbol price history cap. At a 5 s tick interval (mode-2 prod default)
# 600 entries is ~50 minutes of history per symbol. Auto-trimmed by the
# deque maxlen so memory stays bounded across long uptimes.
PRICE_HISTORY_LIMIT = 600


class PaperTradeEngine:
    """
    Owns an in-memory open-order book and a chase loop. Constructor
    parameters:

      `quote_source`   — required; supplies bid/ask each tick.
      `label`          — short tag for log lines / detail strings
                         ("sim" or "paper"). Default "paper".
      `get_max_attempts` — zero-arg callable returning the chase cap.
                          Default reads `simulator.chase_max_attempts`
                          live so a /admin/settings tweak applies on
                          the next tick.
      `on_event`       — optional callback `(event_dict) → None` —
                         receives every chase event (fill / modify /
                         unfilled) so the simulator can forward them
                         into its tick log; mode 2 keeps its own.
    """

    def __init__(
        self,
        *,
        quote_source: QuoteSource,
        label: str = "paper",
        get_max_attempts: Optional[Callable[[], int]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._quote = quote_source
        self._label = label
        self._get_max = get_max_attempts or self._default_max_attempts
        self._on_event = on_event or (lambda evt: None)

        # Open paper orders. Each entry is the dict the caller registered
        # plus a runtime-managed `status` / `attempts` / `placed_at` set.
        self._open_orders: list[dict] = []
        # Tracks fire-and-forget DB-write tasks so a graceful shutdown
        # can await them.
        self._pending_updates: set = set()
        # Per-symbol rolling price history surfaced via /api/charts/price-history.
        # Populated in `step()` after every prefetch so the chart panel can
        # show the trajectory of the bid/ask each chase saw, with order-event
        # markers (placed / filled / unfilled / modified) overlaid by the
        # API layer reading from algo_orders.
        self._price_history: dict[str, deque] = {}

        # Parallel buffer for underlying spot prices (NIFTY, BANKNIFTY, …).
        # Populated alongside contract ticks so the chart panel can render
        # underlying lines next to derivatives — same UX as the simulator.
        self._underlying_history: dict[str, deque] = {}

    # ── Public surface ───────────────────────────────────────────────

    def register_open_order(self, order: dict) -> None:
        """
        Caller (the action handler) submits a paper order here after
        persisting the initial AlgoOrder row. Required fields:
            algo_order_id, account, symbol, side, qty,
            limit_price, initial_price, exchange,
            agent_slug, action_type
        The engine seeds status='OPEN' / attempts=0 / placed_at and
        owns the lifecycle from here on.
        """
        order.setdefault("status",    "OPEN")
        order.setdefault("attempts",  0)
        order.setdefault("placed_at", datetime.now().isoformat(timespec="seconds"))
        self._open_orders.append(order)

    def step(self) -> None:
        """
        One chase iteration. Walks every OPEN order, asks the quote
        source for its bid/ask, fills / modifies / marks unfilled. Safe
        to call from a sync simulator tick or from an async background
        loop (DB writes are scheduled via asyncio.create_task).
        """
        if not self._open_orders:
            return
        # Bulk-fetch quotes for every open order before the loop —
        # LiveQuoteSource does one broker.quote([many]) per account
        # instead of N round-trips. SimQuoteSource is in-memory so its
        # prefetch is a no-op.
        open_now = [o for o in self._open_orders if o.get("status") == "OPEN"]
        if open_now:
            try:
                self._quote.prefetch_for(open_now)
            except Exception as e:
                logger.debug(f"PaperTradeEngine[{self._label}] prefetch failed: {e}")
            # Snapshot bid/ask per active symbol so the chart panel can render
            # the trajectory the chase loop saw. Done before the chase walk so
            # the snapshot reflects the same quote the engine evaluated against.
            self._capture_price_history(open_now)
        max_attempts = max(0, int(self._get_max() or 0))

        for order in list(self._open_orders):
            if order.get("status") != "OPEN":
                continue
            bid, ask = self._quote.bid_ask_for_order(order)
            if bid is None or ask is None:
                # Quote unavailable — flip to FILLED with a marker note.
                # Mirrors the simulator's "underlying position absent"
                # auto-close so stale orders can't accumulate.
                order["status"] = "FILLED"
                self._record_event(order, kind="fill",
                                   note="underlying position absent — auto-closed")
                continue

            side  = str(order.get("side") or "SELL").upper()
            limit = float(order.get("limit_price") or 0)

            fillable = (side == "SELL" and bid >= limit) or \
                       (side == "BUY"  and ask <= limit)
            if fillable:
                fill_price = bid if side == "SELL" else ask
                order["status"]     = "FILLED"
                order["fill_price"] = fill_price
                order["filled_at"]  = datetime.now().isoformat(timespec="seconds")
                self._record_event(order, kind="fill",
                                   note=f"filled @₹{fill_price:,.2f}")
                # Hand off to the quote source — sim removes the
                # filled position from its book; live is a no-op.
                self._quote.on_fill(order)
                continue

            # Not fillable — chase by re-quoting. The new limit
            # depends on the order's `chase_agg` setting:
            #   high (default): peg to the marketable side — SELL→
            #     bid, BUY→ ask — so the next tick fills immediately
            #     (cross the spread to take liquidity).
            #   med: peg to the midpoint of bid+ask. Fills only when
            #     the inside moves halfway in our favour.
            #   low: peg to the passive side — SELL→ ask, BUY→ bid.
            #     Order rests on our own side and waits for the
            #     market to lift it.
            # Unknown values fall back to 'high' so legacy callers
            # (existing agents using register_open_order without the
            # field) keep their current behaviour.
            if order.get("attempts", 0) >= max_attempts:
                order["status"] = "UNFILLED"
                self._record_event(order, kind="unfilled",
                                   note=f"gave up after {max_attempts} chase attempts")
                continue
            agg = str(order.get("chase_agg") or "low").lower()
            if agg == "high":
                new_limit = bid if side == "SELL" else ask
            elif agg == "med":
                new_limit = (bid + ask) / 2.0
            else:  # 'low' — patient (default): peg to your own side
                new_limit = ask if side == "SELL" else bid
            prev_limit = limit
            order["limit_price"] = new_limit
            order["attempts"]    = int(order.get("attempts", 0)) + 1
            self._record_event(
                order, kind="modify",
                note=(f"chase #{order['attempts']} [{agg}] {side} "
                      f"₹{prev_limit:,.2f} → ₹{new_limit:,.2f}"),
            )

    async def tick_loop(self, interval_seconds: int = 5) -> None:
        """
        Mode-2 entry point — runs `step()` every `interval_seconds`
        until cancelled. Mode 1 doesn't call this; the simulator's
        scenario tick drives `step()` directly.

        `step()` is sync but calls `LiveQuoteSource.prefetch_for()` which
        makes blocking broker HTTP requests. Offload to a thread executor
        so the event loop is never stalled by the ~200–500 ms Kite round-
        trip.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(None, self.step)
            except Exception as e:
                logger.error(f"PaperTradeEngine[{self._label}] step failed: {e}")
            await asyncio.sleep(max(1, interval_seconds))

    def has_open_orders(self) -> bool:
        return any(o.get("status") == "OPEN" for o in self._open_orders)

    def open_order_details(self) -> list[dict]:
        """Compact snapshot of in-flight chases for the UI."""
        return [
            {
                "account":       o.get("account"),
                "symbol":        o.get("symbol"),
                "side":          o.get("side"),
                "qty":           o.get("qty"),
                "limit_price":   o.get("limit_price"),
                "initial_price": o.get("initial_price"),
                "attempts":      o.get("attempts", 0),
                "status":        o.get("status"),
                "algo_order_id": o.get("algo_order_id"),
            }
            for o in self._open_orders
            if o.get("status") == "OPEN"
        ]

    def reset(self) -> None:
        """Wipe the open-order book — used by SimDriver.start()."""
        self._open_orders = []
        self._price_history = {}
        self._underlying_history = {}

    # ── Price history ────────────────────────────────────────────────

    def _capture_price_history(self, open_orders: list[dict]) -> None:
        """One (ts, ltp, bid, ask) entry per active-order symbol. Called
        after prefetch_for, so the QuoteSource cache is warm and reads are
        cheap. Symbols are deduplicated so two open orders on the same
        symbol only produce one tick in the history. Also fetches the
        underlying spot for any derivative symbol so the chart panel can
        render underlying lines next to options."""
        from backend.api.algo.derivatives import (
            parse_tradingsymbol, underlying_ltp_key,
        )

        ts   = datetime.now().isoformat(timespec="seconds")
        seen: set[str] = set()
        underlyings: dict[str, str] = {}   # name → ltp_key
        for o in open_orders:
            sym = str(o.get("symbol") or "")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            bid, ask = self._quote.bid_ask_for_order(o)
            if bid is None and ask is None:
                continue
            ltp = (bid + ask) / 2.0 if (bid is not None and ask is not None) \
                  else (bid if bid is not None else ask)
            buf = self._price_history.get(sym)
            if buf is None:
                buf = deque(maxlen=PRICE_HISTORY_LIMIT)
                self._price_history[sym] = buf
            buf.append({"ts": ts, "ltp": float(ltp),
                        "bid": float(bid) if bid is not None else None,
                        "ask": float(ask) if ask is not None else None})
            parsed = parse_tradingsymbol(sym)
            if parsed:
                underlyings.setdefault(parsed["underlying"],
                                       underlying_ltp_key(parsed["underlying"]))

        # Best-effort underlying spot fetch — ONE broker.ltp call covers
        # every distinct underlying. Routes through the first open order's
        # account; underlying spots aren't account-specific so any handle
        # works. Failures are silent — charts just miss the underlying line.
        if underlyings and open_orders:
            self._capture_underlyings(ts, open_orders[0].get("account"),
                                      underlyings)

    def _capture_underlyings(self, ts: str, account: str | None,
                             underlyings: dict[str, str]) -> None:
        # Underlying spots (NIFTY 50, NIFTY BANK, etc.) aren't
        # account-scoped — they're public market data. Route through
        # `get_price_broker()` which honors the
        # `connections.price_account` setting, so the operator can
        # centralize "which Kite handle do we use for shared data" in
        # /admin/settings. Fall back to whatever account opened the
        # first order if the setting hasn't been set + the pinned
        # account isn't configured.
        try:
            from backend.shared.brokers.registry import get_price_broker, get_broker
            try:
                broker = get_price_broker()
            except Exception:
                if not account:
                    return
                broker = get_broker(account)
            keys = list(underlyings.values())
            resp = broker.ltp(keys) or {}
        except Exception as e:
            logger.debug(f"PaperTradeEngine[{self._label}] underlying ltp fetch failed: {e}")
            return
        for name, key in underlyings.items():
            quote = resp.get(key) or {}
            ltp   = quote.get("last_price")
            if ltp is None:
                continue
            buf = self._underlying_history.get(name)
            if buf is None:
                buf = deque(maxlen=PRICE_HISTORY_LIMIT)
                self._underlying_history[name] = buf
            buf.append({"ts": ts, "ltp": float(ltp), "bid": None, "ask": None})

    def price_history(self, symbol: str, *, since: str | None = None,
                      limit: int = 600) -> list[dict]:
        buf = self._price_history.get(symbol) or self._underlying_history.get(symbol)
        if not buf:
            return []
        out: list[dict] = []
        for entry in buf:
            if since and entry["ts"] <= since:
                continue
            out.append(entry)
        if limit and len(out) > limit:
            out = out[-limit:]
        return out

    def price_history_symbols(self) -> list[str]:
        names = {s for s, buf in self._price_history.items() if buf}
        names.update(s for s, buf in self._underlying_history.items() if buf)
        return sorted(names)

    def underlying_for(self, symbol: str) -> str | None:
        """Return the underlying name for a contract, or None if `symbol`
        is itself an underlying / not a derivative. Used by the chart UI
        to overlay the spot line on each option chart."""
        if symbol in self._underlying_history:
            return None
        from backend.api.algo.derivatives import parse_tradingsymbol
        parsed = parse_tradingsymbol(symbol)
        if not parsed:
            return None
        und = parsed["underlying"]
        return und if und in self._underlying_history else None

    async def recover_from_db(self) -> int:
        """
        Re-register this engine's `mode == self._label` rows that are
        still OPEN in the database. Survives a service restart so paper
        chases that were mid-flight when the process died can resume
        from where they left off.

        Returns the count recovered.
        """
        from backend.api.database  import async_session
        from backend.api.models    import AlgoOrder
        from sqlalchemy            import select, and_

        try:
            async with async_session() as s:
                rows = (await s.execute(
                    select(AlgoOrder).where(and_(
                        AlgoOrder.mode   == self._label,
                        AlgoOrder.status == "OPEN",
                    ))
                )).scalars().all()
        except Exception as e:
            logger.warning(f"PaperTradeEngine[{self._label}] recover query failed: {e}")
            return 0

        for r in rows:
            init_price = float(r.initial_price) if r.initial_price is not None else None
            self.register_open_order({
                "algo_order_id": r.id,
                "account":       r.account,
                "symbol":        r.symbol,
                "exchange":      r.exchange,
                "side":          r.transaction_type,
                "qty":           int(r.quantity or 0),
                # Restart at initial_price — the in-memory current limit
                # was lost on shutdown. The chase loop will re-quote on
                # the very next tick anyway, so worst case we re-do one
                # cycle. attempts resets to 0 so a stranded order near
                # the cap gets a clean chance to fill.
                "limit_price":   init_price,
                "initial_price": init_price,
                "agent_slug":    "(recovered)",
                "action_type":   "recovered",
            })
        if rows:
            logger.info(
                f"PaperTradeEngine[{self._label}]: recovered {len(rows)} OPEN "
                "order(s) from DB after restart"
            )
        return len(rows)

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _default_max_attempts() -> int:
        from backend.shared.helpers.settings import get_int
        return get_int("simulator.chase_max_attempts", 5)

    def _record_event(self, order: dict, *, kind: str, note: str) -> None:
        """
        Persist a chase lifecycle event:
          1. Build the structured event payload (used by the on_event
             callback — sim forwards into its tick log).
          2. Log a one-line warning so operators tailing logs see the
             full chase history.
          3. Schedule a DB update on the AlgoOrder row so attempts /
             status / fill_price stay in sync without waiting for the
             terminal state.
        """
        tag = self._label.upper()
        evt = {
            "ts":         datetime.now().isoformat(timespec="seconds"),
            "kind":       kind,
            "label":      self._label,
            "note":       (f"[{tag}] {order.get('agent_slug','?')} · "
                           f"{order.get('action_type','?')}: "
                           f"{order.get('side')} {order.get('qty')} "
                           f"{order.get('symbol')} · {note}"),
            "order": {
                "account":       order.get("account"),
                "symbol":        order.get("symbol"),
                "side":          order.get("side"),
                "qty":           order.get("qty"),
                "limit_price":   order.get("limit_price"),
                "status":        order.get("status"),
                "attempts":      order.get("attempts"),
                "algo_order_id": order.get("algo_order_id"),
            },
        }
        try:
            self._on_event(evt)
        except Exception as e:
            logger.debug(f"PaperTradeEngine[{self._label}] on_event failed: {e}")

        logger.warning(
            f"[{tag}] order {kind} · {order.get('agent_slug','?')} · "
            f"{order.get('symbol')} {order.get('side')} "
            f"{order.get('qty')} · {note}"
        )

        if kind in ("fill", "unfilled", "modify") and order.get("algo_order_id"):
            try:
                task = asyncio.create_task(self._safe_update_algo_order(order, kind))
                self._pending_updates.add(task)
                task.add_done_callback(self._pending_updates.discard)
            except RuntimeError:
                # No event loop (sync `Step` button etc.) — DB will get
                # the terminal state from the next sync update path.
                pass

    async def _safe_update_algo_order(self, order: dict, kind: str) -> None:
        try:
            await self._update_algo_order(order, kind)
        except Exception as e:
            logger.warning(
                f"[{self._label.upper()}] _update_algo_order failed "
                f"(kind={kind}, id={order.get('algo_order_id')}): {e}"
            )

    async def _update_algo_order(self, order: dict, kind: str) -> None:
        from backend.api.database import async_session
        from backend.api.models  import AlgoOrder
        from sqlalchemy          import select as _select
        async with async_session() as s:
            row = (await s.execute(
                _select(AlgoOrder).where(AlgoOrder.id == order["algo_order_id"])
            )).scalar_one_or_none()
            if not row:
                return
            # Status transitions:
            #   modify   → stays OPEN (attempts + detail refresh)
            #   fill     → FILLED, fill_price + slippage + filled_at
            #   unfilled → UNFILLED, attempts frozen at the cap
            if kind == "fill":
                row.status = "FILLED"
            elif kind == "unfilled":
                row.status = "UNFILLED"
            row.attempts = int(order.get("attempts", 0))

            tag    = self._label.upper()
            side   = order.get("side") or "?"
            qty    = order.get("qty") or 0
            symbol = order.get("symbol") or "?"
            limit  = order.get("limit_price")
            agent  = order.get("agent_slug", "?")

            if kind == "modify":
                if limit is not None:
                    row.detail = (
                        f"[{tag}] {agent} {side} {qty} {symbol} · "
                        f"chase #{row.attempts} limit=₹{limit:,.2f}"
                    )
                else:
                    row.detail = (
                        f"[{tag}] {agent} {side} {qty} {symbol} · "
                        f"chase #{row.attempts}"
                    )
            elif kind == "fill" and order.get("fill_price") is not None:
                row.fill_price = float(order["fill_price"])
                initial = row.initial_price or 0
                if initial:
                    row.slippage = float(order["fill_price"]) - float(initial)
                row.filled_at = datetime.now()
                row.detail = (
                    f"[{tag}] {agent} {side} {qty} {symbol} · "
                    f"FILLED @₹{row.fill_price:,.2f} after {row.attempts} chase(s)"
                )
            elif kind == "unfilled":
                row.detail = (
                    f"[{tag}] {agent} {side} {qty} {symbol} · "
                    f"UNFILLED — gave up after {row.attempts} chase(s)"
                )
            await s.commit()


# ═════════════════════════════════════════════════════════════════════════
#  Prod singleton — mode 2 (real-data paper on main branch)
# ═════════════════════════════════════════════════════════════════════════

_prod_paper_engine: Optional[PaperTradeEngine] = None


def get_prod_paper_engine() -> PaperTradeEngine:
    """
    Lazily constructed PaperTradeEngine fed by LiveQuoteSource.
    Used by the mode-2 action path: every broker-hitting handler that
    isn't promoted to live writes an AlgoOrder(mode='paper') and
    registers the order here. A background `tick_loop` runs the chase
    against real Kite quotes.

    On non-main branches this is unused — dev's paper trading is the
    existing simulator (SimDriver owns its own engine fed by
    SimQuoteSource).
    """
    global _prod_paper_engine
    if _prod_paper_engine is None:
        from backend.api.algo.quote import LiveQuoteSource
        _prod_paper_engine = PaperTradeEngine(
            quote_source=LiveQuoteSource(),
            label="paper",
        )
    return _prod_paper_engine
