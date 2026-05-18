"""
Market-price simulation driver for /api/simulator/*.

**Positions-only simulator.** Holdings are intentionally not part of the
simulation — intraday risk lives in F&O positions + fund negatives, which
is what this exercises end-to-end. Agents checking holdings metrics
(day_pct, day_rate_abs, day_rate_pct) run against live production data
only; the synthesizer refuses to build a scenario for them.

Design goals
  1. **Price-driver, not aggregate-driver.** Each tick moves per-symbol
     `last_price` on positions; `pnl` is recomputed from it. The agent
     engine sees `sum_positions` + `df_margins` in the same shape as the
     live path. `sum_holdings` is always an empty frame.
  2. **No code branches in the hot path.** The agent engine, dispatcher and
     action handlers are unaware that data came from here — they read
     `sim_mode` off `alert_state` and prepend `[SIM]` where needed.
  3. **Branch-gated.** `assert_enabled()` requires `cap_in_<branch>.simulator
     = True`. Default shipped values: dev on, prod off. Auto-stops after 30
     minutes so a forgotten sim can't bleed forever.
  4. **Deterministic replay.** `step()` applies exactly one tick; `start()`
     runs the scenario at a user-set cadence via asyncio. `random_walk`
     moves accept a `seed` so the tick stream is reproducible.

The driver is a module-level singleton because only one simulation can run
per process — concurrent sims would race for the same `_sim_alert_state`
and emit confusing alerts.
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import random
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config

logger = get_logger(__name__)

SCENARIOS_PATH = Path(__file__).parent / "scenarios.yaml"
TICK_LOG_LIMIT = 200
# Per-symbol price history for the chart panel. Cap at 600 entries — at the
# default 2 s tick rate that's ~20 minutes of history per symbol, which is
# the relevant window for an in-flight chase. Auto-trimmed by deque maxlen
# so there's no manual cleanup; the buffer wipes on every start().
PRICE_HISTORY_LIMIT = 600


# Market-state presets the simulator exposes to scenarios + the UI. Each
# preset maps to the segment-flag + minutes-since-open/close overrides
# `_build_context()` will respect, so time-aware agents (rate rules with
# baseline gates, minutes_until_close conditions, expiry rules) fire
# against a simulated clock instead of wall-clock time.
MARKET_STATE_PRESETS: dict[str, dict] = {
    "pre_open":     {"nse_open": False, "mcx_open": False,
                     "minutes_since_nse_open": 0,   "minutes_since_nse_close": 0,
                     "minutes_since_mcx_open": 0,   "minutes_since_mcx_close": 0,
                     "is_expiry_day": False},
    "at_open":      {"nse_open": True,  "mcx_open": True,
                     "minutes_since_nse_open": 1,   "minutes_since_nse_close": 0,
                     "minutes_since_mcx_open": 1,   "minutes_since_mcx_close": 0,
                     "is_expiry_day": False},
    "mid_session":  {"nse_open": True,  "mcx_open": True,
                     "minutes_since_nse_open": 180, "minutes_since_nse_close": 0,
                     "minutes_since_mcx_open": 180, "minutes_since_mcx_close": 0,
                     "is_expiry_day": False},
    "pre_close":    {"nse_open": True,  "mcx_open": True,
                     "minutes_since_nse_open": 360, "minutes_since_nse_close": 0,
                     "minutes_since_mcx_open": 360, "minutes_since_mcx_close": 0,
                     "is_expiry_day": False},
    "at_close":     {"nse_open": False, "mcx_open": True,
                     "minutes_since_nse_open": 375, "minutes_since_nse_close": 0,
                     "minutes_since_mcx_open": 375, "minutes_since_mcx_close": 0,
                     "is_expiry_day": False},
    "post_close":   {"nse_open": False, "mcx_open": False,
                     "minutes_since_nse_open": 375, "minutes_since_nse_close": 60,
                     "minutes_since_mcx_open": 375, "minutes_since_mcx_close": 60,
                     "is_expiry_day": False},
    # Thursday expiry scenario — mid-session on the day the weekly options
    # settle. Flips is_expiry_day so expiry auto-close agents engage.
    "expiry_day":   {"nse_open": True,  "mcx_open": True,
                     "minutes_since_nse_open": 240, "minutes_since_nse_close": 0,
                     "minutes_since_mcx_open": 240, "minutes_since_mcx_close": 0,
                     "is_expiry_day": True},
}


def _resolve_market_state(spec: dict | None) -> dict:
    """
    Turn a scenario's `market_state` block (or a runtime UI override)
    into a flat dict of overrides consumable by _build_context. Unknown
    presets fall back to mid_session and log a warning.
    """
    if not spec:
        return dict(MARKET_STATE_PRESETS["mid_session"])
    out: dict = {}
    preset = spec.get("preset") if isinstance(spec, dict) else None
    if preset:
        if preset not in MARKET_STATE_PRESETS:
            logger.warning(
                f"[SIM] Unknown market_state preset '{preset}' — "
                f"using mid_session. Valid: {list(MARKET_STATE_PRESETS)}"
            )
            preset = "mid_session"
        out.update(MARKET_STATE_PRESETS[preset])
    # Explicit fields in the spec override the preset (e.g. "use pre_close
    # but flip is_expiry_day").
    for k, v in (spec or {}).items():
        if k == "preset":
            continue
        out[k] = v
    return out


def _auto_stop_after() -> timedelta:
    """Read auto-stop window from DB settings each time (falls back to 30 min)."""
    from backend.shared.helpers.settings import get_int
    return timedelta(minutes=get_int("simulator.auto_stop_minutes", 30))


def _positions_every_default() -> int:
    from backend.shared.helpers.settings import get_int
    return get_int("simulator.positions_every_n_ticks", 1)


# Compatibility shim for callers that still reference the old name.
POSITIONS_UPDATE_EVERY_DEFAULT = 1
AUTO_STOP_AFTER                = timedelta(minutes=30)


class SimGuardError(RuntimeError):
    """Raised when an operator tries to run the sim in a forbidden context."""


def assert_enabled() -> None:
    """
    Branch-aware simulator gate.

    The simulator runs only when the capability flag for the current branch
    allows it. Default shipping values:
      - cap_in_prod.simulator: False  (prod won't run sim by default)
      - cap_in_dev.simulator:  True   (dev runs sim by default)
    """
    from backend.shared.helpers.utils import is_enabled
    if is_enabled("simulator"):
        return
    branch = config.get("deploy_branch", "dev")
    section = "cap_in_prod" if branch == "main" else "cap_in_dev"
    raise SimGuardError(
        f"Market simulator is disabled. Set {section}.simulator: True in "
        f"backend_config.yaml (branch: {branch})."
    )


# Back-compat alias used by older callers.
assert_dev = assert_enabled


def load_scenarios() -> list[dict]:
    """Load scenarios.yaml. Empty list if the file is missing or malformed."""
    if not SCENARIOS_PATH.exists():
        return []
    try:
        with SCENARIOS_PATH.open() as fh:
            data = yaml.safe_load(fh) or []
        return [s for s in data if isinstance(s, dict) and s.get("slug")]
    except Exception as e:
        logger.error(f"Sim: failed to load scenarios.yaml: {e}")
        return []


def get_scenario(slug: str) -> Optional[dict]:
    for s in load_scenarios():
        if s.get("slug") == slug:
            return s
    return None


# ═════════════════════════════════════════════════════════════════════════
#  Per-row math — LTP changes drive every derived field.
# ═════════════════════════════════════════════════════════════════════════

def _recompute_position_row(row: dict, spread_pct: float = 0.0) -> None:
    """
    Mutate a positions row in-place: keep `pnl` / `bid` / `ask` consistent
    with the current `last_price`. Real Kite `pnl` includes realised + m2m;
    for the simulator we use the simple model
    `(last_price - average_price) × quantity` because that's what the
    loss-* agents read. `bid` / `ask` are derived from `spread_pct` (a
    decimal fraction — 0.001 = 0.10% spread) so paper-trade limit prices
    can pick the correct side of the market.
    """
    qty = float(row.get("quantity")       or 0)
    avg = float(row.get("average_price")  or 0)
    lp  = float(row.get("last_price")     or 0)
    row["pnl"] = (lp - avg) * qty
    half = max(0.0, float(spread_pct)) / 2.0
    row["bid"] = lp * (1.0 - half) if lp else 0.0
    row["ask"] = lp * (1.0 + half) if lp else 0.0


def _recompute_holding_row(row: dict) -> None:
    """
    Mirror of `_recompute_position_row` for the holdings section.
    Kite holdings rows carry `quantity`, `average_price`, `last_price`,
    `close_price`, `pnl`, `day_change`, `day_change_percentage`. The
    loss agents that gate on holdings metrics (`day_pct`,
    `day_rate_abs`, `day_rate_pct`) read `day_change` /
    `day_change_percentage`, so we keep those + `pnl` in lockstep
    with the current `last_price`.
    """
    qty   = float(row.get("quantity")       or 0)
    avg   = float(row.get("average_price")  or 0)
    lp    = float(row.get("last_price")     or 0)
    close = float(row.get("close_price")    or row.get("close")    or 0)
    row["pnl"] = (lp - avg) * qty
    if close > 0:
        day_change = (lp - close) * qty
        day_pct    = (lp - close) / close
    else:
        day_change = 0.0
        day_pct    = 0.0
    row["day_change"]            = day_change
    row["day_change_percentage"] = day_pct


# ═════════════════════════════════════════════════════════════════════════
#  Custom-positions input — operators add ad-hoc symbols via the sim UI
# ═════════════════════════════════════════════════════════════════════════

def _normalise_custom_positions(rows: list[dict]) -> list[dict]:
    """
    Validate + fill defaults for rows posted from the simulator's "Custom
    positions" UI. Returns a list of position dicts in the same shape
    `broker.fetch_positions` produces, ready to drop into `_positions_rows`.

    Required fields: `tradingsymbol`, `quantity`, `last_price`.
    Inferred defaults:
      - `account`        → "ZG####" (a generic synthetic account; doesn't
                           need to exist in secrets.yaml — the engine reads
                           it as a string label only).
      - `average_price`  → `last_price` (so pnl starts at 0; the operator
                           can override if they want immediate P&L).
      - `exchange`       → "NFO" for parseable F&O, "NSE" otherwise.
      - `multiplier`     → 1 (Kite multiplier; only matters for legacy
                           pnl computations).
    Bad rows (missing tradingsymbol or quantity) are silently skipped so a
    half-filled UI form doesn't blow up the sim.
    """
    from backend.api.algo.derivatives import parse_tradingsymbol

    out: list[dict] = []
    for raw in (rows or []):
        sym = str((raw or {}).get("tradingsymbol") or "").strip().upper()
        if not sym:
            continue
        try:
            qty = int(raw.get("quantity"))
        except (TypeError, ValueError):
            continue
        try:
            ltp = float(raw.get("last_price"))
        except (TypeError, ValueError):
            continue
        avg = raw.get("average_price")
        try:
            avg = float(avg) if avg is not None and str(avg) != "" else ltp
        except (TypeError, ValueError):
            avg = ltp
        # Exchange inference — F&O symbols (parser hits) default to NFO,
        # everything else to NSE. Operators can override by typing it
        # explicitly into the row.
        exch = str(raw.get("exchange") or "").strip().upper()
        if not exch:
            exch = "NFO" if parse_tradingsymbol(sym) else "NSE"
        out.append({
            "account":        str(raw.get("account") or "ZG####"),
            "tradingsymbol":  sym,
            "exchange":       exch,
            "quantity":       qty,
            "last_price":     ltp,
            "average_price":  avg,
            "multiplier":     int(raw.get("multiplier") or 1),
            "product":        str(raw.get("product") or "MIS"),
        })
    return out


# ═════════════════════════════════════════════════════════════════════════
#  Glob scope matching — section.account.tradingsymbol
# ═════════════════════════════════════════════════════════════════════════

async def _fetch_user_watchlist_rows(user_id: int) -> list[dict]:
    """Fetch every watchlist item the user owns + batch-quote them.
    Builds zero-qty rows tagged section='watchlist', account=<list name>
    ready to drop into SimDriver._watchlist_rows."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import Watchlist, WatchlistItem
    from backend.api.routes.watchlist import _fetch_quotes

    async with async_session() as session:
        wl_row = await session.execute(
            select(Watchlist.id, Watchlist.name)
            .where(Watchlist.user_id == user_id)
        )
        wls = list(wl_row.all())
        if not wls:
            return []
        wl_map = {wid: wname for (wid, wname) in wls}
        items_row = await session.execute(
            select(WatchlistItem)
            .where(WatchlistItem.watchlist_id.in_(list(wl_map.keys())))
            .order_by(WatchlistItem.watchlist_id, WatchlistItem.sort_order)
        )
        items = list(items_row.scalars().all())

    if not items:
        return []
    quotes = await _fetch_quotes(items)
    qmap = {q.item_id: q for q in quotes}

    rows: list[dict] = []
    for it in items:
        q = qmap.get(it.id)
        ltp = float(q.ltp if q else 0.0) or 0.0
        rows.append({
            "section":       "watchlist",
            "account":       wl_map.get(it.watchlist_id, "Default"),
            "tradingsymbol": (q.quote_symbol if q else it.tradingsymbol),
            "exchange":      it.exchange,
            "quantity":      0,
            "average_price": 0.0,
            "last_price":    ltp,
            "close_price":   float(q.close if (q and q.close) else 0.0),
            "bid":           float(q.bid)  if (q and q.bid)  else 0.0,
            "ask":           float(q.ask)  if (q and q.ask)  else 0.0,
            "pnl":           0.0,
            "day_change":    float(q.change     if q else 0.0),
            "day_change_percentage": float(q.change_pct if q else 0.0) / 100.0,
            "_watchlist_id": it.watchlist_id,
            "_watchlist_item_id": it.id,
        })
    return rows


# Direct-LTP move primitives. Realistic market simulation should use
# `underlying_*` instead so options reprice off spot via Black-Scholes.
# These are kept for the per-agent synthesizer (which uses target_pnl
# to force specific thresholds) and for testing non-derivative legs.
_UNREALISTIC_DERIV_MOVES = {"pct", "abs", "random_walk", "target_pnl"}


def _warn_unrealistic_moves(scen: dict, slug: str) -> None:
    """
    Emit one warning per (slug, move_type) when a YAML scenario applies
    a direct-LTP primitive to a positions-scope move. Derivatives in
    that scope bypass Black-Scholes — option/future prices move
    independently of the underlying spot, which is unrealistic for any
    book that has F&O.
    """
    seen: set[str] = set()
    for tick in (scen.get("ticks") or []):
        for move in (tick.get("moves") or []):
            mtype = (move.get("type") or "").lower()
            scope = move.get("scope") or ""
            if mtype in _UNREALISTIC_DERIV_MOVES and scope.startswith("positions."):
                if mtype in seen:
                    continue
                seen.add(mtype)
                logger.warning(
                    f"[SIM] scenario '{slug}' uses '{mtype}' over '{scope}' — "
                    f"direct-LTP move on positions bypasses Black-Scholes; "
                    f"options/futures will tick without the underlying moving. "
                    f"Use 'underlying_pct' / 'underlying_random_walk' for "
                    f"realistic re-pricing."
                )


def _match_glob(glob: str, section: str, account: str, symbol: str) -> bool:
    """
    Match a glob like `holdings.**` / `holdings.ZG*.*` / `positions.*.NIFTY*`
    against a (section, account, symbol) triple. `*` matches any run of
    characters within one segment; `**` matches everything remaining.
    """
    target = f"{section}.{account}.{symbol}"
    # `**` as a stand-alone segment matches any remaining path.
    norm = glob.replace(".**", ".*")
    if glob.endswith(".**"):
        norm = glob[:-3] + ".*"
    # Apply fnmatch segment-wise so `*` doesn't eat dots.
    g_parts = norm.split(".")
    t_parts = target.split(".")
    if len(g_parts) != len(t_parts):
        # Handle `**` at the tail: glob has fewer parts — expand.
        if glob.endswith(".**") and len(t_parts) >= len(g_parts) - 1:
            g_parts = g_parts[:-1]
            t_parts = t_parts[:len(g_parts)]
        else:
            return False
    return all(fnmatch.fnmatchcase(tp, gp) for gp, tp in zip(g_parts, t_parts))


class SimDriver:
    """
    Singleton simulation driver. Keeps the running per-symbol state and
    applies scenario moves tick-by-tick. Also exposes a "seed from live
    book" entrypoint so the operator can stress-test their actual positions.
    """

    _instance: Optional["SimDriver"] = None

    def __init__(self) -> None:
        self.active: bool = False
        self.scenario_slug: Optional[str] = None
        self.scenario: Optional[dict] = None
        self.seed_mode: str = "scripted"       # scripted | live | live+scenario
        self.started_at: Optional[datetime] = None
        self.tick_index: int = 0
        self.rate_ms: int = 2000

        # ── Iteration-mode state ────────────────────────────────────────
        # Populated by start_run(); zero-state for legacy single-shot
        # start() runs (the iteration scheduler isn't involved there).
        # `_run_active` distinguishes "between iterations of a multi-run"
        # from "fully stopped" — `self.active` flips on/off per iteration
        # whereas `_run_active` stays true for the whole run.
        self._run_active: bool = False
        self._scheduler_task: Optional[asyncio.Task] = None
        self.parent_run_id: Optional[int] = None
        self.current_sim_iteration_id: Optional[int] = None
        self.iteration_index: int = 0
        self.iterations_total: int = 0
        self.iteration_regime: Optional[str] = None
        self.iteration_max_minutes: Optional[int] = None
        self.iteration_started_at: Optional[datetime] = None
        self.iteration_force_close: bool = True
        # Set per iteration end so the scheduler knows why _run_loop exited
        # (book_empty / time_limit / stopped). Reset at iteration start.
        self.iteration_end_reason: Optional[str] = None
        # Optional: list of agent IDs to restrict this sim to — lets the
        # operator dry-fire a single agent from the /algo page.
        self.only_agent_ids: list[int] | None = None
        # Positions tick cadence — how often positions' LTPs refresh. 1 =
        # every tick. Resolved at start() from request override, scenario
        # YAML, or DB setting `simulator.positions_every_n_ticks`.
        self.positions_every_n_ticks: int = POSITIONS_UPDATE_EVERY_DEFAULT
        # Simulated market state — dict of overrides passed into run_cycle's
        # _build_context so time-aware agents see a simulated clock. Keyed
        # by preset or explicit fields; see MARKET_STATE_PRESETS.
        self.market_state: dict = dict(MARKET_STATE_PRESETS["mid_session"])
        # Simulated clock offset in minutes. Mutated by the
        # `advance_clock` primitive each tick. Drives both:
        #   - market_state.minutes_since_nse_open advances each tick so
        #     baseline-gated rate agents can be tested without waiting
        #     wall-clock minutes
        #   - reprice_row(ref_now=now + offset) so DTE / theta decay
        #     can be tested on expiry-day auto-close agents
        self._sim_clock_offset_minutes: int = 0
        self.market_state_preset: str = "mid_session"
        self._task: Optional[asyncio.Task] = None

        # Running per-symbol state. Holdings is deliberately unused (empty
        # forever) — positions-only sim. Kept here so `dataframes()` can
        # still return a valid empty sum_holdings frame.
        self._holdings_rows:  list[dict] = []
        # Which input buckets the operator asked to seed. Defaults to
        # ["positions"] (the historical behaviour); when "holdings" is
        # passed, seed_live() additionally pulls the holdings book and
        # snapshot() exposes summary_holdings; when "watchlist" is in
        # the list, watchlist symbols get re-quoted even with no open
        # position. Source of truth for the UI's conditional Holdings
        # summary panel.
        self.inputs:          list[str] = ["positions"]
        # Account scope — list of broker account codes (ZG0790,
        # ZJ6294, …). Empty list = all loaded accounts (historical
        # behaviour). When set, seed_live() filters every captured
        # row to only those accounts so the sim runs against a
        # subset of the operator's book.
        self.accounts:        list[str] = []
        self._positions_rows: list[dict] = []
        # Watchlist rows — zero-qty market-data-only entries. Move
        # primitives (pct / abs / random_walk / underlying_*) apply to
        # them the same as positions; agents can later condition on
        # `watchlist.<list>.<symbol>` scopes. Account-equivalent slot
        # carries the watchlist name (e.g. "Markets") for scoping.
        self._watchlist_rows: list[dict] = []
        self._margins_rows: list[dict] = []

        # Cached snapshot of the most recently fetched live book — lets the
        # UI preview "Load live book" before committing to Start.
        self._live_snapshot: Optional[dict] = None

        # Per-move random generator so random_walk scenarios are reproducible
        # across runs. Re-seeded on every start() from scenario config.
        self._rng: random.Random = random.Random()

        # Rolling buffer of recent ticks surfaced via /api/simulator/ticks/recent.
        self._tick_log: deque[dict] = deque(maxlen=TICK_LOG_LIMIT)

        # Per-symbol price history surfaced via /api/charts/price-history?mode=sim.
        # `(ts_iso, ltp, bid, ask)` per tick. Capped per-symbol so memory stays
        # bounded even on a long run; oldest entries fall off the deque.
        self._price_history: dict[str, deque] = {}

        # Derivatives state — populated at seed time so options re-price
        # coherently off underlying moves. `_underlyings` maps underlying
        # name (e.g. "NIFTY") → current spot. `_iv_cache` is per-position
        # implied vol locked at seed. `_underlying_history` mirrors
        # `_price_history` shape but for underlyings, charted alongside
        # the option prices via the same /api/charts endpoint.
        self._underlyings:        dict[str, float] = {}
        self._iv_cache:           dict[str, float] = {}
        self._underlying_history: dict[str, deque] = {}
        # Index of positions by underlying so an `underlying_pct` move
        # reaches its derivatives in O(1) instead of re-walking every
        # position. Built once at seed time, kept in sync as positions are
        # added (e.g. custom_positions) — fills don't remove from this
        # index because the chase engine handles the fill state itself.
        self._positions_by_underlying: dict[str, list[dict]] = {}

        # Bid/ask spread (decimal fraction — 0.001 = 0.10%). Applied on
        # every _recompute_position_row so every position carries per-side
        # prices. Resolved at start() from the request override or the DB
        # setting `simulator.default_spread_pct`.
        self.spread_pct: float = 0.0

        # Paper trade engine — owns the open-order book, fill / modify /
        # unfilled lifecycle, AlgoOrder DB writes. Fed by SimQuoteSource
        # so it reads bid/ask from this driver's `_positions_rows` (the
        # fabricated book). Mode 2 (real-data paper on prod) constructs
        # its own PaperTradeEngine fed by LiveQuoteSource — same engine,
        # different quote source.
        from backend.api.algo.paper      import PaperTradeEngine
        from backend.api.algo.quote      import SimQuoteSource
        self._paper = PaperTradeEngine(
            quote_source=SimQuoteSource(self),
            label="sim",
            on_event=self._forward_chase_event,
        )

    @classmethod
    def instance(cls) -> "SimDriver":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Public state snapshot ─────────────────────────────────────────

    def _tick_pcts_for_ui(self) -> list[float | None]:
        """
        Extract per-tick pct values from the currently-loaded scenario so
        the /admin/simulator page can render them as editable defaults.
        Returns a list the length of scenario.ticks; each entry is the
        first `pct`-move's value in that tick, or None when the tick has
        no pct move (e.g. the tick is target_pnl / set_margin).
        """
        if not self.scenario:
            return []
        out: list[float | None] = []
        for t in (self.scenario.get("ticks") or []):
            pct = None
            for m in (t.get("moves") or []):
                if (m.get("type") or "").lower() == "pct":
                    try:
                        pct = float(m.get("value"))
                    except (TypeError, ValueError):
                        pct = None
                    break
            out.append(pct)
        return out

    def snapshot(self) -> dict:
        return {
            "active":           self.active,
            "run_active":       self._run_active,
            "scenario":         self.scenario_slug,
            "seed_mode":        self.seed_mode,
            "tick_index":       self.tick_index,
            "rate_ms":          self.rate_ms,
            "started_at":       self.started_at.isoformat() if self.started_at else None,
            # Iteration-mode fields — zero/null when running a legacy
            # single-shot start() that didn't go through start_run.
            "parent_run_id":         self.parent_run_id,
            "iteration_index":       self.iteration_index,
            "iterations_total":      self.iterations_total,
            "iteration_regime":      self.iteration_regime,
            "iteration_max_minutes": self.iteration_max_minutes,
            "iteration_started_at":  self.iteration_started_at.isoformat()
                                        if self.iteration_started_at else None,
            "iteration_end_reason":  self.iteration_end_reason,
            "current_iteration_id":  self.current_sim_iteration_id,
            "total_ticks":      len(self.scenario.get("ticks", [])) if self.scenario else 0,
            "holdings_count":   len(self._holdings_rows),
            "positions_count":  len(self._positions_rows),
            "margins_count":    len(self._margins_rows),
            "only_agent_ids":   list(self.only_agent_ids) if self.only_agent_ids else [],
            "live_snapshot_at": (self._live_snapshot or {}).get("snapshot_at"),
            "positions_every_n_ticks": self.positions_every_n_ticks,
            "market_state_preset":     self.market_state_preset,
            "market_state":            dict(self.market_state),
            # Tick pct values actually running (after overrides applied) —
            # lets the UI reflect "what's active" even if the operator
            # changes the inputs after Start.
            "tick_pcts":               self._tick_pcts_for_ui(),
            "symbol_filter":           [r.get("tradingsymbol") for r in self._positions_rows]
                                        if self.scenario else [],
            # Distinct tradingsymbols currently loaded. Lets the UI keep the
            # Symbol picker fresh even when the operator started without
            # pressing "Load live book" first.
            "symbols":                 sorted({
                                           str(r.get("tradingsymbol", ""))
                                           for r in self._positions_rows
                                           if r.get("tradingsymbol")
                                       }),
            # Compact per-position snapshot — the Simulator page renders
            # this as a small pill list so operators see fills actually
            # remove rows from the book (not just a shrinking counter).
            "positions":               [
                {
                    "account":   r.get("account"),
                    "symbol":    r.get("tradingsymbol"),
                    "quantity":  r.get("quantity"),
                    "last_price": r.get("last_price"),
                    "bid":       r.get("bid"),
                    "ask":       r.get("ask"),
                    "pnl":       r.get("pnl"),
                }
                for r in self._positions_rows
            ],
            # Open-order snapshots — one per in-flight chase. Mirrors the
            # chase engine's internal state so the Simulator page can show
            # "NIFTY BUY 50 @ ₹21,800 · attempt 2/5" live.
            "open_order_details":      self._paper.open_order_details(),
            "spread_pct":              self.spread_pct,
            "open_orders":             len(self._paper.open_order_details()),
            # Per-account aggregates — same shape /dashboard renders, so
            # the Simulator panel can drop in the same summary grids
            # without re-computing on the frontend. Computed lazily here
            # because snapshot() is hot-path (polled every 2 s by the UI).
            "summary_positions":       self._summary_rows("positions"),
            "summary_holdings":        self._summary_rows("holdings"),
            # Per-underlying spot snapshot — drives the per-underlying
            # chart grid. Keys: NIFTY, BANKNIFTY, FINNIFTY, etc. Values:
            # current spot. PriceChart fetches /api/charts/price-history
            # ?symbol=NIFTY&mode=sim for the line data; this field just
            # tells the UI which underlyings to render charts for.
            "underlyings":             dict(self._underlyings),
            # What was actually seeded — positions, holdings, watchlist.
            # The UI uses this to decide whether to render the Holdings
            # summary panel (hidden when 'holdings' isn't in this list).
            "inputs":                  list(self.inputs),
            # Account scope for the run. Empty = all loaded accounts.
            "accounts":                list(self.accounts),
        }

    def _summary_rows(self, kind: str) -> list[dict]:
        """
        Per-account + TOTAL aggregate rows for the snapshot. Same shape
        the /dashboard summary grids consume — frontend just drops them
        into an ag-Grid. `kind` ∈ {'positions', 'holdings'}.

        For holdings, returns [] when self.inputs doesn't include
        'holdings' so the UI naturally hides the panel.
        """
        if kind == "holdings" and "holdings" not in self.inputs:
            return []
        rows = self._positions_rows if kind == "positions" else self._holdings_rows
        if not rows:
            return []
        # Per-account aggregate. Pandas-free path so this stays cheap
        # on every snapshot() call.
        per_acct: dict[str, dict] = {}
        for r in rows:
            acct = str(r.get("account") or "—")
            agg = per_acct.setdefault(acct, {
                "account": acct, "pnl": 0.0, "day_pnl": 0.0, "cur_val": 0.0,
            })
            try:
                agg["pnl"]     += float(r.get("pnl")     or 0.0)
                agg["day_pnl"] += float(r.get("day_pnl") or 0.0)
                qty = float(r.get("quantity") or 0)
                ltp = float(r.get("last_price") or 0.0)
                agg["cur_val"] += qty * ltp
            except (TypeError, ValueError):
                pass
        out = sorted(per_acct.values(), key=lambda d: d["account"])
        # TOTAL row.
        total = {
            "account": "TOTAL",
            "pnl":     sum(r["pnl"]     for r in out),
            "day_pnl": sum(r["day_pnl"] for r in out),
            "cur_val": sum(r["cur_val"] for r in out),
        }
        out.append(total)
        return out

    # ── DataFrame builder the agent engine consumes ───────────────────

    def dataframes(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Return (sum_holdings, sum_positions, df_margins) in the exact shape
        the real background task feeds into run_cycle. sum_holdings is
        always empty (positions-only sim); sum_positions uses the same
        summarise helper the live path uses so rounding matches.
        """
        from backend.shared.helpers.summarise import summarise_positions

        df_p_raw = pd.DataFrame(self._positions_rows) if self._positions_rows else pd.DataFrame()

        # Holdings-based agents (loss-hold-*) will see zero rows and
        # therefore never fire in the sim. That's the point — they're
        # untestable by design here; they only evaluate against live data.
        sum_h = pd.DataFrame(columns=["account", "inv_val", "cur_val", "pnl", "day_change_val"])
        sum_p = summarise_positions(df_p_raw) if not df_p_raw.empty \
                else pd.DataFrame(columns=["account", "pnl"])

        # df_margins: build per-account + TOTAL with the same columns the
        # real path produces. Flat passthrough with a computed TOTAL row.
        if self._margins_rows:
            df_m = pd.DataFrame(self._margins_rows)
            numeric = df_m.select_dtypes(include="number").sum()
            total   = numeric.to_dict()
            total["account"] = "TOTAL"
            df_m = pd.concat([df_m, pd.DataFrame([total])], ignore_index=True)
        else:
            df_m = pd.DataFrame(columns=["account"])

        return sum_h, sum_p, df_m

    # ── Control ───────────────────────────────────────────────────────

    def start(self, scenario_slug: str, rate_ms: int = 2000,
              *, seed_mode: str = "scripted",
              only_agent_ids: list[int] | None = None,
              positions_every_n_ticks: int | None = None,
              market_state_override: dict | None = None,
              inline_scenario: dict | None = None,
              pct_overrides: list[float] | None = None,
              symbol_filter: list[str] | None = None,
              spread_pct: float | None = None,
              custom_positions: list[dict] | None = None,
              walk_drift: float | None = None,
              walk_vol: float | None = None,
              walk_seed: int | None = None,
              chase_max_attempts: int | None = None,
              inputs: list[str] | None = None,
              accounts: list[str] | None = None) -> dict:
        """
        Start the sim against a named scenario from scenarios.yaml, or an
        `inline_scenario` dict (same shape) built at call time by the
        synthesiser. Inline scenarios do not live in the YAML catalog —
        useful for per-agent auto-generated tests.
        """
        assert_enabled()
        if self.active:
            raise SimGuardError("Sim is already running — stop it first.")
        if inline_scenario is not None:
            scen = inline_scenario
            scenario_slug = scen.get("slug") or scenario_slug
        else:
            scen = get_scenario(scenario_slug)
            if not scen:
                raise SimGuardError(f"Unknown scenario '{scenario_slug}'")
            # Realism check — non-inline scenarios are YAML-authored
            # and should drive market change via underlying_* moves so
            # derivatives reprice coherently off spot via BS. Direct-LTP
            # primitives (pct/abs/random_walk/target_pnl) on positions
            # scopes bypass the spot path and produce unrealistic option
            # ticks. The synthesizer (inline_scenario != None) is exempt
            # because it deliberately uses target_pnl to force agent
            # thresholds. Soft warning only — operator may still want
            # to run the scenario for ad-hoc testing.
            _warn_unrealistic_moves(scen, scenario_slug)

        # Apply pct_overrides into the scenario's ticks before we store
        # it. The shape we handle cleanly: every override slot [i] replaces
        # the `value` of every `pct`-typed move in ticks[i].moves. Scenarios
        # that don't match this shape (random_walk, target_pnl, set_margin)
        # are unaffected by pct_overrides — those moves are left alone.
        if pct_overrides:
            scen = copy.deepcopy(scen)
            for i, pct in enumerate(pct_overrides):
                if pct is None:
                    continue
                ticks = scen.get("ticks") or []
                if i >= len(ticks):
                    break
                for move in (ticks[i].get("moves") or []):
                    if (move.get("type") or "").lower() == "pct":
                        try:
                            move["value"] = float(pct)
                        except (TypeError, ValueError):
                            pass

        # Random-walk overrides — replace drift / vol on every random_walk
        # and underlying_random_walk move across every tick. Scenarios that
        # don't contain walk-shaped moves are unaffected. `walk_seed`
        # overrides the scenario-level seed for reproducibility from the UI.
        if walk_drift is not None or walk_vol is not None or walk_seed is not None:
            scen = copy.deepcopy(scen)
            if walk_seed is not None:
                scen["seed"] = int(walk_seed)
            for tick in (scen.get("ticks") or []):
                for move in (tick.get("moves") or []):
                    mtype = (move.get("type") or "").lower()
                    if mtype in ("random_walk", "underlying_random_walk"):
                        if walk_drift is not None:
                            move["drift"] = float(walk_drift)
                        if walk_vol is not None:
                            move["vol"]   = float(walk_vol)

        self.scenario_slug  = scenario_slug
        self.scenario       = scen
        self.seed_mode      = seed_mode
        # Input buckets — normalise to a clean list of known values.
        # Defaults to ["positions"] (historical behaviour). Unknown
        # values are silently dropped so a future "options-only" or
        # "watchlist+positions" UI mode doesn't error here.
        _known_inputs = {"positions", "holdings", "watchlist"}
        _req_inputs = [s.strip().lower() for s in (inputs or ["positions"]) if s]
        self.inputs = [s for s in _req_inputs if s in _known_inputs] or ["positions"]
        # Account scope — stored for snapshot serialization + passed
        # to seed_live below. Empty list = all loaded accounts.
        self.accounts = [str(a).strip().upper() for a in (accounts or []) if a]
        self.rate_ms        = max(200, int(rate_ms))
        self.tick_index     = 0
        self.started_at     = datetime.now()
        self.only_agent_ids = list(only_agent_ids) if only_agent_ids else None
        self._paper.reset()

        # Reset the rate-history bucket so consecutive sim runs don't
        # inherit stale samples — the rate evaluator otherwise sees
        # a stale baseline across the boundary between two runs of
        # different scenarios.
        self._sim_alert_state['pnl_history'] = {}
        # Anchor session_date so the engine's `_update_pnl_history`
        # rollover wipe doesn't trip on the first tick of the new run.
        if hasattr(self.started_at, 'date'):
            self._sim_alert_state['session_date'] = self.started_at.date()

        # Spread — request override > scenario YAML > DB setting. Stored as
        # a decimal fraction internally (0.001 = 0.10%). The UI submits a
        # percent (0.10) which the route layer converts before calling us.
        from backend.shared.helpers.settings import get_float
        raw_sp = (spread_pct
                  if spread_pct is not None
                  else scen.get("spread_pct",
                                get_float("simulator.default_spread_pct", 0.10) / 100.0))
        try:
            self.spread_pct = max(0.0, float(raw_sp))
        except (TypeError, ValueError):
            self.spread_pct = 0.0

        # Positions cadence — request override > scenario YAML > DB default.
        # Clamped to >= 1 so nothing ever gets divided by zero or silenced.
        raw_pos = (positions_every_n_ticks
                   if positions_every_n_ticks is not None
                   else scen.get("positions_every_n_ticks", _positions_every_default()))
        try:
            self.positions_every_n_ticks = max(1, int(raw_pos))
        except (TypeError, ValueError):
            self.positions_every_n_ticks = _positions_every_default()

        # Market-state resolution — request override > scenario YAML > default.
        # Both accept the same shape: {preset: "…"} or explicit fields.
        spec = market_state_override if market_state_override else scen.get("market_state")
        self.market_state = _resolve_market_state(spec)
        # Reset the simulated clock offset on every start so a fresh
        # run begins at the picked preset's nominal time.
        self._sim_clock_offset_minutes = 0
        # Reset regime-switch phase tracking so the first tick with a
        # `phase` field always logs a transition.
        self._last_phase = None
        # Per-run chase cap override — when set, replaces the paper
        # engine's default getter for the duration of this run.
        if chase_max_attempts is not None:
            cap = max(1, min(50, int(chase_max_attempts)))
            self._paper._get_max = lambda c=cap: c
        else:
            # Restore default when not overridden so a previous run's
            # override doesn't leak into this one.
            from backend.api.algo.paper import PaperTradeEngine
            self._paper._get_max = PaperTradeEngine._default_max_attempts
        self.market_state_preset = (
            (spec or {}).get("preset")
            if isinstance(spec, dict) and spec.get("preset") in MARKET_STATE_PRESETS
            else "mid_session"
        )

        # Seed the running state — either from scenario.initial, the live-book
        # snapshot, or both stacked. For the live modes, auto-snapshot if the
        # operator hasn't pressed "Load live book" yet. Holdings is now
        # simulated alongside positions (was positions-only) so day_pct /
        # day_rate_abs / day_rate_pct agents become testable too.
        self._holdings_rows = []
        self._watchlist_rows = []
        if seed_mode in ("live", "live+scenario"):
            # Re-seed when the account filter changed since the last
            # snapshot OR no snapshot exists. Otherwise the cached
            # snapshot may include rows from accounts the operator
            # has now excluded from this run.
            cached_accts = (self._live_snapshot or {}).get("accounts_filter") or []
            if not self._live_snapshot or cached_accts != self.accounts:
                try:
                    self.seed_live(accounts=self.accounts or None)
                except Exception as e:
                    raise SimGuardError(
                        f"Auto-seed of live book failed: {e}. "
                        f"Try POST /api/simulator/seed-live manually to surface "
                        f"the broker error."
                    )
            self._positions_rows = copy.deepcopy(self._live_snapshot["positions"])
            self._margins_rows   = copy.deepcopy(self._live_snapshot["margins"])
            self._holdings_rows  = copy.deepcopy(self._live_snapshot.get("holdings", []))
            self._watchlist_rows = copy.deepcopy(self._live_snapshot.get("watchlist", []))

        if seed_mode in ("scripted", "live+scenario"):
            initial = scen.get("initial") or {}
            if seed_mode == "scripted":
                self._positions_rows = copy.deepcopy(initial.get("positions", []))
                self._margins_rows   = copy.deepcopy(initial.get("margins", []))
                self._holdings_rows  = copy.deepcopy(initial.get("holdings", []))
            else:
                # live+scenario — scripted initial rows are layered on top of
                # the live snapshot (useful for injecting a specific symbol).
                self._positions_rows.extend(copy.deepcopy(initial.get("positions", [])))
                self._margins_rows.extend(copy.deepcopy(initial.get("margins", [])))
                self._holdings_rows.extend(copy.deepcopy(initial.get("holdings", [])))

        # Custom positions submitted from the UI's "Custom positions" panel.
        # These are layered ON TOP of whatever scripted/live seeding produced
        # so an operator can stress-test a synthetic NIFTY24500CE without
        # touching their real book. Each row needs at minimum tradingsymbol
        # + quantity + last_price; account / exchange / multiplier defaults
        # are inferred so a one-line entry is enough to start.
        if custom_positions:
            self._positions_rows.extend(_normalise_custom_positions(custom_positions))

        for r in self._positions_rows:
            _recompute_position_row(r, self.spread_pct)

        # Symbol filter — drop positions whose tradingsymbol isn't in the
        # requested allow-list. Operators use this to target a single
        # instrument (e.g. "simulate only my NIFTY short"). Empty / None
        # means no filter. Applied AFTER recompute so the filtered set
        # still has derived fields intact.
        if symbol_filter:
            allow = {str(s) for s in symbol_filter if s}
            if allow:
                self._positions_rows = [
                    r for r in self._positions_rows
                    if str(r.get("tradingsymbol", "")) in allow
                ]

        # When scripted seeding leaves the state empty (a scenario without
        # an `initial:` block — all 5 shipped ones + every synthesized
        # scenario), auto-upgrade to live+scenario and snapshot the real
        # book. Saves the operator from having to flip seed_mode manually
        # every time they press Start. Only reachable in `scripted` mode;
        # live / live+scenario paths already seeded earlier.
        if seed_mode == "scripted" and not (self._positions_rows or self._margins_rows):
            logger.info(
                f"[SIM] '{scenario_slug}' has no scripted initial — "
                f"auto-loading live book."
            )
            try:
                if not self._live_snapshot:
                    self.seed_live()
                self._positions_rows = copy.deepcopy(self._live_snapshot["positions"])
                self._margins_rows   = copy.deepcopy(self._live_snapshot["margins"])
                for r in self._positions_rows:
                    _recompute_position_row(r)
                self.seed_mode = "live"   # reflect what actually happened
            except Exception as e:
                self.scenario = None
                self.active   = False
                raise SimGuardError(
                    f"Scenario '{scenario_slug}' has no scripted initial state "
                    f"and auto-load of live book failed: {e}"
                )

        if not (self._positions_rows or self._margins_rows):
            self.scenario = None
            self.active   = False
            raise SimGuardError(
                f"Scenario '{scenario_slug}' has no initial state and the live "
                f"book returned no positions or margins. Nothing to simulate."
            )

        rng_seed = scen.get("seed")
        self._rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

        self._tick_log.clear()
        # Wipe price history so each sim run gets a clean chart. The chart
        # panel keys on (mode, symbol) so a fresh start should look fresh.
        self._price_history.clear()
        self._underlying_history.clear()
        # Detect derivatives in the seeded book, resolve each underlying's
        # spot, and calibrate per-option IV against current LTP. Done before
        # the run loop starts so the very first underlying_pct move re-prices
        # everything correctly. Failures are logged but don't block start —
        # a non-derivative book just leaves these dicts empty.
        self._seed_derivatives(scen)
        self._record_tick(
            kind="started", moves=[], changes=[],
            note=(f"{scenario_slug} · seed={seed_mode} · "
                  f"{len(self._holdings_rows)} holdings · "
                  f"{len(self._positions_rows)} positions"),
        )
        self.active = True
        logger.warning(
            f"[SIM] Started scenario={scenario_slug} seed={seed_mode} "
            f"rate={self.rate_ms}ms agents={self.only_agent_ids or 'all'}"
        )
        self._task = asyncio.create_task(self._run_loop(), name="sim-driver")
        return self.snapshot()

    def stop(self) -> dict:
        """
        EXTERNAL stop — invoked by `/api/simulator/stop`. Cancels the
        in-flight iteration AND the iteration scheduler so remaining
        iterations don't kick off.

        Internal end-of-iteration stops (book empty, time_limit) go
        through `_internal_stop()` instead — they keep the scheduler
        alive so it can roll over to the next iteration.
        """
        if not self.active and not self._run_active:
            return self.snapshot()
        # Cancel the scheduler FIRST so it doesn't react to the
        # in-flight iteration ending below.
        if self._run_active and self._scheduler_task and not self._scheduler_task.done():
            if self.active and not self.iteration_end_reason:
                self.iteration_end_reason = "stopped"
            self._scheduler_task.cancel()
            self._scheduler_task = None
            self._run_active = False
        return self._internal_stop()

    def _internal_stop(self) -> dict:
        """
        Per-iteration shutdown. Called from `_run_loop` when an
        auto-stop condition trips (book empty / time_limit / global
        auto_stop), and from the public `stop()` after the scheduler
        is cancelled. Does NOT touch the scheduler — the scheduler
        observes `self.active == False` and rolls over to the next
        iteration.
        """
        if not self.active:
            return self.snapshot()
        self.active = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._revert_settings()
        self._record_tick(
            kind="stopped", moves=[], changes=[],
            note=f"Stopped after {self.tick_index} ticks",
        )
        logger.warning(f"[SIM] Stopped after {self.tick_index} ticks")
        return self.snapshot()

    def step(self) -> dict:
        """Apply one tick (for deterministic debugging)."""
        assert_enabled()
        if not self.scenario:
            raise SimGuardError("No scenario loaded. Call start(...) first.")
        self._apply_next_tick()
        return self.snapshot()

    async def _run_loop(self) -> None:
        """Async loop driving the scenario at `rate_ms` cadence.

        Sets `self.iteration_end_reason` to the reason this iteration
        terminated so `_iteration_scheduler` knows whether to roll over
        to the next iteration or treat the run as failed.
        """
        try:
            auto_stop = _auto_stop_after()
            while self.active:
                # Global wall-clock cap (safety net — sim shouldn't bleed forever)
                if datetime.now() - self.started_at > auto_stop:
                    logger.warning(f"[SIM] Auto-stop after {auto_stop}")
                    if not self.iteration_end_reason:
                        self.iteration_end_reason = "auto_stop"
                    self._internal_stop()
                    return
                # Per-iteration max_minutes cap. Distinguished from the
                # global auto_stop: this one triggers force-close when
                # positions remain (controlled by iteration_force_close).
                if (self.iteration_max_minutes and self.iteration_started_at
                        and (datetime.now() - self.iteration_started_at).total_seconds() / 60.0
                            >= self.iteration_max_minutes):
                    self.iteration_end_reason = "time_limit"
                    if self.iteration_force_close and self._positions_rows:
                        try:
                            # Await so the AlgoOrder rows land BEFORE the
                            # scheduler reads them in _compute_iteration_fees.
                            # Fire-and-forget create_task() left fees=0 in
                            # the previous smoke run.
                            await self._force_close_open_positions("iteration time_limit")
                        except Exception as e:
                            logger.error(f"[SIM] force-close failed: {e}")
                    self._internal_stop()
                    return
                self._apply_next_tick()
                await self._run_cycle_once()
                await asyncio.sleep(self.rate_ms / 1000)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[SIM] Loop crashed: {e}")
            if not self.iteration_end_reason:
                self.iteration_end_reason = "failed"
            self.active = False

    async def _force_close_open_positions(self, reason: str) -> int:
        """
        Write synthetic close orders against every remaining open position
        at last_price. Called when an iteration hits its time_limit with
        positions still open. Side is inverted from the position's signed
        quantity (long → SELL close, short → BUY close).

        Each close lands as an AlgoOrder(mode='sim', engine='sim',
        status='FILLED') in the live DB. The position row is removed
        from `_positions_rows` so the next tick (if any) sees a clean
        book.

        Column names match the AlgoOrder model exactly:
          symbol, transaction_type, quantity, initial_price, fill_price,
          status, mode, engine, detail, filled_at, created_at, account,
          exchange, agent_id, attempts, slippage, broker_order_id,
          expiry_date.

        Awaitable so the insert COMPLETES before the iteration's
        finalize step reads the rows (prior fire-and-forget pattern
        left fees=0 because the rows hadn't landed yet).

        Returns the number of synthetic closes written.
        """
        from backend.api.database import async_session
        from backend.api.models import AlgoOrder
        from sqlalchemy import insert
        from datetime import datetime as _dt, timezone as _tz

        closes: list[dict] = []
        for row in list(self._positions_rows):
            qty = int(row.get("quantity") or 0)
            if qty == 0:
                continue
            side = "SELL" if qty > 0 else "BUY"
            fill = float(row.get("last_price") or 0.0)
            sym  = str(row.get("tradingsymbol") or "")
            acct = str(row.get("account") or "")
            closes.append({
                "account":          acct,
                "symbol":           sym,
                "exchange":         row.get("exchange") or "NFO",
                "transaction_type": side,
                "quantity":         abs(qty),
                "initial_price":    fill,
                "fill_price":       fill,
                "status":           "FILLED",
                "mode":             "sim",
                "engine":           "sim",
                "detail":           f"[SIM] force-close ({reason}) {side} {abs(qty)} {sym} @ ₹{fill:.2f}",
                "filled_at":        _dt.now(_tz.utc),
                "created_at":       _dt.now(_tz.utc),
            })

        # Clear the position rows BEFORE the DB insert so a tick crossing
        # the boundary doesn't see them. Re-add on insert failure.
        snapshot = list(self._positions_rows)
        self._positions_rows.clear()

        if not closes:
            return 0

        try:
            async with async_session() as s:
                await s.execute(insert(AlgoOrder), closes)
                await s.commit()
        except Exception as e:
            logger.error(f"[SIM] force-close insert failed: {e}")
            self._positions_rows.extend(snapshot)
            return 0

        logger.warning(f"[SIM] Force-closed {len(closes)} positions ({reason})")
        return len(closes)

    # ─── Iteration-mode scheduler ──────────────────────────────────────
    async def start_run(self, *, iterations: int, max_minutes: int,
                        regimes: list[str], agent_ids: list[int] | None,
                        seed: int | None, force_close_on_timeout: bool,
                        seed_mode: str, rate_ms: int | None,
                        spread_pct: float | None,
                        custom_positions: list[dict] | None,
                        inputs: list[str] | None = None,
                        accounts: list[str] | None = None,
                        run_name: str | None = None) -> dict:
        """
        Multi-iteration entry point. The driver runs `iterations` scenarios
        sequentially, round-robining through `regimes`. Each iteration:
          - Persists a SimIteration DB row (started_at, regime, seed)
          - Calls start() to initialize state and spawn _run_loop
          - Waits until _run_loop exits (book_empty / time_limit / failed)
          - Updates the SimIteration row (ended_at, end_reason, summary)
          - Brief pause before the next iteration

        Caller's POST returns immediately with the run snapshot; iterations
        play out in a background asyncio task.
        """
        assert_enabled()
        if self.active or self._run_active:
            raise SimGuardError("Sim is already running — stop it first.")
        if iterations < 1:
            raise SimGuardError("iterations must be >= 1")
        if not regimes:
            raise SimGuardError("regimes list cannot be empty")
        # Validate every regime resolves to a known scenario before we
        # spend an iteration discovering one isn't real.
        for r in regimes:
            if not get_scenario(r):
                raise SimGuardError(f"Unknown regime '{r}'")

        # Build the (regime, seed) plan — round-robin regimes, derive seeds
        # from `seed` when provided (seed + idx) so the run is replayable.
        plan: list[tuple[str, int | None]] = []
        for idx in range(iterations):
            regime = regimes[idx % len(regimes)]
            iter_seed = (seed + idx) if seed is not None else None
            plan.append((regime, iter_seed))

        self._run_active = True
        self.parent_run_id = None
        self.iterations_total = iterations
        self.iteration_force_close = bool(force_close_on_timeout)
        self.iteration_max_minutes = int(max_minutes)

        # Operator-supplied run name — sets the slug prefix for every
        # iteration in this run. Stash on self so _build_iteration_slug
        # can read it without threading the arg through every call.
        self.run_name = (run_name or '').strip() or None

        # Stash per-iteration start params so we can pass them through
        # to start() for each iteration without re-resolving.
        self._iter_start_params = {
            "agent_ids":        agent_ids,
            "seed_mode":        seed_mode,
            "rate_ms":          rate_ms,
            "spread_pct":       spread_pct,
            "custom_positions": custom_positions,
            "inputs":           inputs,
            "accounts":         accounts,
        }
        self._iter_plan = plan

        self._scheduler_task = asyncio.create_task(
            self._iteration_scheduler(), name="sim-iteration-scheduler",
        )
        return self.snapshot()

    async def _iteration_scheduler(self) -> None:
        """Outer task that walks `_iter_plan` sequentially."""
        from backend.api.database import async_session
        from backend.api.models import SimIteration
        from datetime import datetime as _dt, timezone as _tz
        import json as _json

        # Track the in-flight iteration's row across the loop so an
        # outer cancel/crash can finalise it with `end_reason='stopped'`
        # or `'failed'` rather than leaving an orphan pending row.
        in_flight_row_id: Optional[int] = None
        try:
            for idx, (regime, iter_seed) in enumerate(self._iter_plan, 1):
                self.iteration_index = idx
                self.iteration_regime = regime
                slug = self._build_iteration_slug(regime, idx)

                # Persist iteration row (started_at)
                row_id: Optional[int] = None
                try:
                    async with async_session() as s:
                        rec = SimIteration(
                            slug=slug,
                            parent_run_id=self.parent_run_id,
                            iteration_index=idx,
                            iterations_total=self.iterations_total,
                            regime=regime,
                            seed=iter_seed,
                            started_at=_dt.now(_tz.utc),
                            params_json=_json.dumps({
                                **self._iter_start_params,
                                "regime":       regime,
                                "seed":         iter_seed,
                                "max_minutes":  self.iteration_max_minutes,
                                "force_close":  self.iteration_force_close,
                            }, default=str),
                        )
                        s.add(rec)
                        await s.commit()
                        await s.refresh(rec)
                        row_id = rec.id
                        if self.parent_run_id is None:
                            self.parent_run_id = rec.id
                except Exception as e:
                    logger.error(f"[SIM] SimIteration insert failed: {e}")

                in_flight_row_id = row_id
                self.current_sim_iteration_id = row_id
                self.iteration_end_reason = None
                self.iteration_started_at = _dt.now()
                # Defensive: each iteration resets every per-iter dict
                # in alert_state so a previous iteration's residue
                # (suppression, shadow lifespan, exhaustion list) can't
                # leak. Pre-existing run-level dicts in alert_state
                # (sim_mode flag, pnl_history) are untouched.
                self._sim_alert_state['shadow_lifespan'] = {}
                self._sim_alert_state['lifespan_exhausted_agents'] = []

                # Kick off the per-iteration tick loop via the legacy
                # start() path. Pass walk_seed so reproducibility flows
                # through the existing random_walk plumbing.
                params = dict(self._iter_start_params)
                try:
                    self.start(
                        regime,
                        rate_ms=params.get("rate_ms") or 2000,
                        seed_mode=params.get("seed_mode") or "live",
                        only_agent_ids=params.get("agent_ids"),
                        spread_pct=params.get("spread_pct"),
                        custom_positions=params.get("custom_positions"),
                        walk_seed=iter_seed,
                        inputs=params.get("inputs"),
                        accounts=params.get("accounts"),
                    )
                except SimGuardError as e:
                    logger.warning(f"[SIM] iteration {idx} start failed: {e}")
                    self.iteration_end_reason = "failed"
                    await self._finalize_iteration_row(row_id, end_reason="failed",
                                                        summary={"error": str(e)})
                    in_flight_row_id = None  # finalized; nothing for the outer handler to do
                    continue

                # Wait for _run_loop to exit (auto-stop on book empty,
                # time_limit, or external stop()). Poll lightly.
                while self.active:
                    await asyncio.sleep(0.3)

                # Resolve end_reason if _run_loop didn't set one (book
                # auto-stopped on empty positions + no open orders, which
                # is the normal "scenario completed cleanly" outcome).
                if not self.iteration_end_reason:
                    self.iteration_end_reason = "book_empty" if not self._positions_rows else "scenario_complete"

                # Capture summary stats + persist. Fees are async (DB
                # walk) so compute them before the sync summary builder.
                fees = await self._compute_iteration_fees()
                summary = self._compute_iteration_summary(total_fees=fees)
                await self._finalize_iteration_row(
                    row_id, end_reason=self.iteration_end_reason, summary=summary,
                )
                in_flight_row_id = None  # cleanly finalized

                # Brief inter-iteration pause so observers can see the
                # boundary in the UI / logs.
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.warning("[SIM] Iteration scheduler cancelled")
            # Finalize the in-flight iteration row so the audit trail
            # isn't half-written. Operator-pressed Stop → 'stopped';
            # internal cancel without an explicit reason also lands here.
            if in_flight_row_id is not None:
                reason = self.iteration_end_reason or "stopped"
                try:
                    summary = self._compute_iteration_summary()
                except Exception:
                    summary = {"error": "summary computation failed during cancel"}
                await self._finalize_iteration_row(
                    in_flight_row_id, end_reason=reason, summary=summary,
                )
        except Exception as e:
            logger.error(f"[SIM] Scheduler crashed: {e}")
            if in_flight_row_id is not None:
                try:
                    summary = self._compute_iteration_summary()
                except Exception:
                    summary = {"error": str(e)}
                await self._finalize_iteration_row(
                    in_flight_row_id, end_reason="failed", summary=summary,
                )
        finally:
            self._run_active = False
            self._scheduler_task = None

    def _build_iteration_slug(self, regime: str, idx: int) -> str:
        """Iteration slug. When the operator supplied a `run_name`, use it
        as the prefix so every iteration in the run carries the operator's
        chosen label (e.g. `weekend-stress-iter-01`). Otherwise fall back
        to the legacy `<regime>-<HHMM>-<NN>` format that reads the regime
        + a short clock stamp at a glance.
        """
        prefix = (getattr(self, "run_name", "") or "").strip()
        if prefix:
            # Lowercase, replace whitespace+slash with '-', strip everything
            # but [a-z0-9-_] so the slug stays URL-safe.
            import re as _re
            slug_prefix = _re.sub(r"[^a-z0-9_-]+", "-", prefix.lower()).strip("-")
            if slug_prefix:
                return f"{slug_prefix}-iter-{idx:02d}"
        ts = datetime.now().strftime("%H%M")
        return f"{regime}-{ts}-{idx:02d}"

    def _compute_iteration_summary(self, *, total_fees: float = 0.0) -> dict:
        """Cheap end-of-iteration stats — total P&L, position counts, etc.

        Accepts an externally-computed `total_fees` (async helper does
        the DB walk; this method stays sync because some callers (e.g.
        the CancelledError handler) need to compute summary without
        await). The net_pnl figure is what the operator would actually
        keep after broker charges — typically 0.5-2% lower than gross.
        """
        rows = list(self._positions_rows)
        total_pnl = sum(float(r.get("pnl") or 0) for r in rows)
        hung      = sum(1 for r in rows if int(r.get("quantity") or 0) != 0)
        # Shadow-lifespan: agents that hit their budget during this
        # iteration. Captured so the report surfaces "agent X would
        # have exhausted at fire #N under this regime" without the
        # operator having to inspect every event row.
        exhausted = list(self._sim_alert_state.get('lifespan_exhausted_agents') or [])

        return {
            "tick_index":          self.tick_index,
            "total_pnl_remaining": total_pnl,   # P&L of still-open positions (gross)
            "total_fees":          round(float(total_fees), 2),
            "net_pnl_remaining":   round(total_pnl - float(total_fees), 2),
            "hung_positions":      hung,
            "regime":              self.iteration_regime,
            "lifespan_exhausted_agents": exhausted,
        }

    async def _compute_iteration_fees(self) -> float:
        """
        Sum Kite-style brokerage + STT + ancillary + GST for every sim
        AlgoOrder written within this iteration's timestamp window.
        Called from the async scheduler before persisting the summary.
        Best-effort: any DB/lookup error returns 0 so the scheduler
        finalises without aborting.
        """
        if not self.iteration_started_at:
            return 0.0
        try:
            from backend.shared.helpers.fees import compute_order_fees
            from backend.api.database import async_session
            from backend.api.models import AlgoOrder
            from sqlalchemy import select, and_
        except Exception as e:
            logger.warning(f"[SIM] fee imports failed: {e}")
            return 0.0
        try:
            async with async_session() as s:
                rows = (await s.execute(
                    select(AlgoOrder).where(and_(
                        AlgoOrder.mode == 'sim',
                        AlgoOrder.created_at >= self.iteration_started_at,
                    ))
                )).scalars().all()
            fees = 0.0
            for o in rows:
                fees += compute_order_fees({
                    "tradingsymbol":    o.symbol,
                    "transaction_type": o.transaction_type,
                    "quantity":         o.quantity,
                    "fill_price":       o.fill_price,
                    "initial_price":    o.initial_price,
                })
            return fees
        except Exception as e:
            logger.warning(f"[SIM] fee computation failed: {e}")
            return 0.0

    async def _finalize_iteration_row(self, row_id: Optional[int], *,
                                       end_reason: str, summary: dict) -> None:
        if row_id is None:
            return
        from backend.api.database import async_session
        from backend.api.models import SimIteration
        from sqlalchemy import update
        from datetime import datetime as _dt, timezone as _tz
        import json as _json
        try:
            async with async_session() as s:
                await s.execute(
                    update(SimIteration).where(SimIteration.id == row_id).values(
                        ended_at=_dt.now(_tz.utc),
                        end_reason=end_reason,
                        summary_json=_json.dumps(summary, default=str),
                    )
                )
                await s.commit()
        except Exception as e:
            logger.error(f"[SIM] SimIteration finalize failed: {e}")

    # Long-lived alert_state for the simulator — kept separate from the real
    # background task's state so rate-history and suppression don't cross.
    _sim_alert_state: dict = {"sim_mode": True}

    async def _run_cycle_once(self) -> None:
        """Invoke the agent engine against the current sim state."""
        try:
            from backend.api.algo.agent_engine import run_cycle
            from backend.api.routes.algo import _broadcast_event
            from backend.shared.helpers.date_time_utils import (
                timestamp_display, timestamp_indian,
            )

            sum_h, sum_p, df_m = self.dataframes()
            ctx = {
                "sum_holdings":   sum_h,
                "sum_positions":  sum_p,
                "df_margins":     df_m,
                # Watchlist rows from the sim driver flow through to
                # the agent evaluator's Context.watchlist_rows field so
                # agents conditioning on `watchlist.*` scopes can fire.
                "watchlist_rows": list(self._watchlist_rows),
                "now":            timestamp_indian(),
                "ist_display":    timestamp_display(),
                "seg_state":      {},
                "segments":       [],
                "alert_state":    self._sim_alert_state,
                "sim_mode":       True,
                # Simulated clock + segment flags — picked up by run_cycle →
                # _build_context so time-aware agents evaluate against the
                # scenario's market state, not wall-clock time.
                "market_state":   dict(self.market_state),
            }
            # Isolated ("Run in Simulator" per-agent) runs want every tick
            # to fire so the operator gets immediate feedback — they bypass
            # suppression. General sim runs keep suppression on so the same
            # breach doesn't fire on every tick.
            isolated = bool(self.only_agent_ids)
            await run_cycle(
                ctx, _broadcast_event,
                only_agent_ids=self.only_agent_ids,
                bypass_schedule=True,
                bypass_suppression=isolated,
            )
        except Exception as e:
            logger.error(f"[SIM] run_cycle failed: {e}")

    # ── Tick + move application ──────────────────────────────────────

    def _apply_next_tick(self) -> None:
        """Apply the next tick in the scenario (wraps at end)."""
        if not self.scenario:
            return
        ticks = self.scenario.get("ticks", []) or []
        if not ticks:
            return
        tick = ticks[self.tick_index % len(ticks)]

        # Regime-switch annotation — log when the phase changes between
        # ticks so the operator can see "calm → crash" in the tick log.
        phase = tick.get("phase")
        if phase and phase != getattr(self, "_last_phase", None):
            self._record_tick(
                kind="phase-change", moves=[], changes=[],
                note=f"phase → {phase}",
            )
            self._last_phase = phase

        # A tick may carry `moves` (Model B, price-level) or `patch` (legacy
        # aggregate). We support both — moves take precedence.
        moves = tick.get("moves") or []
        if not moves and tick.get("patch"):
            # Legacy aggregate patch — apply directly to matching account row.
            changes = self._apply_legacy_patch(tick["patch"])
        else:
            changes = self._apply_moves(moves)

        self.tick_index += 1
        self._record_tick(kind="tick", moves=moves, changes=changes)
        # Snapshot the post-move price for every symbol still in the book so
        # the chart panel can render the trajectory. Capture happens before
        # the chase step so a fill that removes the row from `_positions_rows`
        # still leaves its last LTP in the history.
        self._capture_price_history()
        # Run the chase engine against the new bid/ask state: fill any
        # orders whose limit crossed, otherwise re-quote them one step
        # closer to the opposite side.
        self._paper.step()
        # If every position has closed out (either via fills or because the
        # operator scoped the sim to an empty symbol list), there's nothing
        # left to simulate — halt cleanly with a terminal log entry so the
        # operator knows why the loop exited.
        self._check_auto_complete()

    def _iter_rows(self, section: str):
        """Yield (row, section) pairs for a given section name."""
        if section == "holdings":
            return self._holdings_rows
        if section == "positions":
            return self._positions_rows
        if section == "margins":
            return self._margins_rows
        if section == "watchlist":
            return self._watchlist_rows
        return []

    def _apply_moves(self, moves: list[dict]) -> list[dict]:
        """Apply a list of price moves and return change diffs for the tick log."""
        changes: list[dict] = []
        # Positions-only sim: positions refresh every Nth tick per the
        # cadence setting; holdings scope globs are silently skipped (we
        # don't carry holdings state). Tick 0 always refreshes so market
        # open feels right.
        positions_tick = (self.tick_index % self.positions_every_n_ticks) == 0
        for move in moves:
            mtype = (move.get("type") or "").lower()
            scope = move.get("scope") or ""
            if mtype == "set_margin":
                changes.extend(self._apply_set_margin(scope, move))
                continue
            # Time-travel primitive — advances the simulated clock by N
            # minutes. Affects market_state.minutes_since_nse_open (the
            # baseline gate for rate agents) AND DTE via ref_now in
            # subsequent reprice_row calls (theta decay testing).
            if mtype == "advance_clock":
                changes.extend(self._apply_advance_clock(move))
                continue
            # Implied-vol shock — directly mutates _iv_cache for matched
            # option positions. Subsequent underlying moves re-price
            # those options with the new σ (vega shock testing).
            if mtype == "set_iv":
                changes.extend(self._apply_set_iv(scope, move))
                continue
            # IV skew shift — like set_iv but per-strike: ATM IV moves by
            # atm_delta; OTM puts get extra `put_skew × (1 − K/S)`; OTM
            # calls get extra `call_skew × (K/S − 1)`. Models how IV
            # behaves asymmetrically across strikes during a crash (puts
            # get bid up MORE than ATM, calls less). Used by the
            # extreme-gap-down / extreme-gap-up regimes for realistic
            # tail-hedge P&L.
            if mtype == "set_iv_skew":
                changes.extend(self._apply_set_iv_skew(scope, move))
                continue
            # Non-market event primitive — overrides a DB-backed setting
            # for the duration of the sim run (e.g. lower a threshold,
            # disable a capability). The override is reverted on Stop /
            # Clear so prod operator-set values aren't touched.
            if mtype == "set_setting":
                changes.extend(self._apply_set_setting(move))
                continue
            # Underlying moves: shift the spot, then re-price every option /
            # future on that underlying using the cached IV. Drives coherent
            # F&O sims off a single "−3% NIFTY" tick.
            if mtype in ("underlying_pct", "underlying_abs", "underlying_target",
                         "underlying_random_walk"):
                changes.extend(self._apply_underlying_move(mtype, scope, move))
                continue
            # Holdings + positions both flow through the same scope
            # matcher and primitive helpers; the section is read from
            # the row when `_refresh` recomputes derived columns.
            if scope.startswith("positions.") and not positions_tick:
                continue
            matched = self._scope_matches(scope)
            if not matched:
                logger.info(f"[SIM] move {mtype} scope='{scope}' matched nothing")
                continue
            if mtype == "pct":
                changes.extend(self._apply_pct(matched, float(move.get("value") or 0)))
            elif mtype == "abs":
                changes.extend(self._apply_abs(matched, float(move.get("value") or 0)))
            elif mtype == "random_walk":
                drift = float(move.get("drift") or 0.0)
                vol   = float(move.get("vol")   or 0.0)
                changes.extend(self._apply_random_walk(matched, drift, vol))
            elif mtype == "target_pnl":
                target = float(move.get("value") or 0)
                changes.extend(self._apply_target_pnl(matched, target))
            else:
                logger.warning(f"[SIM] unknown move type '{mtype}'")
        return changes

    # ── Derivatives ──────────────────────────────────────────────────

    def _seed_derivatives(self, scen: dict) -> None:
        """Walk the seeded position book, detect underlyings, resolve each
        one's spot, and calibrate per-option IV. Underlyings are sourced
        from (in order):
          1. `scen.initial.underlyings: {NAME: spot}` — explicit override.
          2. The futures contract on that underlying — its last_price IS
             effectively spot for our purposes (intraday cost-of-carry is
             well below the tick).
          3. The ATM call+put midpoint via crude proxy: the strike of the
             nearest-to-money option (no put-call parity needed in v1).
        Anything that can't be resolved is left out — those positions will
        only respond to per-symbol pct/abs moves, not underlying moves."""
        from backend.api.algo.derivatives import (
            calibrate_iv_for_row, parse_tradingsymbol,
        )

        self._underlyings.clear()
        self._iv_cache.clear()
        self._positions_by_underlying.clear()

        explicit = (scen.get("initial") or {}).get("underlyings") or {}
        explicit = {str(k).upper(): float(v) for k, v in explicit.items()}

        # 1. Explicit overrides win.
        for name, spot in explicit.items():
            self._underlyings[name] = spot

        # Walk positions ONCE — stash the parser result on each row and
        # group by underlying. Both downstream loops (spot-resolution +
        # IV calibration) reuse the cached parse, and `_reprice_…` reads
        # from `_positions_by_underlying` for O(1) underlying lookup
        # instead of re-walking every position per underlying move.
        for r in self._positions_rows:
            sym    = str(r.get("tradingsymbol") or "")
            parsed = parse_tradingsymbol(sym) if sym else None
            r["_parsed"] = parsed
            if not parsed:
                continue
            self._positions_by_underlying.setdefault(parsed["underlying"], []).append(r)

        for name, rows in self._positions_by_underlying.items():
            if name in self._underlyings:
                continue
            # 2. Futures last_price as spot proxy. Reuse the cached parse.
            fut = next((r for r in rows
                        if (r.get("_parsed") or {}).get("kind") == "fut"
                        and r.get("last_price")), None)
            if fut:
                self._underlyings[name] = float(fut["last_price"])
                continue
            # 3. Crude ATM proxy: among options, find the strike nearest to
            #    the median strike (assumes the operator's book straddles
            #    the spot, which is the common case for hedged F&O).
            strikes = [
                (r["_parsed"] or {}).get("strike") for r in rows
                if (r.get("_parsed") or {}).get("kind") == "opt"
            ]
            strikes = [s for s in strikes if s is not None]
            if strikes:
                strikes.sort()
                self._underlyings[name] = strikes[len(strikes) // 2]

        # Calibrate IV per option position. Reuses the same cached parse.
        ref_now = self._ref_now()
        for r in self._positions_rows:
            p = r.get("_parsed")
            if not p or p.get("kind") != "opt":
                continue
            spot = self._underlyings.get(p["underlying"])
            if spot is None:
                continue
            sigma = calibrate_iv_for_row(r, spot, ref_now=ref_now)
            if sigma is not None:
                self._iv_cache[str(r.get("tradingsymbol") or "")] = sigma

        if self._underlyings:
            spots = ", ".join(f"{n}={v:,.2f}" for n, v in self._underlyings.items())
            logger.info(
                f"[SIM] derivatives seeded · underlyings: {spots} · "
                f"iv-calibrated: {len(self._iv_cache)}"
            )

    # Default beta table for cross-underlying correlation propagation.
    # When a `underlying_*` move fires without an explicit `propagate:`
    # list, the engine looks the leading underlying up here and applies
    # derived moves to the listed peers at `beta × primary_delta`.
    # Betas roughly match observed NSE intraday correlations:
    #   - NIFTY moves typically lead BANKNIFTY by 1.25-1.35×
    #   - FINNIFTY tracks NIFTY at ~1.10×
    #   - BANKNIFTY ↔ NIFTY relationship in reverse uses 1/beta ≈ 0.77
    # Operator can override per scenario by putting an explicit
    # `propagate: [{to: "X", beta: 0.5}, …]` on the underlying move.
    _DEFAULT_BETAS: dict = {
        "NIFTY":     [{"to": "BANKNIFTY", "beta": 1.30},
                      {"to": "FINNIFTY",  "beta": 1.10}],
        "BANKNIFTY": [{"to": "NIFTY",     "beta": 0.77}],
        "FINNIFTY":  [{"to": "NIFTY",     "beta": 0.91}],
    }

    def _apply_underlying_move(self, mtype: str, scope: str,
                               move: dict, _propagate_depth: int = 0) -> list[dict]:
        """
        Underlying scope is `underlying.<NAME>` or `underlying.*`. The move
        types are:
          underlying_pct          value=0.03                     → spot × 1.03
          underlying_abs          value=25                       → spot + 25
          underlying_target       value=22000                    → spot ← 22000
          underlying_random_walk  drift=-0.001 vol=0.005         → spot ×
                                                                   (1 + drift + vol·N(0,1))
        After updating the spot, every option/future position whose
        underlying matches is re-priced (BS for options using cached σ;
        spot 1:1 for futures).

        `underlying_random_walk` is the realistic-market primitive: walks
        the SPOT (not each contract), so all options on that underlying
        re-price coherently via Black-Scholes each tick.

        Cross-underlying correlation: after the primary move resolves, if
        the move carries `propagate: [{to: NAME, beta: X}, ...]` OR the
        underlying has an entry in `_DEFAULT_BETAS`, derived
        `underlying_pct` moves fire at `beta × primary_delta_pct` on each
        peer. `_propagate_depth` caps at 1 hop so NIFTY→BANKNIFTY
        doesn't recurse back to NIFTY.
        """
        if not scope.startswith("underlying."):
            logger.warning(f"[SIM] underlying move expects 'underlying.*' scope, got '{scope}'")
            return []
        which = scope.split(".", 1)[1].upper()
        names = ([n for n in self._underlyings if fnmatch.fnmatch(n, which)]
                 if "*" in which or "?" in which
                 else ([which] if which in self._underlyings else []))
        if not names:
            logger.info(f"[SIM] underlying move '{scope}' matched no known underlying "
                        f"(known: {sorted(self._underlyings)})")
            return []

        value = float(move.get("value") or 0)
        drift = float(move.get("drift") or 0.0)
        vol   = float(move.get("vol")   or 0.0)
        changes: list[dict] = []
        for name in names:
            old_spot = float(self._underlyings[name])
            if   mtype == "underlying_pct":    new_spot = old_spot * (1.0 + value)
            elif mtype == "underlying_abs":    new_spot = old_spot + value
            elif mtype == "underlying_target": new_spot = value
            elif mtype == "underlying_random_walk":
                # GBM step at the underlying level. drift+vol are per-tick.
                shock    = drift + vol * self._rng.gauss(0.0, 1.0)
                new_spot = old_spot * (1.0 + shock)
            else:                              new_spot = old_spot
            self._underlyings[name] = new_spot
            # Synthetic change row so the tick log shows the underlying move.
            # `_capture_price_history` (called from the tick loop) picks up
            # the new spot into `_underlying_history` for chart overlays.
            # Same shape as `_change` so the LogPanel's Simulator tab renders
            # it without special-casing.
            if mtype == "underlying_random_walk":
                reason = f"{mtype} drift={drift:+.4f} vol={vol:.4f} → {new_spot - old_spot:+.2f}"
            else:
                reason = f"{mtype} {value:+.4f} (underlying)"
            changes.append({
                "section":  "underlying",
                "account":  None,
                "symbol":   name,
                "col":      "last_price",
                "prev":     old_spot,
                "next":     new_spot,
                "delta":    new_spot - old_spot,
                "reason":   reason,
                "bid":      None,
                "ask":      None,
            })
            # Re-price every position on this underlying — produces one
            # change row per derived contract so the operator sees the chain.
            changes.extend(self._reprice_derivatives_for(name, new_spot))

            # Cross-underlying correlation: propagate this move to
            # correlated peers via either the move's explicit
            # `propagate:` list or the default beta table. Bounded to
            # one hop (`_propagate_depth=1`) so NIFTY → BANKNIFTY can't
            # bounce back to NIFTY and create an oscillation.
            if _propagate_depth >= 1:
                continue
            propagate = move.get("propagate")
            if propagate is None:
                propagate = self._DEFAULT_BETAS.get(name, [])
            if not propagate:
                continue
            # Compute primary move as a % delta so we can scale per peer.
            if old_spot <= 0:
                continue
            primary_pct = (new_spot - old_spot) / old_spot
            for peer in propagate:
                peer_name = (peer.get("to") or "").upper()
                if not peer_name or peer_name not in self._underlyings:
                    continue
                try:
                    beta = float(peer.get("beta") or 0)
                except (TypeError, ValueError):
                    beta = 0.0
                if beta == 0.0:
                    continue
                peer_pct = primary_pct * beta
                # Recursive call with depth=1 to apply + reprice + log the
                # peer's move. The recursion guard above prevents further hops.
                changes.extend(self._apply_underlying_move(
                    "underlying_pct",
                    f"underlying.{peer_name}",
                    {"value": peer_pct},
                    _propagate_depth=1,
                ))
        return changes

    def _reprice_derivatives_for(self, underlying: str, spot: float) -> list[dict]:
        """Re-price every position on `underlying` using BS (opts) or 1:1
        spot (futures). Reads from the per-underlying index built at seed
        so this is O(matched) instead of O(positions) — important when a
        50-position book has a single 3-position NIFTY chain."""
        from backend.api.algo.derivatives import reprice_row
        ref_now = self._ref_now()
        changes: list[dict] = []
        rows = self._positions_by_underlying.get(underlying, [])
        for row in rows:
            parsed = row.get("_parsed")
            if not parsed:
                continue
            sym   = str(row.get("tradingsymbol") or "")
            sigma = self._iv_cache.get(sym)
            new   = reprice_row(row, spot=spot, sigma=sigma, ref_now=ref_now)
            if new is None:
                continue
            prev = float(row.get("last_price") or 0)
            row["last_price"] = float(new)
            self._refresh("positions", row)
            kind = parsed["kind"]
            tag  = (f"BS@σ={sigma:.3f}" if (kind == "opt" and sigma is not None)
                    else "fut↔spot" if kind == "fut" else "BS")
            changes.append(self._change("positions", row, prev, new,
                                        reason=f"{tag} (spot={spot:,.2f})"))
        return changes

    def _scope_matches(self, scope: str) -> list[tuple[str, dict]]:
        """Return every (section, row) pair whose path matches the glob."""
        out: list[tuple[str, dict]] = []
        section = scope.split(".", 1)[0]
        for row in self._iter_rows(section):
            acct = str(row.get("account", ""))
            sym  = str(row.get("tradingsymbol", ""))
            if _match_glob(scope, section, acct, sym):
                out.append((section, row))
        return out

    def _apply_pct(self, matched: list[tuple[str, dict]], pct: float) -> list[dict]:
        changes = []
        for section, row in matched:
            prev = float(row.get("last_price") or 0)
            new  = prev * (1.0 + pct)
            row["last_price"] = new
            self._refresh(section, row)
            changes.append(self._change(section, row, prev, new, reason=f"pct {pct*100:+.2f}%"))
        return changes

    def _apply_abs(self, matched: list[tuple[str, dict]], delta: float) -> list[dict]:
        changes = []
        for section, row in matched:
            prev = float(row.get("last_price") or 0)
            new  = prev + delta
            row["last_price"] = new
            self._refresh(section, row)
            changes.append(self._change(section, row, prev, new, reason=f"abs {delta:+.2f}"))
        return changes

    def _apply_random_walk(self, matched: list[tuple[str, dict]],
                            drift: float, vol: float) -> list[dict]:
        changes = []
        for section, row in matched:
            prev = float(row.get("last_price") or 0)
            shock = drift + vol * self._rng.gauss(0.0, 1.0)
            new  = prev * (1.0 + shock)
            row["last_price"] = new
            self._refresh(section, row)
            changes.append(self._change(section, row, prev, new,
                                        reason=f"walk drift={drift:+.4f} vol={vol:.4f}"))
        return changes

    def _apply_target_pnl(self, matched: list[tuple[str, dict]], target: float) -> list[dict]:
        """
        Drive the matched rows' aggregate pnl toward `target` by moving each
        LTP uniformly. Solves `ΔLTP × Σqty = target − currentPnl`. Rejects
        mixed-sign position sets (long + short) where a uniform ΔLTP makes
        no physical sense — documented in the Model-B plan.
        """
        if not matched:
            return []
        qty_sum = 0.0
        cur_pnl_sum = 0.0
        signs = set()
        for _, row in matched:
            q = float(row.get("quantity") or row.get("opening_quantity") or 0)
            qty_sum += q
            cur_pnl_sum += float(row.get("pnl") or 0)
            if q != 0:
                signs.add(1 if q > 0 else -1)
        if len(signs) > 1:
            logger.warning("[SIM] target_pnl refused — scope has mixed long/short")
            return []
        if qty_sum == 0:
            return []
        delta_ltp = (target - cur_pnl_sum) / qty_sum
        changes = []
        for section, row in matched:
            prev = float(row.get("last_price") or 0)
            new  = prev + delta_ltp
            row["last_price"] = new
            self._refresh(section, row)
            changes.append(self._change(section, row, prev, new,
                                        reason=f"target_pnl={target:.0f}"))
        return changes

    def _apply_set_margin(self, scope: str, move: dict) -> list[dict]:
        """
        Direct margin patch — price-decoupled by design. `scope` is
        `margins.<account>` and `fields` is a dict of column overrides.
        """
        changes = []
        parts = scope.split(".", 1)
        if len(parts) != 2 or parts[0] != "margins":
            logger.warning(f"[SIM] set_margin bad scope '{scope}'")
            return changes
        acct_glob = parts[1]
        fields = move.get("fields") or {}
        for row in self._margins_rows:
            if not fnmatch.fnmatchcase(str(row.get("account", "")), acct_glob):
                continue
            for k, v in fields.items():
                prev = row.get(k)
                row[k] = v
                changes.append({
                    "section": "margins", "account": row.get("account"), "symbol": "",
                    "col": k, "prev": prev, "next": v,
                    "delta": (v - prev) if isinstance(prev, (int, float)) and isinstance(v, (int, float)) else None,
                    "reason": "set_margin",
                })
        return changes

    def _apply_advance_clock(self, move: dict) -> list[dict]:
        """
        Advance the simulated clock by N minutes. Two downstream effects:
          1. `minutes_since_nse_open` is bumped on the market_state dict
             the agent engine reads — lets rate agents pass the baseline
             gate without waiting real wall-clock time.
          2. Subsequent `reprice_row` calls receive `ref_now = now() +
             clock_offset`, shrinking DTE — drives theta decay and
             expiry-day auto-close behaviour.

        Accepts: `{minutes: N}` or `{days: N}` (days = minutes × 1440).
        """
        minutes = int(move.get("minutes") or 0)
        days    = int(move.get("days")    or 0)
        delta   = minutes + days * 1440
        if delta == 0:
            return []
        self._sim_clock_offset_minutes += delta
        # Apply to whichever side of market_state is non-zero so the
        # advance reads intuitively: at-open + advance 60 min = 60 min
        # since open; at-close + advance 60 min = 60 min since close.
        if self.market_state.get("minutes_since_nse_open"):
            self.market_state["minutes_since_nse_open"] += delta
        elif self.market_state.get("minutes_since_nse_close"):
            self.market_state["minutes_since_nse_close"] += delta
        else:
            self.market_state["minutes_since_nse_open"] = delta
        return [{
            "section": "clock",
            "account": None,
            "symbol":  "",
            "col":     "sim_clock_offset_min",
            "prev":    self._sim_clock_offset_minutes - delta,
            "next":    self._sim_clock_offset_minutes,
            "delta":   delta,
            "reason":  f"advance_clock {delta:+d} min",
            "bid":     None,
            "ask":     None,
        }]

    def _apply_set_iv(self, scope: str, move: dict) -> list[dict]:
        """
        Vega shock — directly overwrite `_iv_cache[symbol]` for every
        option position matched by `scope`. The new σ is consumed by
        the next `reprice_row` call (typically triggered by a
        subsequent `underlying_*` move in the same or later tick).

        Accepts: `{value: 0.30}` for an absolute σ set, OR
                 `{delta: 0.05}` to add to the existing σ.
        """
        matched = self._scope_matches(scope)
        value   = move.get("value")
        delta   = move.get("delta")
        changes: list[dict] = []
        for section, row in matched:
            if section != "positions":
                continue
            sym = str(row.get("tradingsymbol") or "")
            old = self._iv_cache.get(sym)
            if value is not None:
                new = float(value)
            elif delta is not None and old is not None:
                new = float(old) + float(delta)
            else:
                continue
            new = max(0.0001, min(5.0, new))
            self._iv_cache[sym] = new
            changes.append({
                "section": "positions", "account": row.get("account"),
                "symbol":  sym, "col": "iv",
                "prev":    old, "next": new,
                "delta":   (new - old) if old is not None else None,
                "reason":  "set_iv",
                "bid":     None, "ask": None,
            })
        return changes

    def _apply_set_iv_skew(self, scope: str, move: dict) -> list[dict]:
        """
        Skew-aware IV shift. For each matched option position:
          new_iv = old_iv + atm_delta + skew_extra
        where `skew_extra` depends on whether the strike is OTM put or
        OTM call relative to the underlying's current spot:
          OTM put  (K < S):  skew_extra = put_skew  × (1 − K/S)
          OTM call (K > S):  skew_extra = call_skew × (K/S − 1)
          ATM      (K = S):  skew_extra = 0

        Models the realistic asymmetry where a crash drives OTM put IV
        UP more than ATM, and OTM call IV less than ATM. The
        extreme-gap-down regime uses this with `{atm_delta: 0.30,
        put_skew: 0.50, call_skew: 0.10}` so deep OTM puts see ~80
        vol-point IV jump while ATM sees 30 and OTM calls see 30-40.

        Accepts:
          {atm_delta: 0.30, put_skew: 0.50, call_skew: 0.10}

        scope: e.g. "positions.**" or "positions.*.NIFTY*" — same matcher
        as `set_iv`.
        """
        from backend.api.algo.derivatives import parse_tradingsymbol

        matched = self._scope_matches(scope)
        atm_delta  = float(move.get("atm_delta")  or 0.0)
        put_skew   = float(move.get("put_skew")   or 0.0)
        call_skew  = float(move.get("call_skew")  or 0.0)
        changes: list[dict] = []
        for section, row in matched:
            if section != "positions":
                continue
            sym = str(row.get("tradingsymbol") or "")
            old = self._iv_cache.get(sym)
            if old is None:
                continue  # not an option (no calibrated IV) — skip
            parsed = row.get("_parsed") or parse_tradingsymbol(sym)
            if not parsed or parsed.get("kind") != "opt":
                continue
            strike = float(parsed.get("strike") or 0)
            und    = (parsed.get("underlying") or "").upper()
            spot   = float(self._underlyings.get(und) or 0)
            if strike <= 0 or spot <= 0:
                # No moneyness reference — fall back to flat atm_delta.
                skew_extra = 0.0
            else:
                m = strike / spot
                if m < 1.0:
                    # OTM put (K below S) — extra IV proportional to depth.
                    skew_extra = put_skew * (1.0 - m)
                elif m > 1.0:
                    # OTM call (K above S) — extra IV proportional to depth.
                    skew_extra = call_skew * (m - 1.0)
                else:
                    skew_extra = 0.0
            new = max(0.0001, min(5.0, float(old) + atm_delta + skew_extra))
            self._iv_cache[sym] = new
            changes.append({
                "section": "positions", "account": row.get("account"),
                "symbol":  sym, "col": "iv",
                "prev":    old, "next": new,
                "delta":   new - old,
                "reason":  f"set_iv_skew (atm{atm_delta:+.2f} extra{skew_extra:+.3f})",
                "bid":     None, "ask": None,
            })
        return changes

    def _apply_set_setting(self, move: dict) -> list[dict]:
        """
        Non-market event primitive — override a DB-backed setting in
        the live cache for the duration of this sim run. Examples:
          - lower an alert threshold to verify an agent that wouldn't
            otherwise fire on a small sim move
          - flip a capability flag to test gating behaviour

        Reverted on Stop / Clear via `_revert_settings()`. Does NOT
        touch the settings table — purely an in-memory override.
        """
        from backend.shared.helpers.settings import _CACHE
        key   = move.get("key")
        value = move.get("value")
        if not isinstance(key, str) or value is None:
            return []
        old = _CACHE.get(key)
        if not hasattr(self, "_setting_overrides"):
            self._setting_overrides = {}
        if key not in self._setting_overrides:
            self._setting_overrides[key] = old  # remember pre-sim value
        _CACHE[key] = value
        return [{
            "section": "settings",
            "account": None,
            "symbol":  "",
            "col":     key,
            "prev":    old,
            "next":    value,
            "delta":   None,
            "reason":  "set_setting",
            "bid":     None,
            "ask":     None,
        }]

    def _revert_settings(self) -> None:
        """Restore any settings the sim mutated via `set_setting`. Called
        from `stop()` so the operator's prod values aren't left changed
        after a sim run."""
        if not getattr(self, "_setting_overrides", None):
            return
        from backend.shared.helpers.settings import _CACHE
        for key, old in self._setting_overrides.items():
            if old is None:
                _CACHE.pop(key, None)
            else:
                _CACHE[key] = old
        self._setting_overrides = {}

    def _ref_now(self) -> datetime:
        """Wall clock + simulated offset. Used everywhere we previously
        called `datetime.now()` directly for derivative pricing — keeps
        DTE consistent with the `advance_clock` primitive."""
        return datetime.now() + timedelta(minutes=self._sim_clock_offset_minutes)

    def _refresh(self, section: str, row: dict) -> None:
        if section == "positions":
            _recompute_position_row(row, self.spread_pct)
        elif section == "holdings":
            _recompute_holding_row(row)
        elif section == "watchlist":
            # Watchlist rows: just refresh bid/ask from the new
            # last_price + spread. Day-change derives from close_price
            # the same way holdings does.
            lp    = float(row.get("last_price") or 0)
            close = float(row.get("close_price") or 0) or None
            half  = max(0.0, float(self.spread_pct)) / 2.0
            row["bid"] = lp * (1.0 - half) if lp else 0.0
            row["ask"] = lp * (1.0 + half) if lp else 0.0
            if close:
                row["day_change"]            = lp - close
                row["day_change_percentage"] = (lp - close) / close

    # ── Paper-trade chase engine (delegated to PaperTradeEngine) ─────

    def register_open_order(self, order: dict) -> None:
        """
        Called by `_sim_paper_trade` after the initial AlgoOrder row is
        persisted. Forwards into the PaperTradeEngine the driver was
        constructed with — kept on SimDriver as a thin facade so
        existing callers (`actions.py::_write_sim_order`) don't need to
        know the engine was lifted out.
        """
        self._paper.register_open_order(order)

    def _forward_chase_event(self, evt: dict) -> None:
        """
        Translate PaperTradeEngine events into the simulator's tick-log
        shape so the Simulator log panel keeps rendering chase progress
        the same way it always has. Mode 2's standalone PaperTradeEngine
        keeps its own buffer; only the simulator forwards into the
        scenario tick stream.
        """
        order = evt.get("order") or {}
        self._tick_log.append({
            "ts":         evt.get("ts") or datetime.now().isoformat(timespec="seconds"),
            "tick_index": self.tick_index,
            "scenario":   self.scenario_slug,
            "kind":       evt.get("kind"),
            "moves":      [],
            "changes":    [],
            "note":       evt.get("note"),
            "order":      order,
        })

    def _check_auto_complete(self) -> None:
        """
        Halt the sim when there's nothing left to simulate. Two triggers:
          - _positions_rows is empty (every position closed out — either
            via chase fills or a symbol filter that matched nothing), AND
          - no OPEN sim orders remain (so we're not mid-chase)
        Records a 'completed' entry in the tick log before stopping so the
        operator sees why the loop exited.
        """
        if not self.active:
            return
        if self._positions_rows or self._paper.has_open_orders():
            return
        self._record_tick(
            kind="completed", moves=[], changes=[],
            note="Simulation complete — no positions left to simulate.",
        )
        logger.warning("[SIM] Auto-completed — no positions remaining")
        self.active = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def _change(self, section: str, row: dict, prev: float, new: float,
                *, reason: str) -> dict:
        return {
            "section": section,
            "account": row.get("account"),
            "symbol":  row.get("tradingsymbol", ""),
            "col":     "last_price",
            "prev":    prev,
            "next":    new,
            "delta":   new - prev,
            "reason":  reason,
            # Derived bid/ask included so the tick log panel can show the
            # spread that each position currently quotes.
            "bid":     row.get("bid"),
            "ask":     row.get("ask"),
        }

    # ── Legacy aggregate patch (kept so older scenarios still work) ───

    def _apply_legacy_patch(self, patch: dict) -> list[dict]:
        """
        Apply a flat-dotted-key patch (old Model-A shape). Kept so scenarios
        written before the price-driver cutover keep working — the sim
        mutates the per-symbol state if the row exists, else synthesises an
        aggregate stub row.
        """
        changes: list[dict] = []
        for key, val in patch.items():
            parts = key.split(".", 2)
            if len(parts) != 3:
                logger.warning(f"[SIM] malformed legacy patch key '{key}'")
                continue
            section, account, col = parts
            rows = {
                "holdings":  self._holdings_rows,
                "positions": self._positions_rows,
                "margins":   self._margins_rows,
            }.get(section)
            if rows is None:
                logger.warning(f"[SIM] unknown section '{section}' in patch")
                continue
            match = next((r for r in rows if r.get("account") == account), None)
            if match is None:
                match = {"account": account}
                rows.append(match)
            prev = match.get(col)
            match[col] = val
            changes.append({
                "section": section, "account": account, "symbol": "",
                "col": col, "prev": prev, "next": val,
                "delta": (val - prev) if isinstance(prev, (int, float)) and isinstance(val, (int, float)) else None,
                "reason": "legacy-patch",
            })
        return changes

    # ── Live-book seeding ────────────────────────────────────────────

    def seed_live(self, user_id: int | None = None,
                  accounts: list[str] | None = None) -> dict:
        """
        Snapshot holdings + positions + margins from the real book into
        the driver's `_live_snapshot` field. Holdings are included so
        the simulator can exercise day_pct / day_rate_abs / day_rate_pct
        agents that condition on holdings P&L.

        `accounts` (optional) scopes the snapshot to only the listed
        account codes (e.g. ["ZG0790"]). None or [] = all loaded
        accounts (historical behaviour). The filter is applied AFTER
        the broker fetch so the @for_all_accounts decorator still
        runs as designed; we just drop unwanted rows from the
        snapshot.

        When `user_id` is given, additionally fetches that user's
        watchlists and seeds zero-qty rows for every watchlist item so
        the sim can drive watchlist symbol prices independent of the
        operator's actual book. Watchlist rows live in their own
        `watchlist` section; move primitives address them via
        `watchlist.<list_name>.<symbol>` scopes.

        Note: when called from sync paths (e.g. SimDriver.start auto-
        seed), user_id should be left None — only the async route
        handlers pass user_id and call seed_live_async() instead.
        """
        assert_enabled()
        from backend.shared.helpers import broker_apis

        try:
            df_h = pd.concat(broker_apis.fetch_holdings(),  ignore_index=True)
            df_p = pd.concat(broker_apis.fetch_positions(), ignore_index=True)
            df_m = pd.concat(broker_apis.fetch_margins(),   ignore_index=True)
        except Exception as e:
            raise SimGuardError(f"Live-book fetch failed: {e}")

        # Keep real account codes in the sim book — Telegram + email sim
        # alerts go to the owner and reading `ZG####` everywhere made it
        # impossible to tell which account fired. Public sim endpoints
        # are admin-guarded, so there's no leak path.

        holdings  = df_h.fillna(0).to_dict(orient="records") if not df_h.empty else []
        positions = df_p.fillna(0).to_dict(orient="records") if not df_p.empty else []
        margins   = df_m.fillna(0).to_dict(orient="records") if not df_m.empty else []

        # Per-account scope filter — when caller passes a non-empty
        # list, drop every row whose `account` isn't in it. Applied
        # uniformly across all three buckets so per-account agents
        # see a coherent slice of the book.
        if accounts:
            scoped = {str(a).strip().upper() for a in accounts if a}
            def _in_scope(r):
                acct = str(r.get("account") or "").strip().upper()
                return acct in scoped
            holdings  = [r for r in holdings  if _in_scope(r)]
            positions = [r for r in positions if _in_scope(r)]
            margins   = [r for r in margins   if _in_scope(r)]

        for row in positions:
            _recompute_position_row(row)
        for row in holdings:
            _recompute_holding_row(row)

        # Watchlist rows are seeded via the async path
        # `seed_live_async(user_id)` from the route handler — the sync
        # path leaves them empty to avoid blocking on the async DB
        # session inside a sync caller.
        watchlist_rows: list[dict] = []

        self._live_snapshot = {
            "holdings":    holdings,
            "positions":   positions,
            "margins":     margins,
            "watchlist":   watchlist_rows,
            "snapshot_at": datetime.now().isoformat(timespec="seconds"),
            # Echo the filter back so start() can decide whether the
            # cached snapshot matches the current run's account scope.
            "accounts_filter": sorted([str(a).strip().upper()
                                       for a in (accounts or []) if a]),
        }
        logger.info(
            f"[SIM] seed-live: {len(positions)} positions · {len(margins)} margins · "
            f"{len(holdings)} holdings"
        )
        return {
            "snapshot_at":     self._live_snapshot["snapshot_at"],
            "positions_count": len(positions),
            "margins_count":   len(margins),
            "watchlist_count": len(self._live_snapshot.get("watchlist", []) or []),
            "accounts":        sorted({str(r.get("account", "")) for r in positions + margins if r.get("account")}),
            # Distinct tradingsymbols in the snapshot — populates the
            # Symbol picker on /admin/simulator.
            "symbols":         sorted({str(r.get("tradingsymbol", ""))
                                       for r in positions if r.get("tradingsymbol")}),
        }

    async def seed_live_async(self, user_id: int | None = None) -> dict:
        """Async wrapper around `seed_live()` that additionally fetches
        the user's watchlist items + quotes. The sync core (broker
        fetches via @for_all_accounts) runs the same as before; the
        watchlist fetch runs in the same event loop without blocking.
        """
        # Run the sync seed first — its broker calls are wrapped in a
        # ThreadPoolExecutor by Connections() / broker_apis, so the
        # event loop isn't blocked.
        manifest = self.seed_live(user_id=None)
        if user_id is not None:
            try:
                watchlist_rows = await _fetch_user_watchlist_rows(user_id)
                self._live_snapshot["watchlist"] = watchlist_rows
                manifest["watchlist_count"] = len(watchlist_rows)
                logger.info(f"[SIM] seed-live watchlist: {len(watchlist_rows)} rows for user_id={user_id}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[SIM] watchlist seed failed for user_id={user_id}: {exc}")
        return manifest

    # ── Tick log ─────────────────────────────────────────────────────

    def _record_tick(self, *, kind: str, moves: list, changes: list[dict],
                     note: str = "") -> None:
        self._tick_log.append({
            "ts":         datetime.now().isoformat(timespec="seconds"),
            "tick_index": self.tick_index,
            "scenario":   self.scenario_slug,
            "kind":       kind,
            "moves":      moves,
            "changes":    changes,
            "note":       note,
        })

    def recent_ticks(self, limit: int = 100) -> list[dict]:
        """Return the most recent `limit` ticks (oldest-first)."""
        limit = max(1, min(int(limit), TICK_LOG_LIMIT))
        return list(self._tick_log)[-limit:]

    # ── Price history ────────────────────────────────────────────────

    def _capture_price_history(self) -> None:
        """Append one (ts, ltp, bid, ask) per active symbol to the rolling
        per-symbol buffer. Called once per tick, after moves are applied.
        Also captures every known underlying spot into a parallel buffer
        so the chart UI can render the underlying line alongside its
        derived options."""
        # Millisecond precision is critical for the chart: when the sim
        # tick rate is sub-second (rate_ms < 1000), seconds-rounded
        # timestamps collapse multiple captures to the same `ts` string.
        # The frontend xDomain check then sees `hi == lo`, treats the
        # domain as degenerate, and renders an empty chart even though
        # ticks are accumulating in the deque. Capturing at ms-precision
        # gives every tick a unique x and the line draws normally.
        ts = datetime.now().isoformat(timespec="milliseconds")
        for r in self._positions_rows:
            sym = str(r.get("tradingsymbol") or "")
            if not sym:
                continue
            ltp = r.get("last_price")
            if ltp is None:
                continue
            buf = self._price_history.get(sym)
            if buf is None:
                buf = deque(maxlen=PRICE_HISTORY_LIMIT)
                self._price_history[sym] = buf
            buf.append({
                "ts":  ts,
                "ltp": float(ltp),
                "bid": float(r["bid"]) if r.get("bid") is not None else None,
                "ask": float(r["ask"]) if r.get("ask") is not None else None,
            })
        for name, spot in self._underlyings.items():
            buf = self._underlying_history.get(name)
            if buf is None:
                buf = deque(maxlen=PRICE_HISTORY_LIMIT)
                self._underlying_history[name] = buf
            buf.append({"ts": ts, "ltp": float(spot), "bid": None, "ask": None})

    def price_history(self, symbol: str, *, since: str | None = None,
                      limit: int = 600) -> list[dict]:
        """Per-symbol tick stream for the chart endpoint. `since` is an
        ISO timestamp; entries strictly after `since` are returned. Looks
        up underlyings and contracts in the same flat namespace — chart
        clients don't have to know whether a name is a derivative or its
        underlying."""
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
        """Sorted list of symbols with at least one captured tick. Includes
        underlyings (e.g. NIFTY) alongside contracts so the chart panel
        can render both."""
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
        return und if und in self._underlyings else None

    # ── Convenience ──────────────────────────────────────────────────

    def scenarios_manifest(self) -> list[dict]:
        out = []
        for s in load_scenarios():
            initial = s.get("initial") or {}
            has_initial = bool(
                initial.get("holdings") or initial.get("positions") or initial.get("margins")
            )
            # Default tick pct values — same shape as _tick_pcts_for_ui
            # above. Lets the UI show editable defaults before Start.
            tick_pcts: list[float | None] = []
            for t in (s.get("ticks") or []):
                pct = None
                for m in (t.get("moves") or []):
                    if (m.get("type") or "").lower() == "pct":
                        try:
                            pct = float(m.get("value"))
                        except (TypeError, ValueError):
                            pass
                        break
                tick_pcts.append(pct)
            # Distinct symbols from the scenario's scripted initial
            # positions — lets the Symbol picker show picker options
            # even when the operator hasn't loaded the live book yet.
            init_syms = sorted({
                str(p.get("tradingsymbol", ""))
                for p in (initial.get("positions") or [])
                if p.get("tradingsymbol")
            })
            # Walk-shape detection — flag when ANY tick contains a
            # random_walk or underlying_random_walk move. The UI uses
            # this to surface drift / vol / seed inputs only for
            # walk-style scenarios.
            has_walk = any(
                (m.get("type") or "").lower() in ("random_walk", "underlying_random_walk")
                for t in (s.get("ticks") or [])
                for m in (t.get("moves") or [])
            )
            out.append({
                "slug":            s.get("slug"),
                "name":            s.get("name") or s.get("slug"),
                "description":     s.get("description", ""),
                "mode":            s.get("mode") or ("symbol" if s.get("ticks", [{}])[0].get("moves") else "aggregate"),
                "ticks":           len(s.get("ticks", []) or []),
                "has_initial":     has_initial,
                "tick_pcts":       tick_pcts,
                "initial_symbols": init_syms,
                "has_walk":        has_walk,
            })
        return out


def get_driver() -> SimDriver:
    return SimDriver.instance()
