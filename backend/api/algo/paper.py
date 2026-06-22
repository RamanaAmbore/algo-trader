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
import threading
from collections import deque
from datetime import datetime, timezone
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

        # Serialises concurrent reads/writes to _open_orders.
        # `step()` runs in a thread executor (from tick_loop) while the
        # Litestar event-loop thread can call register_open_order() at any
        # time — without the lock the list can be mutated mid-iteration.
        self._lock = threading.Lock()

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
        order.setdefault("placed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self._lock:
            self._open_orders.append(order)

        # Write the initial placed event asynchronously if we have a DB id.
        oid = order.get("algo_order_id")
        if oid:
            try:
                import asyncio
                from backend.api.algo.order_events import write_event
                task = asyncio.create_task(write_event(
                    oid, "placed",
                    f"[{self._label.upper()}] {order.get('agent_slug','?')} "
                    f"{order.get('side')} {order.get('qty')} {order.get('symbol')} "
                    f"registered with chase engine",
                    payload={
                        "limit_price":   order.get("limit_price"),
                        "initial_price": order.get("initial_price"),
                        "account":       order.get("account"),
                        "exchange":      order.get("exchange"),
                        "agent_slug":    order.get("agent_slug"),
                        "action_type":   order.get("action_type"),
                    },
                ))
                self._pending_updates.add(task)
                task.add_done_callback(self._pending_updates.discard)
            except RuntimeError:
                pass  # no event loop (sync context) — skip

    def step(self) -> None:
        """
        One chase iteration. Walks every OPEN order, asks the quote
        source for its bid/ask, fills / modifies / marks unfilled. Safe
        to call from a sync simulator tick or from an async background
        loop (DB writes are scheduled via asyncio.create_task).
        """
        # Snapshot the list under the lock so register_open_order() on the
        # event-loop thread cannot mutate it while we iterate. The snapshot
        # contains references to the same dicts, so in-place mutations
        # (status, attempts, limit_price) propagate back to _open_orders
        # without needing the lock again. We only re-acquire the lock for
        # structural changes (appends) which only happen in register_open_order.
        with self._lock:
            if not self._open_orders:
                return
            snapshot = list(self._open_orders)
        # Bulk-fetch quotes for every open order before the loop —
        # LiveQuoteSource does one broker.quote([many]) per account
        # instead of N round-trips. SimQuoteSource is in-memory so its
        # prefetch is a no-op.
        open_now = [o for o in snapshot if o.get("status") == "OPEN"]
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

        for order in snapshot:
            if order.get("status") != "OPEN":
                continue
            bid, ask = self._quote.bid_ask_for_order(order)
            if bid is None or ask is None:
                # Quote unavailable — mark UNFILLED so operators can
                # distinguish a real fill from a quote-timeout in the
                # order log. FILLED is reserved for actual bid/ask crosses.
                order["status"] = "UNFILLED"
                self._record_event(order, kind="unfilled",
                                   note="quote unavailable — paper engine could not fill")
                continue

            side  = str(order.get("side") or "SELL").upper()
            limit = float(order.get("limit_price") or 0)

            fillable = (side == "SELL" and bid >= limit) or \
                       (side == "BUY"  and ask <= limit)
            if fillable:
                fill_price = bid if side == "SELL" else ask
                order["status"]     = "FILLED"
                order["fill_price"] = fill_price
                order["filled_at"]  = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._record_event(order, kind="fill",
                                   note=f"filled @₹{fill_price:,.2f}")
                # Hand off to the quote source — sim removes the
                # filled position from its book; live is a no-op.
                self._quote.on_fill(order)
                # Slice 7a — write to the per-strategy FIFO lot
                # ledger. open_lot for a fresh entry, close_lot_fifo
                # for an exit. Determined by the order's
                # `is_close_intent` flag (set by the chase-close
                # action) — when present + true, the fill closes
                # rather than opens. Best-effort; legacy orders
                # without strategy_id skip.
                self._schedule_ledger_write(order, fill_price)
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
        with self._lock:
            return any(o.get("status") == "OPEN" for o in self._open_orders)

    def open_order_details(self) -> list[dict]:
        """Compact snapshot of in-flight chases for the UI."""
        with self._lock:
            snapshot = list(self._open_orders)
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
            for o in snapshot
            if o.get("status") == "OPEN"
        ]

    def reset(self) -> None:
        """Wipe the open-order book — used by SimDriver.start()."""
        self._open_orders = []
        self._price_history = {}
        self._underlying_history = {}

    # ── Operator-initiated lifecycle (Phase 5: MCP cancel/modify) ────

    def cancel_paper_order(self, algo_order_id: int) -> bool:
        """Cancel a paper order by its AlgoOrder.id. Marks the row
        CANCELLED in the engine + schedules the DB update via the
        existing _record_event pipeline (kind='unfilled' so the same
        terminal-status path runs). Returns True if a matching OPEN
        order was found, False otherwise.

        Idempotent — calling twice on the same id returns False on
        the second call (order is already gone). Safe to invoke from
        the MCP cancel-order route under any threading model since
        all _open_orders mutation goes through self._lock."""
        with self._lock:
            target = None
            for o in self._open_orders:
                if int(o.get("algo_order_id") or 0) == int(algo_order_id) \
                   and o.get("status") == "OPEN":
                    target = o
                    break
            if not target:
                return False
            target["status"] = "CANCELLED"
        # Outside the lock — _record_event writes to event sinks + DB.
        # Reuse 'unfilled' kind so _update_algo_order flips the row to
        # CANCELLED — but actually we want CANCELLED, not UNFILLED. We
        # write a dedicated event payload below + bypass the kind-based
        # status flip by calling _update_algo_order_cancel directly.
        self._record_event(target, kind="cancel",
                           note="operator-initiated cancel via MCP")
        try:
            import asyncio
            task = asyncio.create_task(self._safe_update_algo_order_cancel(target))
            self._pending_updates.add(task)
            task.add_done_callback(self._pending_updates.discard)
        except RuntimeError:
            pass
        return True

    def modify_paper_order(
        self,
        algo_order_id: int,
        *,
        new_qty: int | None = None,
        new_price: float | None = None,
        new_trigger: float | None = None,
        new_order_type: str | None = None,
    ) -> bool:
        """Modify a paper order's qty / limit_price / trigger_price /
        order_type in place. Engine's next step() picks up the new
        values automatically (it reads the dict each tick). Returns
        True if the order was found + at least one field changed."""
        with self._lock:
            target = None
            for o in self._open_orders:
                if int(o.get("algo_order_id") or 0) == int(algo_order_id) \
                   and o.get("status") == "OPEN":
                    target = o
                    break
            if not target:
                return False
            changed_parts = []
            if new_qty is not None and int(new_qty) > 0:
                target["qty"] = int(new_qty)
                changed_parts.append(f"qty={new_qty}")
            if new_price is not None:
                target["limit_price"] = float(new_price)
                changed_parts.append(f"limit=₹{float(new_price):,.2f}")
            if new_trigger is not None:
                target["trigger_price"] = float(new_trigger)
                changed_parts.append(f"trig=₹{float(new_trigger):,.2f}")
            if new_order_type:
                target["order_type"] = new_order_type
                changed_parts.append(f"type={new_order_type}")
            if not changed_parts:
                return False
        # Re-quote on the next tick will keep going; the modify event
        # captures the operator's manual override so the audit trail
        # shows it alongside chase-driven re-quotes.
        self._record_event(target, kind="modify",
                           note=f"operator override: {' '.join(changed_parts)}")
        return True

    async def _safe_update_algo_order_cancel(self, order: dict) -> None:
        """DB update for an operator-cancelled paper order. Sets
        status=CANCELLED + writes a 'cancel' AlgoOrderEvent. Mirrors
        _safe_update_algo_order but for the CANCELLED terminal state."""
        try:
            from backend.api.database import async_session
            from backend.api.models  import AlgoOrder
            from sqlalchemy          import select as _select
            from backend.api.algo.order_events import write_event
            async with async_session() as s:
                row = (await s.execute(
                    _select(AlgoOrder).where(AlgoOrder.id == order["algo_order_id"])
                )).scalar_one_or_none()
                if not row:
                    return
                row.status = "CANCELLED"
                row.attempts = int(order.get("attempts", 0))
                tag    = self._label.upper()
                side   = order.get("side") or "?"
                qty    = order.get("qty") or 0
                symbol = order.get("symbol") or "?"
                row.detail = f"[{tag}] CANCELLED by operator via MCP · {side} {qty} {symbol}"
                await s.commit()
            await write_event(
                order["algo_order_id"], "cancel",
                f"[{self._label.upper()}] operator-initiated cancel via MCP",
                payload={"source": "mcp"},
            )
        except Exception as e:
            logger.warning(
                f"[{self._label.upper()}] _safe_update_algo_order_cancel "
                f"failed (id={order.get('algo_order_id')}): {e}"
            )

    # ── Price history ────────────────────────────────────────────────

    def _capture_price_history(self, open_orders: list[dict]) -> None:
        """One (ts, ltp, bid, ask) entry per active-order symbol. Called
        after prefetch_for, so the QuoteSource cache is warm and reads are
        cheap. Symbols are deduplicated so two open orders on the same
        symbol only produce one tick in the history. Also fetches the
        underlying spot for any derivative symbol so the chart panel can
        render underlying lines next to options."""
        from backend.api.algo.derivatives import (
            parse_tradingsymbol, option_underlying_quote_key,
        )

        ts   = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                name = parsed.get("underlying")
                # One call handles every flavour — index option →
                # NSE:NIFTY 50, stock option → NSE:RELIANCE, MCX
                # commodity option → MCX:<matching-month>FUT. None
                # means "no chartable underlying for this symbol".
                ltp_key = option_underlying_quote_key(sym)
                if name and ltp_key:
                    underlyings.setdefault(name, ltp_key)

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
                # Re-hydrate the bracket-attach context — template_id +
                # product + mode + parent_order_id let the fill path
                # know whether to fire `_fire_template_attach_on_fill`
                # for this row, and what product to inherit on the
                # exit GTT legs. Sprint A fix for the recover_from_db
                # gap (templates auditor #13).
                "template_id":     r.template_id,
                "product":         r.product,
                "mode":            r.mode,
                "parent_order_id": r.parent_order_id,
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

    def _schedule_ledger_write(self, order: dict, fill_price: float) -> None:
        """Schedule a lot-ledger write for this fill, out-of-band.

        Open-vs-close intent: chase-close actions set
        `order["is_close_intent"] = True` at register-open time. A
        manual ticket / fresh agent-fire BUY or SELL leaves it false
        — those open new lots. When unset, default to OPEN (the
        common case).

        Writes happen via asyncio.create_task so the chase tick loop
        doesn't block on a DB round-trip. A write failure logs and
        drops — the AlgoOrder row already carries broker pnl so the
        per-strategy view falls back to AlgoOrder.pnl SUM for
        un-ledgered fills.

        SIM SUPPRESSION (slice 7d). Sim runs are ephemeral — operator
        runs a scenario, the engine fires fills, the simulator clears
        state on the next /clear call. Writing those fills to the
        live lot ledger would pollute real per-strategy attribution
        (a sim "filled" a 100-lot NIFTY SHORT against the operator's
        actual strategy's open lots). Skip ledger writes when this
        engine instance is the simulator. Paper + live engines
        (whose fills DO mean real positions on the broker, even if
        paper is broker-side mark-to-market) still write through.
        """
        if self._label == "sim":
            return
        strategy_id = order.get("strategy_id")
        if not strategy_id:
            return
        algo_order_id = order.get("algo_order_id")
        account = str(order.get("account") or "").strip().upper()
        symbol  = str(order.get("symbol")  or "").strip().upper()
        exchange = str(order.get("exchange") or "NFO").strip().upper()
        side    = str(order.get("side") or "BUY").upper()
        qty     = int(order.get("qty") or 0)
        is_close = bool(order.get("is_close_intent"))

        async def _do_write():
            try:
                from backend.api.database import async_session
                from backend.api.algo.lot_ledger import record_fill
                async with async_session() as sess:
                    # Pass explicit `is_close_intent` when the action
                    # set the chase-close flag; otherwise let the
                    # detector heuristic decide (BUY into open SHORT
                    # = close; SELL into open LONG = close; else
                    # OPEN). Same semantics either way for the common
                    # paper-trading case.
                    explicit_intent = True if is_close else None
                    await record_fill(
                        sess,
                        strategy_id=strategy_id,
                        algo_order_id=algo_order_id,
                        account=account, symbol=symbol, exchange=exchange,
                        side_kite=side, qty=qty,
                        fill_price=fill_price,
                        is_close_intent=explicit_intent,
                    )
                    await sess.commit()
            except Exception as exc:
                logger.warning(
                    f"PaperTradeEngine[{self._label}]: lot_ledger write failed "
                    f"for order_id={algo_order_id}: {exc}"
                )

        try:
            asyncio.get_running_loop().create_task(_do_write())
        except RuntimeError:
            # Not in an asyncio context (e.g. test harness). Skip
            # silently — the test's own setUp can call the helper
            # directly with its own session if it cares.
            pass

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
            "ts":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
        from backend.api.algo.order_events import write_event

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
                row.filled_at = datetime.now(timezone.utc)
                row.detail = (
                    f"[{tag}] {agent} {side} {qty} {symbol} · "
                    f"FILLED @₹{row.fill_price:,.2f} after {row.attempts} chase(s)"
                )
            elif kind == "unfilled":
                row.detail = (
                    f"[{tag}] {agent} {side} {qty} {symbol} · "
                    f"UNFILLED — gave up after {row.attempts} chase(s)"
                )
            # Snapshot row fields used downstream for the template-attach
            # fire — captured BEFORE leaving the session so SQLAlchemy
            # doesn't lazy-load post-commit.
            _row_template_id     = row.template_id
            _row_mode            = row.mode
            _row_parent_order_id = row.parent_order_id
            _row_product         = row.product
            _row_account         = row.account
            _row_symbol          = row.symbol
            _row_exchange        = row.exchange
            _row_side            = row.transaction_type
            _row_qty             = int(row.quantity or 0)
            await s.commit()

        # Sprint A fix — paper-engine fills must fire the template attach
        # just like live-mode postbacks do. Pre-fix, an operator who
        # placed a paper ticket with a template selected got the parent
        # order filled but ZERO bracket legs ever attached because this
        # path didn't have the hook. Live mode goes through the postback
        # handler / chase terminal which both call _fire_template_attach_
        # on_fill; paper has no postback so the engine has to fire it.
        # Only fire on terminal-fill (not modify / unfilled), only when
        # a template is set, and only for parent rows (parent_order_id
        # IS NULL — children don't get their own brackets).
        if (
            kind == "fill"
            and _row_template_id
            and _row_parent_order_id is None
            and order.get("fill_price") is not None
        ):
            try:
                from backend.api.routes.orders import _fire_template_attach_on_fill
                asyncio.create_task(_fire_template_attach_on_fill(
                    parent_row_id=order["algo_order_id"],
                    parent_account=str(_row_account),
                    parent_symbol=str(_row_symbol),
                    parent_exchange=str(_row_exchange or "NFO"),
                    parent_side=str(_row_side),
                    parent_qty=_row_qty,
                    fill_price=float(order["fill_price"]),
                    template_id=int(_row_template_id),
                    parent_product=str(_row_product or "NRML"),
                ))
            except Exception as _e:
                logger.warning(
                    f"PaperTradeEngine[{self._label}] template attach failed for "
                    f"#{order['algo_order_id']}: {_e}"
                )

        # Write the timeline event AFTER the AlgoOrder update commits so the
        # event row is never orphaned ahead of the status it describes.
        event_kind = {
            "modify":   "chase_modify",
            "fill":     "fill",
            "unfilled": "unfill",
        }.get(kind, kind)

        payload: dict = {
            "attempts": int(order.get("attempts", 0)),
            "limit_price": order.get("limit_price"),
            "account": order.get("account"),
            "symbol": symbol,
        }
        if kind == "fill":
            payload["fill_price"] = order.get("fill_price")
            payload["slippage"] = float(order.get("fill_price", 0)) - float(
                order.get("initial_price") or order.get("limit_price") or 0
            )

        # Build the message that mirrors row.detail for easy reading.
        if kind == "modify":
            msg = (f"chase #{row.attempts} limit=₹{limit:,.2f}"
                   if limit is not None else f"chase #{row.attempts}")
        elif kind == "fill":
            fp = order.get("fill_price")
            msg = (f"FILLED @₹{fp:,.2f} after {row.attempts} chase(s)"
                   if fp is not None else f"FILLED after {row.attempts} chase(s)")
        else:
            msg = f"UNFILLED — gave up after {row.attempts} chase(s)"

        await write_event(order["algo_order_id"], event_kind, msg, payload)


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
