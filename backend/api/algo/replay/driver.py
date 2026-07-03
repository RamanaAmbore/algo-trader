"""
ReplayDriver — feeds historical Kite OHLCV candles through the agent engine
at an accelerated playback rate.

Follows the SimDriver singleton pattern: one replay per process, branch-gated
via is_enabled('replay'), auto-stops after a configurable wall-clock limit.

The replay flow:
  1. start() — fetches all historical data up-front (one kite.historical_data
     call per symbol), builds the per-symbol candle lists, initialises the
     PaperTradeEngine fed by HistoricalQuoteSource.
  2. _run_loop() — advances one candle per tick, updates the quote source,
     builds a context dict, calls run_cycle(). Pauses rate_ms between ticks.
  3. Results accumulate in _results (agent fires + paper orders).
  4. stop() — cancels the loop, returns results.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

TICK_LOG_LIMIT = 200
PRICE_HISTORY_LIMIT = 600


class ReplayDriver:
    """Module-level singleton — one replay per process."""

    def __init__(self) -> None:
        self.active: bool = False
        self.started_at: Optional[str] = None
        self.tick_index: int = 0
        self.total_ticks: int = 0
        self.rate_ms: int = 100
        self.date_from: Optional[date] = None
        self.date_to: Optional[date] = None
        self.interval: str = "5minute"
        self.agent_ids: Optional[list[int]] = None

        # Per-symbol candle lists: {symbol: [candle_dict, ...]}
        self._candles: dict[str, list[dict]] = {}
        # Current candle index per symbol
        self._candle_idx: dict[str, int] = {}
        # Instrument token cache: {symbol: int}
        self._instrument_tokens: dict[str, int] = {}

        # Results
        self._results: list[dict] = []
        self._tick_log: deque = deque(maxlen=TICK_LOG_LIMIT)
        self._price_history: dict[str, deque] = {}

        # Engine
        self._paper = None
        self._quote_source = None
        self._loop_task: Optional[asyncio.Task] = None

    def start(
        self,
        symbols: list[str],
        date_from: date,
        date_to: date,
        interval: str = "5minute",
        rate_ms: int = 100,
        agent_ids: Optional[list[int]] = None,
        spread_pct: float = 0.10,
    ) -> dict:
        """
        Fetch historical data and kick off the replay loop.
        Raises on bad input or if already active.
        """
        from backend.shared.helpers.utils import is_enabled

        if not is_enabled("replay"):
            from backend.shared.helpers.utils import config
            branch = config.get("deploy_branch", "dev")
            section = "cap_in_prod" if branch == "main" else "cap_in_dev"
            raise RuntimeError(
                f"Replay is disabled. Set {section}.replay: True "
                f"in backend_config.yaml (branch: {branch})."
            )

        if self.active:
            raise RuntimeError("A replay is already running. Stop it first.")

        if not symbols:
            raise ValueError("At least one symbol is required.")

        if date_from > date_to:
            raise ValueError("date_from must be <= date_to.")

        from backend.shared.helpers.settings import get_int
        max_days = get_int("replay.max_days", 60)
        if (date_to - date_from).days > max_days:
            raise ValueError(f"Date range exceeds {max_days} days.")

        # Reset state
        self.active = True
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.tick_index = 0
        self.date_from = date_from
        self.date_to = date_to
        self.interval = interval
        self.rate_ms = max(10, rate_ms)
        self.agent_ids = agent_ids
        self._results = []
        self._tick_log = deque(maxlen=TICK_LOG_LIMIT)
        self._price_history = {}
        self._candles = {}
        self._candle_idx = {}

        # Fetch historical data
        self._fetch_all_history(symbols, date_from, date_to, interval)

        if not self._candles:
            self.active = False
            raise RuntimeError("No historical data found for the given symbols/dates.")

        # Total ticks = max candle count across symbols
        self.total_ticks = max(len(c) for c in self._candles.values())

        # Initialize quote source and paper engine
        from backend.api.algo.quote.historical import HistoricalQuoteSource
        from backend.api.algo.paper import PaperTradeEngine

        self._quote_source = HistoricalQuoteSource(spread_pct=spread_pct)
        self._paper = PaperTradeEngine(
            quote_source=self._quote_source,
            label="replay",
        )

        # Start the async loop
        try:
            loop = asyncio.get_running_loop()
            self._loop_task = loop.create_task(self._run_loop())
        except RuntimeError:
            pass

        logger.info(
            f"[REPLAY] Started: {len(symbols)} symbols, "
            f"{date_from} → {date_to}, {self.total_ticks} ticks, "
            f"rate={rate_ms}ms, interval={interval}"
        )

        return self.snapshot()

    def stop(self) -> dict:
        """Stop the replay and return final results."""
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        self.active = False
        logger.info(f"[REPLAY] Stopped at tick {self.tick_index}/{self.total_ticks}")
        return self.snapshot()

    def snapshot(self) -> dict:
        """Current state for the UI."""
        return {
            "active": self.active,
            "started_at": self.started_at,
            "tick_index": self.tick_index,
            "total_ticks": self.total_ticks,
            "rate_ms": self.rate_ms,
            "date_from": str(self.date_from) if self.date_from else None,
            "date_to": str(self.date_to) if self.date_to else None,
            "interval": self.interval,
            "agent_ids": self.agent_ids,
            "symbols": sorted(self._candles.keys()),
            "results_count": len(self._results),
        }

    def results(self) -> list[dict]:
        """Agent fire results from the replay run."""
        return list(self._results)

    # ── Internal ─────────────────────────────────────────────────────

    def _fetch_all_history(
        self,
        symbols: list[str],
        date_from: date,
        date_to: date,
        interval: str,
    ) -> None:
        """Batch-fetch historical candles for all symbols."""
        from backend.brokers.registry import get_market_data_broker

        try:
            broker = get_market_data_broker()
        except Exception as e:
            raise RuntimeError(f"Cannot get price broker: {e}")

        # Resolve instrument tokens
        self._resolve_instruments(symbols, broker)

        for sym in symbols:
            token = self._instrument_tokens.get(sym)
            if token is None:
                logger.warning(f"[REPLAY] No instrument token for {sym}, skipping")
                continue
            try:
                candles = broker.historical_data(
                    instrument_token=token,
                    from_date=date_from,
                    to_date=date_to,
                    interval=interval,
                )
                if candles:
                    self._candles[sym] = candles
                    self._candle_idx[sym] = 0
                    logger.info(f"[REPLAY] {sym}: {len(candles)} candles loaded")
            except Exception as e:
                logger.warning(f"[REPLAY] Failed to fetch history for {sym}: {e}")

    def _resolve_instruments(self, symbols: list[str], broker) -> None:
        """Look up instrument tokens from Kite's instruments dump."""
        try:
            # Try NFO first (most F&O symbols), then NSE
            for exchange in ["NFO", "NSE", "MCX", "BSE"]:
                remaining = [s for s in symbols if s not in self._instrument_tokens]
                if not remaining:
                    break
                try:
                    instruments = broker.instruments(exchange)
                except Exception:
                    continue
                inst_map = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
                for sym in remaining:
                    if sym in inst_map:
                        self._instrument_tokens[sym] = inst_map[sym]
        except Exception as e:
            logger.warning(f"[REPLAY] Instrument resolution failed: {e}")

    async def _run_loop(self) -> None:
        """Advance one candle per tick until done or stopped."""
        from backend.shared.helpers.settings import get_int

        auto_stop_min = get_int("replay.auto_stop_minutes", 30)
        start_time = datetime.now()
        interval_s = self.rate_ms / 1000.0

        try:
            while self.active and self.tick_index < self.total_ticks:
                # Auto-stop guard
                elapsed = (datetime.now() - start_time).total_seconds() / 60.0
                if elapsed > auto_stop_min:
                    logger.warning(f"[REPLAY] Auto-stopped after {auto_stop_min} min")
                    break

                self._apply_next_tick()
                self.tick_index += 1
                await asyncio.sleep(interval_s)

        except asyncio.CancelledError:
            logger.info("[REPLAY] Loop cancelled")
        except Exception as e:
            logger.error(f"[REPLAY] Loop error: {e}")
        finally:
            self.active = False
            logger.info(
                f"[REPLAY] Complete: {self.tick_index}/{self.total_ticks} ticks, "
                f"{len(self._results)} agent fires"
            )

    def _apply_next_tick(self) -> None:
        """Advance all symbols by one candle and run the agent engine."""
        current_candles: dict[str, dict] = {}
        ts = None

        for sym, candles in self._candles.items():
            idx = self._candle_idx.get(sym, 0)
            if idx < len(candles):
                candle = candles[idx]
                current_candles[sym] = candle
                self._candle_idx[sym] = idx + 1
                # Capture for chart
                candle_ts = candle.get("date")
                if candle_ts:
                    ts = str(candle_ts)
                self._capture_price(sym, candle, ts)

        if not current_candles:
            return

        # Update quote source
        if self._quote_source:
            self._quote_source.set_candles(current_candles)

        # Run paper engine step (process any open chase orders)
        if self._paper:
            self._paper.step()

        # Build positions from candle data and run agent engine
        self._run_agent_cycle(current_candles, ts)

    def _capture_price(self, sym: str, candle: dict, ts: str | None) -> None:
        """Record price history for charts."""
        close = candle.get("close")
        if close is None:
            return
        ts = ts or datetime.now().isoformat(timespec="seconds")
        buf = self._price_history.get(sym)
        if buf is None:
            buf = deque(maxlen=PRICE_HISTORY_LIMIT)
            self._price_history[sym] = buf
        buf.append({
            "ts": ts,
            "ltp": float(close),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "volume": candle.get("volume"),
        })

    def _run_agent_cycle(self, current_candles: dict[str, dict], ts: str | None) -> None:
        """Build a synthetic context and run the agent engine."""
        import pandas as pd
        from backend.shared.helpers.summarise import summarise_positions

        # Build a positions DataFrame from candle data
        rows = []
        for sym, candle in current_candles.items():
            close = candle.get("close")
            if close is None:
                continue
            rows.append({
                "tradingsymbol": sym,
                "account": "REPLAY",
                "exchange": "NFO",
                "quantity": 0,
                "average_price": close,
                "last_price": close,
                "close_price": candle.get("open", close),
                "pnl": 0,
                "day_change": (close - candle.get("open", close)) if candle.get("open") else 0,
            })

        if not rows:
            return

        df_positions = pd.DataFrame(rows)
        sum_positions = summarise_positions(df_positions)
        sum_holdings = pd.DataFrame()
        df_margins = pd.DataFrame()

        # Log tick
        self._tick_log.append({
            "ts": ts or datetime.now().isoformat(timespec="seconds"),
            "tick_index": self.tick_index,
            "kind": "tick",
            "symbols": list(current_candles.keys()),
        })

        # Run agent engine
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_run_cycle(
                sum_holdings, sum_positions, df_margins, ts
            ))
        except RuntimeError:
            pass

    async def _async_run_cycle(
        self, sum_holdings, sum_positions, df_margins, ts
    ) -> None:
        """Async wrapper for run_cycle."""
        from backend.api.algo.agent_engine import run_cycle
        from backend.shared.helpers.date_time_utils import timestamp_display

        ctx = {
            "sum_holdings": sum_holdings,
            "sum_positions": sum_positions,
            "df_margins": df_margins,
            "now": ts or datetime.now().isoformat(),
            "ist_display": ts or timestamp_display(),
            "alert_state": {"replay_mode": True},
            "sim_mode": False,
            "replay_mode": True,
        }

        results_before = len(self._results)

        async def on_fire(agent, event_type, detail, **kwargs):
            self._results.append({
                "tick_index": self.tick_index,
                "timestamp": ts,
                "agent_slug": getattr(agent, "slug", str(agent)),
                "agent_name": getattr(agent, "name", ""),
                "event_type": event_type,
                "detail": detail,
            })

        try:
            await run_cycle(
                ctx,
                broadcast_fn=lambda evt: None,
                only_agent_ids=self.agent_ids,
                bypass_schedule=True,
            )
        except Exception as e:
            logger.error(f"[REPLAY] run_cycle failed at tick {self.tick_index}: {e}")


# ═════════════════════════════════════════════════════════════════════════
#  Module singleton
# ═════════════════════════════════════════════════════════════════════════

_driver: Optional[ReplayDriver] = None


def get_replay_driver() -> ReplayDriver:
    global _driver
    if _driver is None:
        _driver = ReplayDriver()
    return _driver
