"""
Operator health check endpoint.

GET /api/admin/health — single round-trip snapshot of the platform state:
  branch, git HEAD, broker accounts, DB row counts, in-process cache size,
  simulator status, paper-engine status, and per-account source IPs.

Admin-only. No demo access.
"""

import subprocess
import time as _time
from datetime import datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, get, post, delete
from litestar.params import Parameter as _LP
from litestar.exceptions import HTTPException
from sqlalchemy import func, select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session, shared_async_session
from backend.api.models import (
    Agent,
    AgentEvent,
    AlgoOrder,
    BrokerAccount,
    NewsHeadline,
    User,
)
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class BrokerStatus(msgspec.Struct):
    account: str        # masked — ZG#### even for admin; IPs identify better
    status: str         # "LOADED" / "PENDING" / "DISABLED"
    api_key_last4: str  # last 4 chars of plaintext api_key for identification
    source_ip: str      # empty string when not configured


class DBStats(msgspec.Struct):
    users: int
    agents: int
    algo_orders: int
    agent_events: int
    news_headlines: int


class CacheStats(msgspec.Struct):
    keys: int   # rough count of live entries in the in-process TTL cache


class SimStatus(msgspec.Struct):
    active: bool
    scenario: Optional[str]
    tick_count: int


class PaperStatus(msgspec.Struct):
    enabled: bool       # True only when deploy_branch == 'main'
    open_order_count: int


class SparklineWarmStatus(msgspec.Struct):
    symbols_cached: int          # number of symbols with daily data in last warm cycle
    last_warmed_at: Optional[str]  # ISO-8601 UTC of last successful warm, or None


class TickerStatus(msgspec.Struct):
    started: bool            # connect() was called
    connected: bool          # on_connect has fired and socket is live
    subscribed_count: int    # number of instrument tokens subscribed
    ticks_held: int          # number of tokens with at least one live tick
    # Per-symbol staleness (added Jun 2026 after operator reported
    # missing ticks). "Stale" = subscribed token whose last tick is
    # older than 60s, OR has never been ticked.
    stale_count: int = 0          # number of subscribed tokens with stale ticks
    max_age_seconds: float = 0.0  # oldest tick age across all subscribed tokens
    stale_top: list[str] = []     # up to 20 worst offenders: "SYMBOL@<age>s" or "SYMBOL@never"
    # Auto-failover state machine surface (added Jun 2026 with the
    # conn_service ticker-swap watchdog). Additive — all default 0/""
    # so pre-cutover deployments (where the watchdog isn't populating
    # these) still deserialize cleanly.
    active_account: str = ""          # Kite account currently bound to the WS
    failover_list: list[str] = []     # priority-ordered eligible Kite accounts
    consecutive_unhealthy: int = 0    # bad watchdog cycles on active account
    swaps_last_hour: int = 0          # auto-failover swaps within the last 3600s
    last_swap_at: float = 0.0         # unix ts of most recent swap (0 = never)


class HealthResponse(msgspec.Struct):
    branch: str
    git_hash: str       # short commit hash, "unknown" on failure
    git_subject: str    # commit subject, "unknown" on failure
    broker_accounts: list[BrokerStatus]
    db: DBStats
    cache: CacheStats
    sim: SimStatus
    paper: PaperStatus
    sparkline_warm: SparklineWarmStatus
    ticker: TickerStatus
    ipv6: list[str]     # source_ip values configured across all accounts
    persistence: dict   # write_queue health surface (disk + db workers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_hash() -> str:
    """Short commit hash for HEAD; 'unknown' if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _git_subject() -> str:
    """Commit subject for HEAD; 'unknown' if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


async def _fetch_broker_statuses() -> tuple[list[BrokerStatus], list[str]]:
    try:
        from backend.brokers.connections import Connections
        loaded_accounts: set[str] = set(Connections().conn.keys())
    except Exception:
        loaded_accounts = set()

    # Cutover branch — when conn_service owns sessions, the canonical
    # loaded-account list lives there. Merge to keep the navbar pill
    # honest (PENDING → LOADED for everything conn_service reports).
    import os as _os
    if _os.environ.get("RAMBOQ_USE_CONN_SERVICE", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        try:
            from backend.brokers.client.remote_broker import list_remote_accounts
            for r in list_remote_accounts():
                acct = r.get("account")
                if acct:
                    loaded_accounts.add(acct)
        except Exception:
            pass

    statuses: list[BrokerStatus] = []
    ipv6_list: list[str] = []

    try:
        async with shared_async_session() as session:
            rows = (await session.execute(
                select(BrokerAccount).order_by(BrokerAccount.account)
            )).scalars().all()

        for row in rows:
            if not row.is_active:
                status = "DISABLED"
            elif row.account in loaded_accounts:
                status = "LOADED"
            else:
                status = "PENDING"

            api_key_last4 = (row.api_key or "")[-4:] or "????"
            source_ip = row.source_ip or ""

            statuses.append(BrokerStatus(
                account=row.account,
                status=status,
                api_key_last4=api_key_last4,
                source_ip=source_ip,
            ))

            if source_ip:
                ipv6_list.append(source_ip)

    except Exception as exc:
        logger.warning(f"health: broker_accounts query failed: {exc}")

    return statuses, ipv6_list


async def _fetch_db_stats() -> DBStats:
    try:
        async with async_session() as session:
            users = (await session.execute(
                select(func.count()).select_from(User)
            )).scalar_one()
            agents = (await session.execute(
                select(func.count()).select_from(Agent)
            )).scalar_one()
            algo_orders = (await session.execute(
                select(func.count()).select_from(AlgoOrder)
            )).scalar_one()
            agent_events = (await session.execute(
                select(func.count()).select_from(AgentEvent)
            )).scalar_one()
            news_headlines = (await session.execute(
                select(func.count()).select_from(NewsHeadline)
            )).scalar_one()
        return DBStats(
            users=int(users),
            agents=int(agents),
            algo_orders=int(algo_orders),
            agent_events=int(agent_events),
            news_headlines=int(news_headlines),
        )
    except Exception as exc:
        logger.warning(f"health: DB stats query failed: {exc}")
        return DBStats(users=0, agents=0, algo_orders=0, agent_events=0, news_headlines=0)


def _cache_stats() -> CacheStats:
    try:
        from backend.api import cache as _cache_mod
        return CacheStats(keys=len(_cache_mod._store))
    except Exception:
        return CacheStats(keys=0)


def _sim_status() -> SimStatus:
    try:
        from backend.api.algo.sim.driver import SimDriver
        snap = SimDriver.instance().snapshot()
        return SimStatus(
            active=bool(snap.get("active", False)),
            scenario=snap.get("scenario") or None,
            tick_count=int(snap.get("tick_index", 0)),
        )
    except Exception:
        return SimStatus(active=False, scenario=None, tick_count=0)


def _sparkline_warm_status() -> SparklineWarmStatus:
    try:
        from backend.api.routes.quote import _spark_warm_symbols, _spark_warm_at
        return SparklineWarmStatus(
            symbols_cached=_spark_warm_symbols,
            last_warmed_at=_spark_warm_at,
        )
    except Exception:
        return SparklineWarmStatus(symbols_cached=0, last_warmed_at=None)


def _ticker_status() -> TickerStatus:
    """Return a snapshot of the KiteTicker WebSocket state.

    Under cutover (RAMBOQ_USE_CONN_SERVICE=1), this reads from
    conn_service via `MmapTickReader.status()` which round-trips the
    `/internal/ticker/status` endpoint. Auto-failover fields
    (`active_account`, `failover_list`, `consecutive_unhealthy`,
    `swaps_last_hour`, `last_swap_at`) originate in conn_service's
    TickerManager and flow through unchanged.
    """
    try:
        from backend.brokers.kite_ticker import get_ticker
        snap = get_ticker().status()
        return TickerStatus(
            started=bool(snap.get("started", False)),
            connected=bool(snap.get("connected", False)),
            subscribed_count=int(snap.get("subscribed_count", 0)),
            ticks_held=int(snap.get("ticks_held", 0)),
            stale_count=int(snap.get("stale_count", 0)),
            max_age_seconds=float(snap.get("max_age_seconds", 0.0)),
            stale_top=list(snap.get("stale_top", [])),
            active_account=str(snap.get("active_account", "") or ""),
            failover_list=list(snap.get("failover_list", []) or []),
            consecutive_unhealthy=int(snap.get("consecutive_unhealthy", 0)),
            swaps_last_hour=int(snap.get("swaps_last_hour", 0)),
            last_swap_at=float(snap.get("last_swap_at", 0.0)),
        )
    except Exception:
        return TickerStatus(started=False, connected=False,
                            subscribed_count=0, ticks_held=0)


def _paper_status() -> PaperStatus:
    branch = config.get("deploy_branch", "")
    enabled = branch == "main"
    try:
        from backend.api.algo.paper import get_prod_paper_engine
        engine = get_prod_paper_engine()
        open_count = sum(
            1 for o in engine._open_orders if o.get("status") == "OPEN"
        )
        return PaperStatus(enabled=enabled, open_order_count=open_count)
    except Exception:
        return PaperStatus(enabled=enabled, open_order_count=0)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class HealthController(Controller):
    path = "/api/admin/health"
    guards = [admin_guard]

    @get("")
    async def get_health(self) -> HealthResponse:
        """
        Single-call platform health snapshot for the operator.

        Returns branch, git HEAD, per-account broker status, DB row counts,
        in-process cache size, simulator state, paper-engine state, and the
        list of IPv6 source addresses configured per broker account.
        """
        try:
            broker_statuses, ipv6_list = await _fetch_broker_statuses()
            db_stats = await _fetch_db_stats()
            cache_stats = _cache_stats()
            sim = _sim_status()
            paper = _paper_status()
            sparkline_warm = _sparkline_warm_status()
            ticker = _ticker_status()
            git_hash = _git_hash()
            git_subject = _git_subject()
            branch = str(config.get("deploy_branch") or "?")

            from backend.api.persistence import write_queue
            persistence = write_queue.get_health()

            try:
                from backend.api.persistence import (
                    ohlcv_store, instruments_store, holidays_store,
                    intraday_store, runtime_state,
                )
                # Per-store tier-hit metrics — exposes the actual
                # cache-vs-broker pressure ratio the persistence layer
                # is absorbing (slice AJ). Counters reset on restart.
                persistence["stores"] = {
                    "ohlcv_daily":          ohlcv_store._ohlcv_store.get_metrics(),
                    "instruments_snapshot": instruments_store._instruments_store.get_metrics(),
                    "holidays_snapshot":    holidays_store._holidays_store.get_metrics(),
                    "intraday_bars":        intraday_store._intraday_store.get_metrics(),
                }
                persistence["mode"]   = runtime_state.get_mode()
                # Back-compat: dashboards reading `bypass` (slice X surface)
                # still work — true whenever mode is non-default.
                persistence["bypass"] = runtime_state.is_bypass_on()
                # Per-symbol BEL-race monitor — operator-visible. Non-zero
                # entries mean the historical handler returned empty bars
                # for that symbol at least N times today. Resets at IST
                # midnight via key sweep in _record_first_cold_empty.
                try:
                    from backend.api.routes.options import get_first_cold_empty_counts
                    persistence["ohlcv_first_cold_empty"] = get_first_cold_empty_counts()
                except Exception:
                    persistence["ohlcv_first_cold_empty"] = {}
            except Exception:
                pass  # stores block is best-effort; never break the health check

            return HealthResponse(
                branch=branch,
                git_hash=git_hash,
                git_subject=git_subject,
                broker_accounts=broker_statuses,
                db=db_stats,
                cache=cache_stats,
                sim=sim,
                paper=paper,
                sparkline_warm=sparkline_warm,
                ticker=ticker,
                ipv6=ipv6_list,
                persistence=persistence,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"HealthController.get_health failed: {exc}")
            raise HTTPException(status_code=500, detail="Health check failed")


# ---------------------------------------------------------------------------
# Persistence admin — three-state refresh mode + per-store invalidate
# ---------------------------------------------------------------------------

class _ModeResponse(msgspec.Struct):
    mode: str    # "off" | "soft" | "hard"


class _InvalidateResponse(msgspec.Struct):
    store:        str
    rows_deleted: int


class PersistenceAdminController(Controller):
    """Defect-recovery surface for the refresh-cycle pipeline
    (cache → DB → broker API).

    Three-state refresh mode (operator: "they are the two states in
    persistence.bypass — design and code accordingly"):

      off  — normal hierarchy. Default. All store reads consult
             cache + DB before hitting broker.
      soft — non-ticker stores bypass cache + DB. Every read fetches
             from broker and writes back through the queue, healing
             the persistent tiers. Live ticker stream untouched.
      hard — soft + ticker recycle on the transition. The in-memory
             _tick_map + subscriptions rebuild from scratch. Brief
             LTP gap during reconnect; SSE clients auto-reconnect
             so no functional impact.

    Endpoints:
      GET  /api/admin/persistence/mode             → {mode}
      POST /api/admin/persistence/mode/{off|soft|hard}
      POST /api/admin/persistence/invalidate?store=…&symbol=…&exchange=…
    """
    path = "/api/admin/persistence"
    guards = [admin_guard]

    @get("/mode")
    async def get_mode_endpoint(self) -> _ModeResponse:
        from backend.api.persistence import runtime_state
        return _ModeResponse(mode=runtime_state.get_mode())

    @post("/mode/off")
    async def mode_off(self) -> _ModeResponse:
        from backend.api.persistence import runtime_state
        runtime_state.set_mode("off")
        return _ModeResponse(mode="off")

    @post("/mode/soft")
    async def mode_soft(self) -> _ModeResponse:
        from backend.api.persistence import runtime_state
        runtime_state.set_mode("soft")
        return _ModeResponse(mode="soft")

    @post("/mode/hard")
    async def mode_hard(self) -> _ModeResponse:
        from backend.api.persistence import runtime_state
        runtime_state.set_mode("hard")
        return _ModeResponse(mode="hard")

    @post("/invalidate")
    async def invalidate(
        self,
        store:    str = _LP(required=True),
        symbol:   Optional[str] = None,
        exchange: Optional[str] = None,
    ) -> _InvalidateResponse:
        from backend.api.persistence import runtime_state
        if store == "ohlcv_daily":
            n = await runtime_state.invalidate_ohlcv(symbol, exchange)
        elif store == "instruments_snapshot":
            n = await runtime_state.invalidate_instruments(exchange)
        elif store == "holidays_snapshot":
            n = await runtime_state.invalidate_holidays(exchange)
        elif store == "intraday_bars":
            n = await runtime_state.invalidate_intraday(symbol, exchange)
        elif store == "all":
            n = (
                await runtime_state.invalidate_ohlcv(symbol, exchange)
                + await runtime_state.invalidate_instruments(exchange)
                + await runtime_state.invalidate_holidays(exchange)
                + await runtime_state.invalidate_intraday(symbol, exchange)
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown store '{store}' — use ohlcv_daily, "
                    "instruments_snapshot, holidays_snapshot, intraday_bars, or all"
                ),
            )
        return _InvalidateResponse(store=store, rows_deleted=n)

    @post("/backfill")
    async def backfill(
        self,
        kind: str = _LP(query="kind", default="both"),
    ) -> dict:
        """Trigger an on-demand backfill for the operator's symbol universe.

        Query param `kind`:
          daily    — ohlcv_daily only (365-day window)
          intraday — intraday_bars for today only (30minute interval)
          both     — both in sequence (default)

        The backfill runs in a fire-and-forget asyncio.Task so the HTTP
        response returns immediately with the task's name.  Progress is
        visible in the application log.

        The backfill uses the same symbol universe as the startup warm
        (watchlist + holdings + positions + movers, 300-symbol cap).
        Rate-limit cool-off is respected — symbols whose broker is cooling
        off are skipped and noted in the log.
        """
        if kind not in ("daily", "intraday", "both"):
            raise HTTPException(
                status_code=400,
                detail="kind must be one of: daily, intraday, both",
            )

        from backend.api.background import _task_warm_backfill
        import asyncio as _asyncio

        # Reset the singleton guard so the on-demand run fires even if the
        # startup task already ran.
        _task_warm_backfill._fired = False  # type: ignore[attr-defined]

        async def _run_backfill() -> None:
            from backend.api.persistence.backfill import (
                backfill_ohlcv_daily,
                backfill_intraday_today,
            )
            from backend.shared.helpers.date_time_utils import is_any_segment_open
            from backend.shared.helpers.date_time_utils import timestamp_indian

            # Build universe (same logic as _task_warm_backfill).
            symbols: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            try:
                from backend.api.database import async_session as _as
                from backend.api.models import WatchlistItem
                from sqlalchemy import select as _sa_select
                from backend.api.routes.watchlist import (
                    _resolve_mcx_commodity, _resolve_cds_currency,
                )
                async with _as() as sess:
                    rows = (await sess.execute(
                        _sa_select(WatchlistItem.tradingsymbol, WatchlistItem.exchange)
                    )).all()
                for row in rows:
                    sym  = (row.tradingsymbol or "").upper().strip()
                    exch = (row.exchange or "NSE").upper().strip()
                    if not sym:
                        continue
                    if exch == "MCX" and sym.isalpha() and len(sym) <= 12:
                        resolved = await _resolve_mcx_commodity(sym)
                        if resolved:
                            sym = resolved.upper().strip()
                    elif exch == "CDS" and sym.isalpha() and len(sym) <= 12:
                        resolved = await _resolve_cds_currency(sym)
                        if resolved:
                            sym = resolved.upper().strip()
                    key = (sym, exch)
                    if key not in seen:
                        seen.add(key)
                        symbols.append(key)
            except Exception as exc:
                logger.warning(f"backfill/route: watchlist collect failed: {exc}")
            try:
                import pandas as _pd
                from backend.brokers import broker_apis as _ba
                for fetch_fn, default_exch in ((_ba.fetch_holdings, "NSE"), (_ba.fetch_positions, "NFO")):
                    dfs = fetch_fn()
                    df  = _pd.concat(dfs, ignore_index=True) if dfs else _pd.DataFrame()
                    if not df.empty and "tradingsymbol" in df.columns:
                        _exch_col = df["exchange"] if "exchange" in df.columns else _pd.Series([default_exch] * len(df))
                        for s, e in zip(df["tradingsymbol"], _exch_col):
                            sym  = str(s or "").upper().strip()
                            exch = str(e or default_exch).upper().strip()
                            if sym:
                                k = (sym, exch)
                                if k not in seen:
                                    seen.add(k)
                                    symbols.append(k)
            except Exception as exc:
                logger.warning(f"backfill/route: holdings/positions collect failed: {exc}")
            try:
                from backend.shared.helpers.mover_universe import mover_warm_pairs as _mwp
                _mover_set  = set(_mwp())
                book_pairs  = [p for p in symbols if p not in _mover_set]
                mover_pairs = [p for p in symbols if p in _mover_set]
                remaining   = max(0, 300 - len(book_pairs))
                symbols     = book_pairs + mover_pairs[:remaining]
                for key in _mwp():
                    if key not in seen:
                        seen.add(key)
                        symbols.append(key)
                symbols = symbols[:300]
            except Exception as exc:
                logger.warning(f"backfill/route: mover universe collect failed: {exc}")
                symbols = symbols[:300]

            if kind in ("daily", "both"):
                try:
                    r = await backfill_ohlcv_daily(symbols, target_days=365, max_concurrent=3)
                    logger.info(f"backfill/route: ohlcv_daily done — {r}")
                except Exception as exc:
                    logger.error(f"backfill/route: ohlcv_daily failed: {exc}")

            if kind in ("intraday", "both"):
                try:
                    now_ist = timestamp_indian()
                    if is_any_segment_open(now_ist):
                        r2 = await backfill_intraday_today(symbols, interval="30minute", max_concurrent=3)
                        logger.info(f"backfill/route: intraday_today done — {r2}")
                    else:
                        logger.info("backfill/route: intraday_today skipped — no segment open")
                except Exception as exc:
                    logger.error(f"backfill/route: intraday_today failed: {exc}")

        task = _asyncio.create_task(_run_backfill(), name=f"admin-backfill-{kind}")
        return {"status": "started", "kind": kind, "task": task.get_name()}


# ---------------------------------------------------------------------------
# Broker auth-health endpoint
# ---------------------------------------------------------------------------

_BROKER_HEALTH_FRESH_WINDOW_S: float = 300.0   # 5 min — "green" threshold


class BrokerAccountHealth(msgspec.Struct):
    """Per-account auth/freshness state for the navbar badge.

    `is_active_ticker` is True only for the ONE Kite account currently
    running the ticker WebSocket in conn_service. Displayed as a small
    'active' chip next to the account row in BrokerHealthBadge.

    Circuit-breaker fields (added Jul 2026 — defaults allow older
    conn_service responses to deserialize cleanly on rolling deploys):
      circuit_state         — 'closed' | 'half-open' | 'open'
      consecutive_fail_count — number of consecutive failed fetches
      circuit_open_until    — ISO-8601 UTC when the breaker reopens,
                              or None when closed/half-open
    """
    account: str
    broker: str                   # 'kite' | 'dhan' | 'groww' | 'unknown'
    state: str                    # 'green' | 'amber' | 'red'
    reason: str                   # human-readable, IST-stamped
    last_good_at: Optional[str]   # ISO-8601 UTC, or None
    last_check_at: Optional[str]  # ISO-8601 UTC, or None
    is_active_ticker: bool = False  # True only for the Kite account bound to the WS
    # Circuit-breaker fields — default to safe values for back-compat.
    circuit_state: str = "closed"
    consecutive_fail_count: int = 0
    circuit_open_until: Optional[str] = None  # ISO-8601 UTC, or None
    # Per-account poll priority fields (Dhan-only, Jul 2026).
    # Kite/Groww rows carry defaults; UI gates rendering on broker='dhan'.
    poll_priority: str = "hot"
    auto_downgrade_enabled: bool = False
    auto_downgraded_at: Optional[str] = None   # ISO-8601 UTC, or None
    auto_downgrade_reason: Optional[str] = None
    # True when the OPEN/HALF-OPEN state machine is active for this account.
    # False (default) for all accounts except DH6847 (startup migration).
    circuit_breaker_enabled: bool = False


class BrokerHealthResponse(msgspec.Struct):
    accounts: list[BrokerAccountHealth]


def _broker_id_to_label(broker_id: str) -> str:
    if not broker_id:
        return "unknown"
    b = broker_id.lower()
    if "kite" in b or "zerodha" in b:
        return "kite"
    if "dhan" in b:
        return "dhan"
    if "groww" in b:
        return "groww"
    return b


def _ts_to_iso(unix_ts: float | None) -> Optional[str]:
    """Convert a unix timestamp to ISO-8601 UTC string, or None."""
    if not unix_ts:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat(timespec="seconds")


def _ts_to_ist_label(unix_ts: float | None) -> str:
    """Return a short IST HH:MM stamp for a unix timestamp, or empty string."""
    if not unix_ts:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(unix_ts, tz=ZoneInfo("Asia/Kolkata"))
        return dt.strftime("%H:%M IST")
    except Exception:
        return ""


def _derive_account_health(entry: dict, now: float) -> tuple[str, str, str, int, Optional[str]]:
    """Return ``(state, reason, circuit_state, consecutive_fail_count, circuit_open_until_iso)``
    for one _FETCH_HEALTH entry.

    State rules:
      green  — last_ok_at is present AND within the last 5 min
               (circuit must be CLOSED)
      amber  — last_ok_at present but > 5 min ago (stale), OR never tried
      red    — last_fail_at > last_ok_at (most-recent call failed)
               OR circuit breaker is OPEN (overrides auth-ok state)
    """
    last_ok   = entry.get("last_ok_at",   0.0) or 0.0
    last_fail = entry.get("last_fail_at", 0.0) or 0.0
    last_msg  = entry.get("last_fail_msg", "") or ""

    # Circuit-breaker fields (may be absent on legacy entries).
    cb_until  = entry.get("circuit_open_until") or None
    cb_count  = int(entry.get("consecutive_fail_count", 0) or 0)
    if cb_until is not None and now < cb_until:
        cb_state = "open"
    elif cb_until is not None and now >= cb_until:
        cb_state = "half-open"
    else:
        cb_state = "closed"

    cb_until_iso: Optional[str] = _ts_to_iso(cb_until) if cb_until else None

    # Circuit OPEN → always red, with retry timestamp in reason.
    if cb_state == "open":
        label = _ts_to_ist_label(cb_until)
        reason = f"circuit open — auto retry at {label}" if label else "circuit open"
        return "red", reason, cb_state, cb_count, cb_until_iso

    if last_ok == 0.0 and last_fail == 0.0:
        return "amber", "no fetch attempt recorded yet", cb_state, cb_count, cb_until_iso

    if last_fail > last_ok:
        label = _ts_to_ist_label(last_fail)
        reason = f"auth invalid since {label}" if label else "last fetch failed"
        if last_msg:
            short_msg = last_msg[:80]
            reason = f"{reason} — {short_msg}"
        return "red", reason, cb_state, cb_count, cb_until_iso

    # last_ok >= last_fail — account is currently healthy
    age = now - last_ok
    if age <= _BROKER_HEALTH_FRESH_WINDOW_S:
        label = _ts_to_ist_label(last_ok)
        return "green", (f"healthy, last good at {label}" if label else "healthy"), cb_state, cb_count, cb_until_iso
    else:
        label = _ts_to_ist_label(last_ok)
        mins = int(age // 60)
        return "amber", (f"stale — last good {mins} min ago at {label}" if label else f"stale — {mins} min ago"), cb_state, cb_count, cb_until_iso


class BrokerHealthController(Controller):
    """GET /api/admin/broker-health — per-account auth/freshness badge data.

    Derives state from ``_FETCH_HEALTH`` / ``fetch_health_snapshot()`` so the
    same plumbing used by the existing navbar count-chip powers the new auth
    badge.  No new tracking is introduced.

    Poll this every 30 s (visibility-aware).  NOT market-gated — the operator
    needs to know about auth breaks outside market hours too.
    """
    path = "/api/admin/broker-health"
    guards = [admin_guard]

    @get("")
    async def get_broker_health(self) -> BrokerHealthResponse:
        import asyncio as _asyncio
        from backend.brokers.broker_apis import fetch_health_snapshot as _fhs

        # fetch_health_snapshot() may hit conn_service over UDS (sync).
        # Off-load to thread so the event loop stays free.
        health_map: dict[str, dict] = await _asyncio.to_thread(_fhs)

        now = _time.time()

        # Build per-account entries.  Include every account present in
        # health_map; fall back to BrokerAccount rows for broker label.
        broker_label_map: dict[str, str] = {}
        # poll_priority_map: account → dict with poll_priority fields
        poll_priority_map: dict[str, dict] = {}
        try:
            async with shared_async_session() as _sess:
                rows = (await _sess.execute(
                    select(BrokerAccount).order_by(BrokerAccount.account)
                )).scalars().all()
                for row in rows:
                    broker_label_map[row.account] = _broker_id_to_label(
                        row.broker_id or ""
                    )
                    adt = getattr(row, "auto_downgraded_at", None)
                    poll_priority_map[row.account] = {
                        "poll_priority": str(
                            getattr(row, "poll_priority", "hot") or "hot"
                        ),
                        "auto_downgrade_enabled": bool(
                            getattr(row, "auto_downgrade_enabled", False)
                        ),
                        "auto_downgraded_at": (
                            adt.isoformat() if adt else None
                        ),
                        "auto_downgrade_reason": getattr(
                            row, "auto_downgrade_reason", None
                        ),
                        "circuit_breaker_enabled": bool(
                            getattr(row, "circuit_breaker_enabled", False)
                        ),
                    }
        except Exception:
            pass

        # Resolve which Kite account is currently the active ticker so
        # the frontend can render a chip next to that row. Non-fatal:
        # a conn_service outage leaves every row is_active_ticker=False
        # (chip absent) rather than blocking the whole endpoint.
        active_ticker_acct = ""
        try:
            from backend.brokers.kite_ticker import get_ticker as _gt
            active_ticker_acct = str(_gt().current_account() or "")
        except Exception:
            pass

        accounts: list[BrokerAccountHealth] = []
        for acct, entry in health_map.items():
            state, reason, cb_state, cb_count, cb_until_iso = _derive_account_health(entry, now)
            last_ok   = entry.get("last_ok_at",   0.0) or 0.0
            last_fail = entry.get("last_fail_at", 0.0) or 0.0
            last_check = max(last_ok, last_fail)
            _pp = poll_priority_map.get(acct, {})
            accounts.append(BrokerAccountHealth(
                account=acct,
                broker=broker_label_map.get(acct, "unknown"),
                state=state,
                reason=reason,
                last_good_at=_ts_to_iso(last_ok if last_ok else None),
                last_check_at=_ts_to_iso(last_check if last_check else None),
                is_active_ticker=bool(
                    active_ticker_acct and acct == active_ticker_acct
                ),
                circuit_state=cb_state,
                consecutive_fail_count=cb_count,
                circuit_open_until=cb_until_iso,
                poll_priority=_pp.get("poll_priority", "hot"),
                auto_downgrade_enabled=bool(_pp.get("auto_downgrade_enabled", False)),
                auto_downgraded_at=_pp.get("auto_downgraded_at"),
                auto_downgrade_reason=_pp.get("auto_downgrade_reason"),
                circuit_breaker_enabled=bool(
                    _pp.get("circuit_breaker_enabled", False)
                ),
            ))

        # Stable sort: red → amber → green, then account name.
        _order = {"red": 0, "amber": 1, "green": 2}
        accounts.sort(key=lambda a: (_order.get(a.state, 9), a.account))

        return BrokerHealthResponse(accounts=accounts)
