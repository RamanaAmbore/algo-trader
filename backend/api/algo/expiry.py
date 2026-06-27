"""
Expiry-day auto-close engine.

Identifies ITM/NTM option positions on expiry day and closes them using
the chase engine before market close.

Key rules:
  - Equity (NFO): close ALL ITM + NTM options (within buffer %)
  - Commodity (MCX): close only UNBALANCED ITM legs (hedged pairs are safe)
  - Expiry may shift due to holidays — use instrument expiry date, not weekday
  - NSE and MCX have different expiry schedules and market hours
  - Re-scan every 30 min for positions that become ITM during the day

Usage:
    engine = ExpiryEngine(on_event=callback)
    await engine.run()  # blocks until market close
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Optional

from backend.shared.helpers import broker_apis
from backend.shared.helpers.connections import Connections
from backend.shared.helpers.date_time_utils import timestamp_indian, timestamp_display, is_market_open
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config, mask_column

from backend.api.algo.chase import chase_order, ChaseConfig, ChaseResult, ChaseStatus

logger = get_logger(__name__)


@dataclass
class OptionPosition:
    account: str
    tradingsymbol: str
    exchange: str
    instrument_type: str     # CE or PE
    underlying: str          # NIFTY, BANKNIFTY, CRUDE, etc.
    strike: float
    expiry: date
    quantity: int            # positive = long, negative = short
    product: str
    ltp: float = 0.0
    underlying_ltp: float = 0.0
    moneyness: str = ""      # ITM, ATM, NTM, OTM
    needs_close: bool = False
    close_reason: str = ""
    # Per-share daily theta from analytical Greeks. Drives the greedy
    # netting priority — pairs are formed from highest |theta| first.
    theta: float = 0.0
    # Signed residual quantity after the netting pass. When zero, the
    # position is fully offset by partners and needs no close action.
    residual_qty: int = 0


@dataclass
class FuturePosition:
    """Same-underlying FUT position considered as a delta-offset
    candidate for MCX option-position netting at expiry. The MCX
    Close logic pairs option-direction with opposite-delta futures
    to neutralise directional exposure that would otherwise need
    its own close ticket."""
    account: str
    tradingsymbol: str
    underlying: str
    quantity: int            # positive = long, negative = short
    product: str


@dataclass
class ExpiryState:
    status: str = "idle"     # idle, scanning, closing, done
    positions: list = field(default_factory=list)
    pending_chases: dict = field(default_factory=dict)   # symbol → ChaseResult
    closed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    last_scan: str = ""
    total_slippage: float = 0.0


class ExpiryEngine:
    def __init__(self, on_event: Callable | None = None):
        self.state = ExpiryState()
        self.on_event = on_event
        # Settings live in DB now (algo.*); YAML `algo:` block is the
        # boot-time fallback. Re-read on every engine construction so a
        # tweak via /admin/settings takes effect on the next chase run
        # without a service restart.
        from backend.shared.helpers.settings import get_int, get_float
        self._algo_cfg = config.get("algo", {})
        self._ntm_buffer = get_float(
            "algo.expiry_ntm_buffer_pct",
            float(self._algo_cfg.get("expiry_ntm_buffer_pct", 2.0)))
        self._start_offset_h = get_float(
            "algo.expiry_start_offset_hours",
            float(self._algo_cfg.get("expiry_start_offset_hours", 2)))
        self._rescan_min = get_int(
            "algo.expiry_rescan_minutes",
            int(self._algo_cfg.get("expiry_rescan_minutes", 30)))
        self._chase_cfg = ChaseConfig(
            interval_seconds=get_int(
                "algo.chase_interval_seconds",
                int(self._algo_cfg.get("chase_interval_seconds", 20))),
            aggression_step=get_float(
                "algo.aggression_step",
                float(self._algo_cfg.get("aggression_step", 0.10))),
            max_attempts=get_int(
                "algo.max_attempts",
                int(self._algo_cfg.get("max_attempts", 20))),
        )
        self._instruments_cache: dict = {}  # exchange → list of instruments

    def _emit(self, event_type: str, detail: dict = None):
        if self.on_event:
            try:
                self.on_event(event_type, detail or {})
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Instrument cache (loaded once per day per exchange)
    # ------------------------------------------------------------------

    def _load_instruments(self, exchange: str) -> list:
        """Load instruments for an exchange. Cached for the day.

        Routes through the Broker ABC so the call hops to conn_service
        when RAMBOQ_USE_CONN_SERVICE=1 — the main API holds no Kite
        session in that mode."""
        if exchange in self._instruments_cache:
            return self._instruments_cache[exchange]

        from backend.shared.brokers.registry import get_broker, all_brokers
        # Pick any loaded account — instruments are the same broker-side.
        brokers = all_brokers()
        if not brokers:
            return []
        instruments = brokers[0].instruments(exchange)
        self._instruments_cache[exchange] = instruments
        logger.info(f"Expiry: loaded {len(instruments)} instruments for {exchange}")
        return instruments

    def _get_instrument_info(self, exchange: str, tradingsymbol: str) -> dict | None:
        """Find instrument details by tradingsymbol."""
        instruments = self._load_instruments(exchange)
        for inst in instruments:
            if inst["tradingsymbol"] == tradingsymbol:
                return inst
        return None

    # ------------------------------------------------------------------
    # Position scanning
    # ------------------------------------------------------------------

    def _fetch_future_positions(self) -> list[FuturePosition]:
        """Same shape as _fetch_option_positions but filters to FUT
        legs. Used by the MCX netting pass as delta-offset partners
        for ITM commodity options."""
        raw_dfs = broker_apis.fetch_positions()
        out: list[FuturePosition] = []
        for df in raw_dfs:
            if df.empty:
                continue
            for _, row in df.iterrows():
                exchange = row.get("exchange", "")
                symbol   = row.get("tradingsymbol", "")
                qty      = int(row.get("quantity", 0))
                if qty == 0:
                    continue
                inst_exchange = "NFO" if exchange in ("NSE", "NFO") else "MCX"
                inst = self._get_instrument_info(inst_exchange, symbol)
                if not inst:
                    continue
                if inst.get("instrument_type", "") != "FUT":
                    continue
                out.append(FuturePosition(
                    account=row.get("account", ""),
                    tradingsymbol=symbol,
                    underlying=inst.get("name", ""),
                    quantity=qty,
                    product=row.get("product", "NRML"),
                ))
        return out

    def _compute_theta(self, pos: OptionPosition) -> float:
        """Per-share daily theta via Black-Scholes. Uses a flat IV of
        0.15 (commodity options near expiry — IV barely matters for
        theta when the option is deep ITM/OTM and T → 0). Returns 0
        on degenerate inputs (no spot, no strike, expired). Safe to
        call for every position; failures fall back to 0."""
        try:
            from backend.api.algo.derivatives import greeks, days_to_expiry
            if pos.underlying_ltp <= 0 or pos.strike <= 0:
                return 0.0
            close_time = (23, 30) if pos.exchange == "MCX" else (15, 30)
            d = float(days_to_expiry(pos.expiry, close_time=close_time))
            T_years = d / 365.25
            if T_years <= 0:
                return 0.0
            sigma = 0.15
            r = 0.07
            g = greeks(pos.underlying_ltp, pos.strike, T_years,
                       r, sigma, pos.instrument_type)
            return float(g.get("theta", 0.0))
        except Exception:
            return 0.0

    def _net_commodity_group(
        self,
        options: list[OptionPosition],
        futures: list[FuturePosition],
    ) -> None:
        """4-rule greedy theta-priority netting + futures offset.

        Mutates each option's `residual_qty` field in place. Rules:
          1. Long CE  + Short CE  (same opt type, opposite sign)
          2. Long PE  + Short PE
          3. Long CE  + Long PE   (both receive at settlement, no
                                   close while spot stays in band)
          4. Short CE + Short PE  (locked-in payment)
        Plus futures-as-delta-offset:
          5. Long CE  + Short FUT (Long CE delta +1 vs Short FUT -1)
          6. Long PE  + Long FUT
          7. Short CE + Long FUT
          8. Short PE + Short FUT

        Greedy: at each step, walk options by |theta| DESC, find the
        valid partner (option or future) with highest |theta| for
        options or largest |qty| for futures, net by min(|qty|).
        Mirrors the frontend Close-tab algorithm so the agent and the
        operator UI agree on what stays in the close list.
        """
        if not options:
            return

        # remaining_opt / remaining_fut tracks unmatched signed qty.
        remaining_opt: dict[int, int] = {id(o): o.quantity for o in options}
        remaining_fut: dict[int, int] = {id(f): f.quantity for f in futures}
        opts_sorted = sorted(options, key=lambda o: abs(o.theta or 0.0), reverse=True)

        def _opt_pair_ok(a: OptionPosition, b: OptionPosition) -> bool:
            aq = remaining_opt.get(id(a), 0)
            bq = remaining_opt.get(id(b), 0)
            if aq == 0 or bq == 0:
                return False
            same_type = a.instrument_type == b.instrument_type
            opp_sign  = (aq > 0) != (bq > 0)
            if same_type and opp_sign:
                return True                       # rules 1, 2
            if (not same_type) and (not opp_sign):
                return True                       # rules 3, 4
            return False

        def _fut_pair_ok(o: OptionPosition, f: FuturePosition) -> bool:
            oq = remaining_opt.get(id(o), 0)
            fq = remaining_fut.get(id(f), 0)
            if oq == 0 or fq == 0:
                return False
            # Long CE / Short PE → option delta +; needs short fut (qty < 0).
            # Short CE / Long PE → option delta −; needs long  fut (qty > 0).
            opt_long_delta = (
                (o.instrument_type == "CE" and oq > 0)
                or (o.instrument_type == "PE" and oq < 0)
            )
            if opt_long_delta and fq < 0:
                return True
            if (not opt_long_delta) and fq > 0:
                return True
            return False

        for A in opts_sorted:
            aq = remaining_opt.get(id(A), 0)
            while aq != 0:
                # Best option partner — highest |theta|.
                best_opt: Optional[OptionPosition] = None
                best_t = -1.0
                for B in opts_sorted:
                    if B is A or not _opt_pair_ok(A, B):
                        continue
                    t = abs(B.theta or 0.0)
                    if t > best_t:
                        best_opt = B
                        best_t = t
                # Best futures partner — largest |qty|.
                best_fut: Optional[FuturePosition] = None
                best_fq = 0
                for f in futures:
                    if not _fut_pair_ok(A, f):
                        continue
                    fq = abs(remaining_fut.get(id(f), 0))
                    if fq > best_fq:
                        best_fut = f
                        best_fq = fq
                if not best_opt and not best_fut:
                    break
                # Prefer option-option pair when both available — option
                # pairs cancel both legs' settlement obligation; futures
                # only cancel the delta direction.
                if best_opt is not None:
                    bq = remaining_opt[id(best_opt)]
                    net = min(abs(aq), abs(bq))
                    remaining_opt[id(A)]        = aq - net * (1 if aq > 0 else -1)
                    remaining_opt[id(best_opt)] = bq - net * (1 if bq > 0 else -1)
                else:
                    assert best_fut is not None
                    fq = remaining_fut[id(best_fut)]
                    net = min(abs(aq), abs(fq))
                    remaining_opt[id(A)]        = aq - net * (1 if aq > 0 else -1)
                    remaining_fut[id(best_fut)] = fq - net * (1 if fq > 0 else -1)
                aq = remaining_opt[id(A)]

        for o in options:
            o.residual_qty = remaining_opt.get(id(o), 0)

    def _fetch_option_positions(self) -> list[OptionPosition]:
        """Fetch all option positions across all accounts with instrument metadata."""
        import pandas as pd

        raw_dfs = broker_apis.fetch_positions()
        all_positions = []

        for df in raw_dfs:
            if df.empty:
                continue
            for _, row in df.iterrows():
                exchange = row.get("exchange", "")
                symbol = row.get("tradingsymbol", "")
                qty = int(row.get("quantity", 0))

                if qty == 0:
                    continue

                # Look up instrument to get expiry, strike, type
                inst_exchange = "NFO" if exchange in ("NSE", "NFO") else "MCX"
                inst = self._get_instrument_info(inst_exchange, symbol)
                if not inst:
                    continue

                inst_type = inst.get("instrument_type", "")
                if inst_type not in ("CE", "PE"):
                    continue  # not an option

                all_positions.append(OptionPosition(
                    account=row.get("account", ""),
                    tradingsymbol=symbol,
                    exchange=inst_exchange,
                    instrument_type=inst_type,
                    underlying=inst.get("name", ""),
                    strike=float(inst.get("strike", 0)),
                    expiry=inst.get("expiry"),
                    quantity=qty,
                    product=row.get("product", "NRML"),
                ))

        return all_positions

    def _classify_moneyness(self, pos: OptionPosition) -> str:
        """Classify option as ITM, ATM, NTM, or OTM based on underlying LTP."""
        if pos.underlying_ltp <= 0:
            return "UNKNOWN"

        if pos.instrument_type == "CE":
            diff_pct = (pos.underlying_ltp - pos.strike) / pos.strike * 100
        else:  # PE
            diff_pct = (pos.strike - pos.underlying_ltp) / pos.strike * 100

        if diff_pct > self._ntm_buffer:
            return "ITM"
        elif diff_pct > 0:
            return "NTM"  # near the money — within buffer
        elif abs(diff_pct) < 0.5:
            return "ATM"
        else:
            return "OTM"

    def _fetch_underlying_ltps(self, positions: list[OptionPosition]) -> dict:
        """Fetch LTPs for all unique underlyings.

        Routes through the Broker ABC so the call hops to conn_service
        when RAMBOQ_USE_CONN_SERVICE=1."""
        from backend.shared.brokers.registry import all_brokers
        brokers = all_brokers()
        if not brokers:
            return {}
        broker = brokers[0]

        # Map underlying name to its index/futures symbol for LTP
        # For equity indices: use NSE:NIFTY 50, NSE:NIFTY BANK, etc.
        # For commodities: use MCX:CRUDE, MCX:GOLD, etc.
        symbols = set()
        for p in positions:
            if p.exchange == "NFO":
                # Try NSE:<underlying> for index
                symbols.add(f"NSE:{p.underlying}")
            else:
                symbols.add(f"MCX:{p.underlying}")

        if not symbols:
            return {}

        try:
            data = broker.ltp(list(symbols))
            return {k.split(":")[-1]: v.get("last_price", 0) for k, v in data.items()}
        except Exception as e:
            logger.error(f"Expiry: LTP fetch failed: {e}")
            return {}

    def scan_positions(self) -> list[OptionPosition]:
        """
        Scan all option positions and identify those expiring today
        that need closing.

        Rules (mirrors the frontend /admin/options Close tab):
          • Equity (NFO): every ITM + NTM contract closes — Zerodha
            doesn't net-settle pairs; STT on ITM longs and physical
            settlement is the trap.
          • Commodity (MCX): per (account, underlying, expiry) group,
            apply the 4-rule greedy theta-priority netting (long-CE ↔
            short-CE, long-PE ↔ short-PE, long-CE ↔ long-PE, short-CE
            ↔ short-PE) PLUS same-underlying FUT positions as delta-
            offset partners (Long-CE ↔ Short-FUT, Long-PE ↔ Long-FUT,
            etc.). Residual (non-zero) qty after netting is what
            needs closing.
        """
        today = timestamp_indian().date()
        self.state.status = "scanning"
        self._emit("scan_start", {"date": str(today)})

        # Fetch options + futures (futures used as MCX offset partners).
        all_opts = self._fetch_option_positions()
        all_futs = self._fetch_future_positions()
        logger.info(f"Expiry: found {len(all_opts)} option, {len(all_futs)} future positions")

        # Filter to today's expiry (futures filtered by underlying only —
        # an MCX future on the same underlying offsets options even if
        # the future itself expires on a different date).
        expiring = [p for p in all_opts if p.expiry == today]
        logger.info(f"Expiry: {len(expiring)} positions expiring today ({today})")

        if not expiring:
            self.state.status = "idle"
            self._emit("scan_complete", {"count": 0})
            return []

        # Fetch underlying LTPs.
        ltps = self._fetch_underlying_ltps(expiring)

        # Classify moneyness + compute theta for the netting priority.
        for p in expiring:
            p.underlying_ltp = ltps.get(p.underlying, 0)
            p.moneyness = self._classify_moneyness(p)
            p.theta = self._compute_theta(p)
            p.residual_qty = p.quantity   # default before netting

        # Equity (NFO) — ALL ITM + NTM closes, no netting exception.
        for p in expiring:
            if p.exchange == "NFO" and p.moneyness in ("ITM", "NTM"):
                p.needs_close = True
                p.close_reason = f"Equity {p.moneyness} — must close before expiry"

        # Commodity (MCX) — 4-rule + futures greedy theta netting per
        # (account, underlying, expiry). Anything with non-zero
        # residual qty after the netting pass needs closing.
        mcx_in_money = [
            p for p in expiring
            if p.exchange == "MCX" and p.moneyness in ("ITM", "NTM")
        ]
        groups: dict[tuple[str, str, date], list[OptionPosition]] = {}
        for p in mcx_in_money:
            groups.setdefault((p.account, p.underlying, p.expiry), []).append(p)

        for (acct, underlying, _expiry), group in groups.items():
            # Same-account futures on the same underlying are offset
            # candidates. Different-account futures can't net (each
            # account settles independently).
            futs = [
                f for f in all_futs
                if f.account == acct and f.underlying == underlying
            ]
            self._net_commodity_group(group, futs)
            for p in group:
                if p.residual_qty != 0:
                    p.needs_close = True
                    p.close_reason = (
                        f"MCX unhedged {p.moneyness} after 4-rule netting "
                        f"(residual qty {p.residual_qty:+d}; "
                        f"theta={p.theta:.3f})"
                    )
                else:
                    logger.info(
                        f"Expiry: MCX {p.tradingsymbol} ({acct}) fully netted, "
                        f"no close needed."
                    )

        to_close = [p for p in expiring if p.needs_close]
        self.state.positions = expiring
        self.state.last_scan = timestamp_display()

        self._emit("scan_complete", {
            "total_expiring": len(expiring),
            "to_close": len(to_close),
            "positions": [
                {"account": p.account, "symbol": p.tradingsymbol,
                 "exchange": p.exchange, "qty": p.quantity,
                 "moneyness": p.moneyness, "strike": p.strike,
                 "underlying_ltp": p.underlying_ltp}
                for p in to_close
            ],
        })

        logger.info(f"Expiry: {len(to_close)} positions to close")
        return to_close

    # ------------------------------------------------------------------
    # Closing orchestration
    # ------------------------------------------------------------------

    async def close_positions(self, positions: list[OptionPosition]):
        """Chase-close all flagged positions concurrently. Uses the
        RESIDUAL qty so an MCX leg that paired against a partner only
        gets its un-netted remainder closed. residual_qty defaults to
        the gross quantity in scan_positions, so NFO equity rows
        (which skip the netting pass) close their full position."""
        self.state.status = "closing"

        tasks = []
        for pos in positions:
            effective = pos.residual_qty
            if effective == 0:
                continue   # fully netted; nothing to close
            # Determine transaction type: close long → SELL, close short → BUY
            txn = "SELL" if effective > 0 else "BUY"
            qty = abs(effective)

            cfg = ChaseConfig(
                interval_seconds=self._chase_cfg.interval_seconds,
                aggression_step=self._chase_cfg.aggression_step,
                max_attempts=self._chase_cfg.max_attempts,
                exchange=pos.exchange,
                product=pos.product,
            )

            async def _chase_one(p=pos, t=txn, q=qty, c=cfg):
                result = await chase_order(
                    account=p.account,
                    symbol=p.tradingsymbol,
                    transaction_type=t,
                    quantity=q,
                    cfg=c,
                    on_event=self.on_event,
                )
                if result.status == ChaseStatus.FILLED:
                    self.state.closed.append(result)
                    self.state.total_slippage += result.slippage
                else:
                    self.state.failed.append(result)
                return result

            self.state.pending_chases[pos.tradingsymbol] = ChaseResult(
                account=pos.account, symbol=pos.tradingsymbol,
                transaction_type=txn, quantity=qty,
                status=ChaseStatus.PENDING,
            )
            tasks.append(_chase_one())

        # Run all chases concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Expiry: chase exception: {r}")
            elif isinstance(r, ChaseResult):
                # Remove from pending
                self.state.pending_chases.pop(r.symbol, None)

        self.state.status = "done"
        self._emit("close_complete", {
            "closed": len(self.state.closed),
            "failed": len(self.state.failed),
            "total_slippage": self.state.total_slippage,
        })

    # ------------------------------------------------------------------
    # Main run loop (called by background task on expiry days)
    # ------------------------------------------------------------------

    async def run(self):
        """
        Full expiry-day workflow:
        1. Morning scan at 09:15
        2. Wait until T-2h before close
        3. Start closing
        4. Re-scan every 30 min for new ITM positions
        5. Continue until all closed or market close
        """
        now = timestamp_indian()
        today = now.date()

        # Determine market close times per exchange
        segments = config.get("market_segments", {})
        equity_close = dtime(15, 30)
        mcx_close = dtime(23, 30)
        for name, seg in segments.items():
            h, m = map(int, seg.get("hours_end", "15:30").split(":"))
            if name == "equity":
                equity_close = dtime(h, m)
            elif name == "commodity":
                mcx_close = dtime(h, m)

        # Morning scan
        logger.info("Expiry: starting morning scan")
        to_close = self.scan_positions()

        if not to_close:
            logger.info("Expiry: no positions need closing today")
            self._emit("no_positions", {"date": str(today)})
            return

        # Send morning alert
        self._emit("morning_alert", {
            "count": len(to_close),
            "positions": [
                f"{p.account} {p.tradingsymbol} qty={p.quantity} {p.moneyness}"
                for p in to_close
            ],
        })

        # Determine when to start closing based on exchange
        nfo_positions = [p for p in to_close if p.exchange == "NFO"]
        mcx_positions = [p for p in to_close if p.exchange == "MCX"]

        # Close NFO positions (start 2h before equity close)
        if nfo_positions:
            nfo_start = (datetime.combine(today, equity_close) -
                         timedelta(hours=self._start_offset_h)).time()
            now_t = timestamp_indian().time()
            if now_t < nfo_start:
                wait = (datetime.combine(today, nfo_start) -
                        datetime.combine(today, now_t)).total_seconds()
                logger.info(f"Expiry: waiting {wait/60:.0f} min until NFO close phase starts at {nfo_start}")
                self._emit("waiting", {"exchange": "NFO", "start_time": str(nfo_start)})
                await asyncio.sleep(max(wait, 0))

            logger.info(f"Expiry: starting NFO close for {len(nfo_positions)} positions")
            await self.close_positions(nfo_positions)

        # Close MCX positions (start 2h before commodity close)
        if mcx_positions:
            mcx_start = (datetime.combine(today, mcx_close) -
                         timedelta(hours=self._start_offset_h)).time()
            now_t = timestamp_indian().time()
            if now_t < mcx_start:
                wait = (datetime.combine(today, mcx_start) -
                        datetime.combine(today, now_t)).total_seconds()
                logger.info(f"Expiry: waiting {wait/60:.0f} min until MCX close phase starts at {mcx_start}")
                self._emit("waiting", {"exchange": "MCX", "start_time": str(mcx_start)})
                await asyncio.sleep(max(wait, 0))

            # Re-scan MCX positions (market may have moved)
            logger.info("Expiry: re-scanning MCX positions before closing")
            fresh = self.scan_positions()
            mcx_fresh = [p for p in fresh if p.exchange == "MCX" and p.needs_close]
            if mcx_fresh:
                logger.info(f"Expiry: starting MCX close for {len(mcx_fresh)} positions")
                await self.close_positions(mcx_fresh)

        # Summary
        logger.info(f"Expiry: complete — closed {len(self.state.closed)}, "
                     f"failed {len(self.state.failed)}, "
                     f"slippage ₹{self.state.total_slippage:.2f}")
        self._emit("expiry_complete", {
            "closed": len(self.state.closed),
            "failed": len(self.state.failed),
            "total_slippage": self.state.total_slippage,
        })
