"""
Watchlist CRUD endpoints.

Per-user named symbol groups for monitoring. Each user gets two
auto-seeded watchlists on first creation:

  - "Default"  — empty; the operator's working list
  - "Markets"  — seeded with major Indian indices + MCX commodities

The /quotes endpoint (live LTP / bid / ask / day-change) lands in W2.
This module is CRUD only.

Auth: every endpoint requires `jwt_guard` — partners can manage their
own watchlists too, since the watchlist is monitoring, not platform
admin. The `user_id` filter is derived from the JWT's `sub` claim, so
one user can never see or mutate another's lists.
"""

from datetime import datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, Request, delete, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select, func

from backend.api.auth_guard import jwt_guard, auth_or_demo_guard
# NOTE: MARKETS_DEFAULT is still used by the anonymous-demo path inside
# _demo_watchlist_items (imported lazily there). The Markets watchlist
# is no longer seeded for real users — see _ensure_default_watchlists.
from backend.api.database import async_session
from backend.api.models import User, Watchlist, WatchlistItem, MoversSnapshot
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Cap per watchlist — Kite's `quote()` batch tops out around 500 keys
# but the UI gets unreadable past ~50. 100 leaves headroom.
_MAX_ITEMS_PER_LIST = 100


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class WatchlistItemInfo(msgspec.Struct):
    id: int
    watchlist_id: int
    tradingsymbol: str
    exchange: str
    sort_order: int
    added_at: str
    # Optional operator-supplied display name. Frontend shows alias when
    # present and falls back to tradingsymbol.
    alias: Optional[str] = None


class WatchlistInfo(msgspec.Struct):
    id: int
    name: str
    sort_order: int
    is_default: bool
    is_pinned: bool
    item_count: int
    created_at: str
    updated_at: str
    # is_global=true rows are shared across every user; only admin /
    # designated roles can mutate them. Frontend hides Rename / Delete
    # / item-write affordances on global rows for non-admin users.
    is_global: bool = False


class WatchlistFull(msgspec.Struct):
    """A single watchlist + every item it contains."""
    id: int
    name: str
    sort_order: int
    is_default: bool
    is_pinned: bool
    created_at: str
    updated_at: str
    items: list[WatchlistItemInfo]
    is_global: bool = False


class CreateWatchlistRequest(msgspec.Struct):
    name: str


class RenameWatchlistRequest(msgspec.Struct):
    name: Optional[str] = None
    sort_order: Optional[int] = None
    is_default: Optional[bool] = None
    is_pinned: Optional[bool] = None


class AddItemRequest(msgspec.Struct):
    tradingsymbol: str
    exchange: str
    sort_order: Optional[int] = None
    # Optional operator-supplied display name for the row. Empty/None
    # leaves alias unset; the grid then shows the raw tradingsymbol.
    alias: Optional[str] = None


class ReorderItemRequest(msgspec.Struct):
    # PATCH on an item — operator can set sort_order, alias, or both.
    # Either field may be omitted.
    sort_order: Optional[int] = None
    alias: Optional[str] = None


class WatchlistQuote(msgspec.Struct):
    """Live market quote for a single watchlist item."""
    item_id: int
    tradingsymbol: str       # the symbol the user stored (e.g. "GOLD")
    quote_symbol: str        # the symbol we resolved + fetched (e.g. "GOLDM25APRFUT")
    exchange: str
    ltp: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None   # prior-day close — used for day change
    change: float = 0.0              # ltp - close (signed ₹)
    change_pct: float = 0.0          # (ltp - close) / close × 100
    volume: int = 0
    stale: bool = False              # true when broker returned no quote


class WatchlistQuotes(msgspec.Struct):
    watchlist_id: int
    refreshed_at: str
    items: list[WatchlistQuote]


class MoverRow(msgspec.Struct):
    tradingsymbol: str
    exchange: str
    last_price: float
    previous_close: float
    change_pct: float
    peak_pct: float
    sticky: bool   # True iff abs(change_pct) currently < threshold but was >= threshold earlier today
    # Unified animation model (Jul 2026). See schemas.PositionRow for the
    # full triad rationale. Defaults preserve backward compatibility for
    # callers not yet consuming the unified fields.
    price_source: str = "live"
    current_price: float = 0.0
    is_animating: bool = True
    # Resolved broker/SSE key for MCX commodities. MCX movers use a bare
    # underlying root as tradingsymbol (e.g. "CRUDEOIL") while the Kite
    # ticker and symbolStore are keyed on the front-month contract
    # ("CRUDEOIL26JUNFUT"). Without this field the frontend _liveLtpSnap
    # lookup always misses for MCX mover rows and cells fall back to the
    # polled last_price (30 s cadence) instead of the sub-second SSE tick.
    # NSE rows leave this None — the bare tradingsymbol IS the quote key.
    quote_symbol: Optional[str] = None


class MoversResponse(msgspec.Struct):
    movers: list[MoverRow]
    threshold_pct: float
    session_date: str   # ISO date "2026-05-13" — when the sticky set last reset
    # ISO-8601 UTC string when this snapshot was captured.
    # Non-null only when serving a persisted off-hours snapshot so the
    # frontend can show "Last updated: <time>" rather than a live label.
    captured_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# In-process quote cache. Keyed by watchlist_id. The 5s TTL matches the
# paper engine tick + the simulator default rate, so we share one batch
# across multiple polls from the same UI session.
_QUOTE_CACHE: dict[int, tuple[float, WatchlistQuotes]] = {}
_QUOTE_TTL_SECONDS = 5.0


MOVER_THRESHOLD_PCT: float = 1.5
# How many top movers to surface per direction (winners + losers separately).
# Bumped from 6 → 20 so that after frontend _classifyMoverSym splits rows into
# three tabs (underlying / midcap / smallcap) each tab still has ~6-8 rows.
# Frontend caps display at _MOVER_TOP_N=10 per tab — no over-render risk.
# Payload cost: 40 rows × ~200 B ≈ 8 KB, negligible.
MOVER_TOP_N: int = 20

# ---------------------------------------------------------------------------
# Session-sticky movers state
# ---------------------------------------------------------------------------

_session_movers: dict[str, dict] = {}
_session_date: Optional[str] = None  # ISO date string — rolls over at IST midnight

# ---------------------------------------------------------------------------
# Per-day MCX underlyings cache (instruments scan, same buster as NSE cache)
# ---------------------------------------------------------------------------
# Under the unified movers path (Jul 2026) the MCX universe is merged with
# NSE into a single loop. These caches now hold the same content they did
# before (bare commodity roots + earliest-expiry FUT map) — only difference
# is they're consumed by the unified `get_movers` body instead of the
# deleted `_get_movers_mcx_live` branch.
_mcx_underlyings_cache: set[str] = set()  # bare commodity roots with CE/PE chain
_mcx_underlyings_cache_date: Optional[str] = None
_mcx_fut_map: dict[str, str] = {}         # root → earliest-expiry FUT symbol; cleared at midnight

# ---------------------------------------------------------------------------
# Demo synthetic watchlist
# ---------------------------------------------------------------------------
# Anonymous prod visitors (demo sessions) have no user_id and so can't be
# served real watchlists. We synthesise a single "Markets" list with the
# canonical seed (indices + commodities + USDINR) so demo viewers see the
# same pinned-underlyings + market-data block a logged-in partner would
# see on first sign-in. Read-only — every write endpoint stays jwt-only
# so demo can't mutate.
DEMO_WATCHLIST_ID = -1
# Renamed to "Pinned" to match the authenticated experience — the user
# saw "Markets ★" in the manage-watchlists dropdown whenever their JWT
# expired and the API silently fell back to this demo synthetic list.
DEMO_WATCHLIST_NAME = "Pinned"


def _demo_watchlist_items() -> list[WatchlistItemInfo]:
    """Synthetic items derived from the same seed real users get. Each
    gets a deterministic negative id so the frontend's item_id-keyed
    quote map works without collisions against real ids."""
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    out: list[WatchlistItemInfo] = []
    for i, (sym, exch) in enumerate(MARKETS_DEFAULT):
        out.append(WatchlistItemInfo(
            id=-1000 - i,  # negative so it never collides with real ids
            watchlist_id=DEMO_WATCHLIST_ID,
            tradingsymbol=sym, exchange=exch,
            sort_order=i, added_at="",
        ))
    return out


def _demo_watchlist_items_as_objs() -> list:
    """Synthesise transient WatchlistItem-shaped objects for the quote
    pipeline. _fetch_quotes reads .id / .tradingsymbol / .exchange so a
    SimpleNamespace works; nothing persists to the DB."""
    from types import SimpleNamespace
    return [
        SimpleNamespace(
            id=info.id, tradingsymbol=info.tradingsymbol,
            exchange=info.exchange, watchlist_id=info.watchlist_id,
            sort_order=info.sort_order,
        )
        for info in _demo_watchlist_items()
    ]

# Per-day cache of "underlyings that have a CE/PE chain in the instruments
# dump". The instruments cache is 24 h and the set only changes when Kite
# publishes new contracts (daily). Without this cache, every 30 s movers
# poll scans the entire 90k-row instruments list to rebuild the same set.
# Buster = today's IST date, so the set refreshes naturally at midnight
# alongside the session_movers rollover.
_underlyings_cache: set[str] = set()
_underlyings_cache_date: Optional[str] = None


# ---------------------------------------------------------------------------
# Movers snapshot persistence helpers
# ---------------------------------------------------------------------------

async def _save_movers_snapshot(rows: list["MoverRow"], date_iso: str) -> None:
    """Upsert today's movers snapshot to DB (fire-and-forget from the route).

    One row per IST calendar date — repeated saves during market hours
    overwrite with the latest data via ON CONFLICT DO UPDATE.
    """
    import json as _json
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from datetime import date as _date

    if not rows:
        return
    payload = _json.dumps([
        {
            "tradingsymbol": r.tradingsymbol,
            "exchange":       r.exchange,
            "last_price":     r.last_price,
            "previous_close": r.previous_close,
            "change_pct":     r.change_pct,
            "peak_pct":       r.peak_pct,
            "sticky":         r.sticky,
        }
        for r in rows
    ])
    now_utc = datetime.now(timezone.utc)
    try:
        async with async_session() as s:
            stmt = (
                pg_insert(MoversSnapshot)
                .values(date=_date.fromisoformat(date_iso), payload_json=payload, captured_at=now_utc)
                .on_conflict_do_update(
                    constraint="uq_movers_snapshots_date",
                    set_={"payload_json": payload, "captured_at": now_utc},
                )
            )
            await s.execute(stmt)
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Movers snapshot save failed: {exc}")


async def _load_latest_movers_snapshot() -> Optional[MoversSnapshot]:
    """Return the most recent row from movers_snapshots, or None."""
    from sqlalchemy import desc
    try:
        async with async_session() as s:
            result = await s.execute(
                select(MoversSnapshot).order_by(desc(MoversSnapshot.captured_at)).limit(1)
            )
            return result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Movers snapshot load failed: {exc}")
        return None


def _combine_movers(
    live_snapshot: dict[str, dict],
    session_movers: dict[str, dict],
    top_n: int,
) -> dict[str, dict]:
    """Build the combined movers dict from ``live_snapshot`` + ``session_movers``.

    Takes ``top_n`` losers (most negative ``last_pct``) and ``top_n`` winners
    (most positive ``last_pct``) from ``live_snapshot`` INDEPENDENTLY so that
    a strongly bullish or bearish day cannot crowd out the minority direction.

    Directional-fairness invariant: given M winners and L losers in
    ``live_snapshot``, the result always contains min(L, top_n) losers AND
    min(M, top_n) winners regardless of their relative magnitudes.

    Session-sticky entries (``session_movers``) overlay at higher priority —
    they represent underlyings that crossed the threshold threshold earlier
    today and must stay visible even if they have since reverted.

    ``_force_movers_snapshot`` (the NSE-close capture path) writes the full
    untruncated universe to the DB independently of this function — the same
    directional-fairness invariant applies there implicitly because no slice
    is ever applied.
    """
    items_sorted = sorted(live_snapshot.items(), key=lambda kv: kv[1]["last_pct"])
    top_losers = [(u, e) for u, e in items_sorted if e["last_pct"] < 0][:top_n]
    top_winners = sorted(
        [(u, e) for u, e in items_sorted if e["last_pct"] > 0],
        key=lambda kv: kv[1]["last_pct"],
        reverse=True,
    )[:top_n]

    combined: dict[str, dict] = {}
    # Directional slices first (lower priority).
    for u, entry in top_losers + top_winners:
        combined[u] = entry
    # Session-sticky overlay (higher priority — overrides snapshot entries).
    for u, entry in session_movers.items():
        combined[u] = entry
    return combined


def _force_movers_build_key_map(all_items: list) -> dict[str, str]:
    """Build the NSE-only ``{broker_key: underlying}`` map for the
    force-snapshot quote batch. Skips MCX underlyings (commodity roots
    have no spot index key on NSE)."""
    from backend.api.algo.derivatives import underlying_ltp_key, is_mcx_underlying
    key_to_underlying: dict[str, str] = {}
    for inst in all_items:
        if inst.t in ("CE", "PE") and inst.u:
            name = inst.u.upper()
            if not is_mcx_underlying(name):
                key_to_underlying[underlying_ltp_key(name)] = name
    return key_to_underlying


def _force_movers_build_rows(
    key_to_underlying: dict[str, str],
    quote_data: dict,
) -> list["MoverRow"]:
    """Convert a quote batch into sorted ``MoverRow`` list for NSE close
    snapshot. Skips any symbol where LTP or prev_close is 0."""
    rows: list[MoverRow] = []
    for kite_key, underlying in key_to_underlying.items():
        q = quote_data.get(kite_key) or {}
        ltp = float(q.get("last_price") or 0.0)
        ohlc = q.get("ohlc") or {}
        prev_close = float(ohlc.get("close") or 0.0)
        if ltp == 0.0 or prev_close == 0.0:
            continue
        change_pct = (ltp - prev_close) / prev_close * 100.0
        rows.append(MoverRow(
            tradingsymbol=underlying,
            exchange="NSE",
            last_price=ltp,
            previous_close=prev_close,
            change_pct=change_pct,
            peak_pct=change_pct,
            sticky=False,
        ))
    rows.sort(key=lambda r: abs(r.change_pct), reverse=True)
    return rows


async def _force_movers_snapshot() -> int:
    """Fetch a fresh movers quote batch and persist it to ``movers_snapshots``.

    Called by the ``nse:close`` market lifecycle handler so a guaranteed DB
    row lands at session close regardless of the in-memory ``_session_movers``
    state or polling luck.  Uses the same quote + universe logic as the live
    route path but runs independently of ``_session_movers``.

    Returns the number of rows written (0 if no data or on error).
    """
    import asyncio as _asyncio
    from datetime import date as _date, timezone as _tz

    try:
        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS as _INST_TTL
        from backend.brokers.registry import get_market_data_broker
        from backend.shared.helpers.date_time_utils import timestamp_indian

        ist_now = timestamp_indian()
        ist_today = ist_now.date().isoformat()

        # Build NSE-only universe from instruments cache.
        try:
            resp = await get_or_fetch("instruments", _fetch_instruments, ttl_seconds=_INST_TTL)
            all_items = resp.items if resp else []
        except Exception:
            all_items = []

        key_to_underlying = _force_movers_build_key_map(all_items)
        if not key_to_underlying:
            logger.warning("_force_movers_snapshot: empty universe — instruments cache cold?")
            return 0

        # Fresh quote batch (bypass per-route 30s cache at close time).
        keys = list(key_to_underlying.keys())
        try:
            broker = get_market_data_broker()
            quote_data: dict = await _asyncio.to_thread(broker.quote, keys) or {}
        except Exception as exc:
            logger.warning(f"_force_movers_snapshot: quote fetch failed: {exc}")
            return 0

        rows = _force_movers_build_rows(key_to_underlying, quote_data)
        if not rows:
            logger.warning("_force_movers_snapshot: zero rows from live quotes at close")
            return 0

        await _save_movers_snapshot(rows, ist_today)
        return len(rows)

    except Exception as exc:  # noqa: BLE001
        logger.warning(f"_force_movers_snapshot: unexpected error: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Unified movers resolver (hardening: single-source truth + reason tagging)
# ---------------------------------------------------------------------------

async def _get_movers_now_off_hours() -> tuple[list["MoverRow"], str]:
    """Off-hours branch of the unified resolver.

    Returns the persisted DB snapshot when NSE is closed. The tuple's second
    element names which fallback layer served the data so the caller (and
    `[MOVERS-EMPTY]` structured log) can attribute the empty case:

      ("snapshot" , rows) — DB row deserialised cleanly, N>=0 rows returned
      ("snapshot_missing_off_hours", []) — no DB row yet (fresh deploy)
      ("snapshot_deserialise_failed", []) — DB row present but malformed

    The route handler `get_movers` retains its inline paths for now — this
    helper is the single canonical consolidation that both the /movers route
    and any future caller (MCP tool, /admin/history drilldown) share so we
    can't have two paths drift apart on reason semantics.
    """
    import json as _json
    snap = await _load_latest_movers_snapshot()
    if not snap:
        return [], "snapshot_missing_off_hours"
    try:
        snap_rows: list["MoverRow"] = [
            MoverRow(
                tradingsymbol=d["tradingsymbol"],
                exchange=d["exchange"],
                last_price=float(d["last_price"]),
                previous_close=float(d["previous_close"]),
                change_pct=float(d["change_pct"]),
                peak_pct=float(d["peak_pct"]),
                sticky=bool(d.get("sticky", False)),
            )
            for d in _json.loads(snap.payload_json)
        ]
        return snap_rows, "snapshot"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[MOVERS-EMPTY] reason=snapshot_deserialise_failed "
            f"universe_size=0 snapshot_present=True err={exc}"
        )
        return [], "snapshot_deserialise_failed"


def _mcx_pick_nearest_fut(
    fut_candidates: dict[str, list[tuple[str, str]]],
    opt_roots: set[str],
) -> dict[str, str]:
    """From a pre-built ``{root: [(expiry, sym), ...]}`` dict, return the
    mapping ``{root: nearest_expiry_sym}`` restricted to roots that have an
    active option chain (i.e. are in ``opt_roots``)."""
    underlying_to_fut: dict[str, str] = {}
    for root, candidates in fut_candidates.items():
        if root in opt_roots:
            candidates.sort(key=lambda t: t[0] or "")
            underlying_to_fut[root] = candidates[0][1]
    return underlying_to_fut


def _mcx_classify_inst(
    inst,
    is_mcx_underlying,
    opt_roots: set,
    fut_candidates: dict,
) -> None:
    """Classify a single instrument row into ``opt_roots`` or ``fut_candidates``.
    Mutates both dicts in-place; skips non-MCX and unknown-underlying rows."""
    if getattr(inst, "e", None) != "MCX":
        return
    underlying = (getattr(inst, "u", None) or "").upper()
    if not underlying or not is_mcx_underlying(underlying):
        return
    inst_type = getattr(inst, "t", None)
    if inst_type in ("CE", "PE"):
        opt_roots.add(underlying)
    elif inst_type == "FUT":
        sym = getattr(inst, "s", None) or ""
        if sym:
            expiry = getattr(inst, "x", None) or ""
            fut_candidates.setdefault(underlying, []).append((expiry, sym))


def _mcx_scan_instruments(
    all_items: list,
) -> tuple[set[str], dict[str, list[tuple[str, str]]]]:
    """Single-pass collector: build ``(opt_roots, fut_candidates)`` for all
    MCX instruments. ``opt_roots`` = roots with a CE/PE chain. ``fut_candidates``
    = ``{root: [(expiry, sym), ...]}``. Delegates per-instrument branching to
    ``_mcx_classify_inst``."""
    from backend.api.algo.derivatives import is_mcx_underlying
    opt_roots: set[str] = set()
    fut_candidates: dict[str, list[tuple[str, str]]] = {}
    for inst in all_items:
        _mcx_classify_inst(inst, is_mcx_underlying, opt_roots, fut_candidates)
    return opt_roots, fut_candidates


def _build_mcx_universe(
    all_items: list,
) -> tuple[set[str], dict[str, str]]:
    """Single-pass over instruments list to build MCX movers universe.

    Returns:
        (mcx_underlyings_with_opts, underlying_to_fut_symbol)

    ``mcx_underlyings_with_opts`` — bare commodity roots (e.g. 'GOLD',
    'CRUDEOIL') that have at least one MCX CE/PE row in the instruments
    dump, i.e. they have an active option chain.

    ``underlying_to_fut_symbol`` — earliest-expiry MCX FUT tradingsymbol per
    underlying root. This is what Kite wants in the quote key, e.g.
    'MCX:GOLD26JUNFUT'.  When no FUT row exists for a root (cache cold) the
    root is absent from this dict and skipped at quote-key construction time.
    """
    opt_roots, fut_candidates = _mcx_scan_instruments(all_items)
    underlying_to_fut = _mcx_pick_nearest_fut(fut_candidates, opt_roots)
    return opt_roots, underlying_to_fut


async def _resolve_mcx_commodity(commodity_name: str) -> Optional[str]:
    """Map a bare MCX commodity name (e.g. 'GOLD') to the nearest-month
    future tradingsymbol.

    Delegates to the canonical resolver in ``symbol_resolver.py``.
    Falls back to the most-recently listed (highest expiry) contract on
    instruments-cache lag (so the caller gets a non-None symbol rather than
    a silent row drop).
    """
    from backend.api.algo.symbol_resolver import (
        list_active_futures, _list_all_futures_fallback,
    )
    futures = await list_active_futures(commodity_name, "MCX", limit=1)
    if futures:
        return futures[0]
    # Cache lag: fetch all listed contracts (including expiring-today) and
    # return the LAST one (highest expiry) — same semantic as original logic.
    fallback = await _list_all_futures_fallback(commodity_name, "MCX", limit=100)
    return fallback[-1] if fallback else None


async def _resolve_cds_currency(currency_name: str) -> Optional[str]:
    """Map a bare CDS currency pair name (e.g. 'USDINR') to the nearest-month
    future tradingsymbol.

    Delegates to the canonical resolver in ``symbol_resolver.py``.
    Falls back to the most-recently listed contract on instruments-cache lag.
    """
    from backend.api.algo.symbol_resolver import (
        list_active_futures, _list_all_futures_fallback,
    )
    futures = await list_active_futures(currency_name, "CDS", limit=1)
    if futures:
        return futures[0]
    fallback = await _list_all_futures_fallback(currency_name, "CDS", limit=100)
    return fallback[-1] if fallback else None


async def _resolve_one_exchange_root(
    sym_upper: str,
    root: str,
    is_next: bool,
    exch: str,
) -> Optional[tuple[str, str]]:
    """Resolve a single virtual root for one exchange (MCX or CDS).
    Returns ``(broker_key, quote_sym)`` or ``None``."""
    from backend.api.algo.symbol_resolver import resolve_symbol
    if is_next:
        resolved = await resolve_symbol(sym_upper, exch)
        if resolved and resolved != sym_upper:
            return f"{exch}:{resolved}", resolved
    else:
        resolver = _resolve_mcx_commodity if exch == "MCX" else _resolve_cds_currency
        resolved = await resolver(root)
        if resolved:
            return f"{exch}:{resolved}", resolved
    return None


async def _resolve_virtual_root_key(
    sym_upper: str,
    root: str,
    is_next: bool,
    exch: str,
) -> Optional[tuple[str, str]]:
    """Try to resolve a bare MCX or CDS virtual root (or its _NEXT variant)
    to a concrete contract broker key. Returns ``(broker_key, quote_sym)``
    on success or ``None`` when no resolution is available."""
    if exch == "MCX":
        return await _resolve_one_exchange_root(sym_upper, root, is_next, "MCX")
    if exch == "CDS":
        return await _resolve_one_exchange_root(sym_upper, root, is_next, "CDS")
    return None


async def _build_quote_key(item: WatchlistItem) -> tuple[str, str]:
    """Returns (broker_key, quote_symbol). MCX bare commodity names get
    resolved to the near-month future; everything else passes through.

    Also handles back-month virtual roots (GOLDM_NEXT, CRUDEOIL_NEXT) which
    contain an underscore and fail the isalpha() guard.  The _NEXT suffix is
    stripped for the alpha check; the full symbol (with suffix) is passed to
    resolve_symbol which maps it to the back-month contract."""
    from backend.api.algo.symbol_resolver import _strip_next
    sym, exch = item.tradingsymbol, item.exchange
    sym_upper = sym.upper()

    # Strip _NEXT suffix to get the bare root for the alpha check.
    root, is_next = _strip_next(sym_upper)
    is_bare_root = root.isalpha() and len(root) <= 12

    if is_bare_root and exch in ("MCX", "CDS"):
        result = await _resolve_virtual_root_key(sym_upper, root, is_next, exch)
        if result is not None:
            return result
    return f"{exch}:{sym}", sym


def _eod_rows_to_map(
    rows: list,
    want: set,
) -> dict[tuple[str, str], dict]:
    """Convert raw SQL rows from ohlcv_daily into a ``{(sym, exch): {...}}``
    dict, filtering to only the ``(sym, exch)`` pairs in ``want``."""
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (str(r[0]), str(r[1]))
        if key not in want:
            continue
        out[key] = {
            "open":   float(r[2]) if r[2] is not None else None,
            "high":   float(r[3]) if r[3] is not None else None,
            "low":    float(r[4]) if r[4] is not None else None,
            "close":  float(r[5]) if r[5] is not None else None,
            "volume": int(r[6])   if r[6] is not None else 0,
        }
    return out


async def _eod_fallback_map(
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict]:
    """Read most-recent OHLCV bar per (symbol, exchange) from the
    ohlcv_daily store/table. Returns {(sym, exch): {ltp, close, open,
    high, low, volume}} where ltp = the last available close (a
    sensible cold-mount placeholder until a live quote lands).

    Used by `_fetch_quotes` to fill in zero/stale broker responses so
    the Pulse Pinned + Watch grids paint with yesterday's EOD prices
    on cold mount instead of an empty 0. Operator goal: "every
    relevant truth should go through the cycle".
    """
    if not pairs:
        return {}
    # Use two flat-array params instead of a composite-row ANY (which
    # asyncpg can't bind). We fetch by symbol IN-list then filter by
    # exchange in Python — over-fetches a bit when symbols span
    # exchanges but keeps the SQL portable + the asyncpg codec happy.
    # Slice AQ caught the prior version silently returning {} for every
    # call because ANY((symbol, exchange) = ANY(:pairs)) raised on
    # parameter bind.
    from sqlalchemy import text as _sql_text
    sql = _sql_text("""
        SELECT DISTINCT ON (symbol, exchange)
               symbol, exchange, open, high, low, close, volume
        FROM ohlcv_daily
        WHERE symbol = ANY(:syms)
        ORDER BY symbol, exchange, date DESC
    """)
    syms = sorted({p[0] for p in pairs})
    want = set(pairs)
    try:
        async with async_session() as session:
            result = await session.execute(sql, {"syms": syms})
            rows = result.fetchall()
    except Exception as exc:
        logger.warning(f"watchlist EOD fallback query failed: {exc}")
        return {}
    return _eod_rows_to_map(rows, want)


async def _build_watchlist_key_map(
    items: list[WatchlistItem],
) -> dict[int, tuple[str, str]]:
    """Resolve every item to a `(broker_key, quote_symbol)` pair keyed by
    watchlist_item.id. Extracted from `_fetch_quotes` for clarity."""
    key_map: dict[int, tuple[str, str]] = {}
    for it in items:
        broker_key, quote_sym = await _build_quote_key(it)
        key_map[it.id] = (broker_key, quote_sym)
    return key_map


async def _batch_broker_quotes(distinct_keys: list[str]) -> dict:
    """One `broker.quote()` call for every distinct key. Returns `{}` on
    broker failure so the caller can fall back to EOD data."""
    import asyncio
    from backend.brokers.registry import get_market_data_broker

    if not distinct_keys:
        return {}
    try:
        broker = get_market_data_broker()
        return await asyncio.to_thread(broker.quote, distinct_keys) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Watchlist quote fetch failed: {exc}")
        return {}


def _collect_watchlist_eod_pairs(
    items: list[WatchlistItem],
    key_map: dict[int, tuple[str, str]],
    quote_data: dict,
) -> list[tuple[str, str]]:
    """Return `(sym, exchange)` pairs that the broker missed so the caller
    can look up prior-close data from `ohlcv_daily`."""
    eod_pairs: list[tuple[str, str]] = []
    for it in items:
        broker_key, quote_sym = key_map[it.id]
        q = quote_data.get(broker_key) or {}
        if not q or not float(q.get("last_price") or 0.0):
            eod_pairs.append((quote_sym.upper().strip(), it.exchange.upper().strip()))
    return eod_pairs


def _extract_depth_bid_ask(
    depth: dict,
) -> tuple[Optional[float], Optional[float]]:
    """Pull best bid / best ask from a Kite-style depth block. Returns
    `(None, None)` when the book is empty or the top-of-book price is 0."""
    buys  = depth.get("buy") or []
    sells = depth.get("sell") or []
    bid   = float(buys[0]["price"])  if buys  and (buys[0].get("price") or 0)  else None
    ask   = float(sells[0]["price"]) if sells and (sells[0].get("price") or 0) else None
    return bid, ask


def _apply_watchlist_eod_substitution(
    ltp: float,
    close: Optional[float],
    ohlc: dict,
    eod: Optional[dict],
) -> tuple[float, Optional[float]]:
    """Fill missing LTP / close / OHLC from ohlcv_daily's most recent bar.
    The stale flag stays TRUE upstream — the frontend marks the row
    accordingly. Returns the potentially-updated `(ltp, close)`."""
    if not eod:
        return ltp, close
    if not ltp:   ltp   = float(eod.get("close") or 0.0)
    if not close: close = eod.get("close")
    if not ohlc.get("open"):  ohlc.setdefault("open",  eod.get("open"))
    if not ohlc.get("high"):  ohlc.setdefault("high",  eod.get("high"))
    if not ohlc.get("low"):   ohlc.setdefault("low",   eod.get("low"))
    return ltp, close


def _watchlist_quote_ohlc(
    ohlc: dict,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Unpack open/high/low from an OHLC dict, returning ``None`` for missing
    or zero values so the frontend knows to leave the cell blank."""
    open_ = float(ohlc["open"]) if ohlc.get("open") else None
    high  = float(ohlc["high"]) if ohlc.get("high") else None
    low   = float(ohlc["low"])  if ohlc.get("low")  else None
    return open_, high, low


def _resolve_watchlist_quote_prices(
    quote_sym: str,
    it_exchange: str,
    q: dict,
    eod_map: dict,
) -> tuple[float, Optional[float], dict, Optional[float], Optional[float], bool]:
    """Parse broker quote and apply EOD fallback when the broker returned
    nothing. Returns ``(ltp, close, ohlc, bid, ask, is_stale)``."""
    ltp   = float(q.get("last_price") or 0.0)
    ohlc  = q.get("ohlc") or {}
    close = float(ohlc.get("close") or 0.0) or None
    bid, ask = _extract_depth_bid_ask(q.get("depth") or {})
    is_stale = not q or not ltp
    eod = eod_map.get((quote_sym.upper().strip(), it_exchange.upper().strip())) if is_stale else None
    ltp, close = _apply_watchlist_eod_substitution(ltp, close, ohlc, eod)
    return ltp, close, ohlc, bid, ask, is_stale


def _build_watchlist_quote_row(
    it: WatchlistItem,
    broker_key: str,
    quote_sym: str,
    quote_data: dict,
    eod_map: dict,
) -> WatchlistQuote:
    """Per-item WatchlistQuote build. Mirrors the row-shape used by the
    frontend grid; EOD data substitutes for a cold-mount / broker miss."""
    q = quote_data.get(broker_key) or {}
    ltp, close, ohlc, bid, ask, is_stale = _resolve_watchlist_quote_prices(
        quote_sym, it.exchange, q, eod_map,
    )
    change = (ltp - close) if (close and ltp) else 0.0
    chg_pct = (change / close * 100.0) if close else 0.0
    open_, high, low = _watchlist_quote_ohlc(ohlc)
    return WatchlistQuote(
        item_id=it.id,
        tradingsymbol=it.tradingsymbol,
        quote_symbol=quote_sym,
        exchange=it.exchange,
        ltp=ltp,
        bid=bid, ask=ask,
        open=open_, high=high, low=low,
        close=close,
        change=change, change_pct=chg_pct,
        volume=int(q.get("volume") or 0),
        stale=is_stale,
    )


async def _fetch_quotes(items: list[WatchlistItem]) -> list[WatchlistQuote]:
    """One batched broker.quote() call for every distinct key. asyncio
    runs the sync broker call in a thread so the event loop isn't
    blocked on the network round-trip."""
    key_map = await _build_watchlist_key_map(items)

    distinct_keys = sorted({k[0] for k in key_map.values() if k[0]})
    quote_data = await _batch_broker_quotes(distinct_keys)

    # EOD fallback — when broker returned 0/stale for any item, look up
    # the most recent close from ohlcv_daily so cold-mount paints with
    # yesterday's EOD instead of 0. Built only for items the broker
    # actually missed (skips the DB query when every quote was live).
    eod_pairs = _collect_watchlist_eod_pairs(items, key_map, quote_data)
    eod_map = await _eod_fallback_map(eod_pairs) if eod_pairs else {}

    out: list[WatchlistQuote] = []
    for it in items:
        broker_key, quote_sym = key_map[it.id]
        out.append(_build_watchlist_quote_row(
            it, broker_key, quote_sym, quote_data, eod_map,
        ))
    return out

def _actor_sub(request) -> str:
    payload = getattr(request.state, "token_payload", {}) or {}
    return payload.get("sub", "")


async def _resolve_user_id(session, username: str) -> int:
    row = await session.execute(select(User.id).where(User.username == username))
    uid = row.scalar_one_or_none()
    if uid is None:
        raise HTTPException(status_code=404, detail="User not found")
    return int(uid)


async def _sgp_migration_done(session, key: str) -> bool:
    """Return True when the one-shot migration *key* has already been applied."""
    from backend.api.models import Setting
    row = (await session.execute(
        select(Setting).where(Setting.key == key)
    )).scalar_one_or_none()
    return row is not None and row.value == "1"


async def _sgp_mark_migration(session, key: str) -> None:
    """Persist a one-shot migration marker so the cleanup wave never re-fires."""
    from backend.api.models import Setting
    now_ts = datetime.now(timezone.utc)
    existing = (await session.execute(
        select(Setting).where(Setting.key == key)
    )).scalar_one_or_none()
    if existing is None:
        session.add(Setting(
            category="migrations",
            key=key,
            value_type="string",
            value="1",
            default_value="0",
            description=f"One-shot pinned cleanup: {key}",
            updated_at=now_ts,
        ))
    else:
        existing.value = "1"
        existing.updated_at = now_ts


async def _sgp_find_or_create_global(session, now: datetime) -> "Watchlist":
    """Return the single global Pinned Watchlist row, creating it if absent."""
    global_row = (await session.execute(
        select(Watchlist).where(Watchlist.is_global == True).limit(1)
    )).scalar_one_or_none()
    if global_row is None:
        global_row = Watchlist(
            user_id=None, name="Pinned", sort_order=0,
            is_default=True, is_pinned=True, is_global=True,
            created_at=now, updated_at=now,
        )
        session.add(global_row)
        await session.flush()
        logger.info("Watchlist: seeded global Pinned (id=%s)", global_row.id)
    return global_row


async def _sgp_migrate_legacy(session, global_row, now: datetime) -> None:
    """Absorb any per-user Pinned/Default rows into the global row then delete them."""
    from sqlalchemy import delete as sa_delete
    legacy_rows = (await session.execute(
        select(Watchlist)
        .where(
            Watchlist.is_global == False,
            Watchlist.is_pinned == True,
            Watchlist.name.in_(["Pinned", "Default"]),
        )
    )).scalars().all()
    legacy_ids = [r.id for r in legacy_rows]
    if not legacy_ids:
        return

    existing_pairs = {
        (r.tradingsymbol.upper(), r.exchange.upper())
        for r in (await session.execute(
            select(WatchlistItem).where(WatchlistItem.watchlist_id == global_row.id)
        )).scalars().all()
    }
    sort_max = (await session.execute(
        select(func.coalesce(func.max(WatchlistItem.sort_order), -1))
        .where(WatchlistItem.watchlist_id == global_row.id)
    )).scalar() or -1
    sort_next = int(sort_max) + 1

    legacy_items = (await session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id.in_(legacy_ids))
    )).scalars().all()
    for it in legacy_items:
        key = (it.tradingsymbol.upper(), it.exchange.upper())
        if key in existing_pairs:
            continue
        existing_pairs.add(key)
        session.add(WatchlistItem(
            watchlist_id=global_row.id,
            tradingsymbol=it.tradingsymbol,
            exchange=it.exchange,
            alias=getattr(it, "alias", None),
            sort_order=sort_next,
            added_at=it.added_at or now,
        ))
        sort_next += 1

    await session.execute(sa_delete(Watchlist).where(Watchlist.id.in_(legacy_ids)))
    logger.info("Watchlist: migrated %d legacy Pinned rows into global", len(legacy_ids))


async def _sgp_run_delete_wave(
    session, global_row, key: str, pairs: list[tuple[str, str]], label: str
) -> None:
    """One-shot cleanup wave: delete pinned items for the given (symbol, exchange)
    pairs if the migration marker *key* has not yet been applied.
    """
    from sqlalchemy import delete as sa_delete
    if await _sgp_migration_done(session, key):
        return
    for _sym, _exch in pairs:
        await session.execute(
            sa_delete(WatchlistItem).where(
                WatchlistItem.watchlist_id == global_row.id,
                WatchlistItem.tradingsymbol == _sym,
                WatchlistItem.exchange == _exch,
            )
        )
    await _sgp_mark_migration(session, key)
    logger.info("Watchlist: one-shot cleanup %s done", label)


async def _sgp_wave4_usdinr_bare_root(session, global_row) -> None:
    """Wave 4: drop USDINR contract-name rows so top-up seeds the bare root."""
    from sqlalchemy import delete as sa_delete
    import re as _re
    _W4_KEY = "migrations.pinned_usdinr_bare_root_v1"
    if await _sgp_migration_done(session, _W4_KEY):
        return
    # Regex applied Python-side — SQLite (test dialect) lacks native REGEXP;
    # the CDS row-set is tiny so load-then-DELETE is cheap.
    _CONTRACT_PAT = _re.compile(
        r'^USDINR\d{2}(?:[A-Z]{3}|\d{3,4})FUT$', _re.IGNORECASE
    )
    all_cds = (await session.execute(
        select(WatchlistItem.id, WatchlistItem.tradingsymbol).where(
            WatchlistItem.watchlist_id == global_row.id,
            WatchlistItem.exchange == "CDS",
        )
    )).all()
    victim_ids = [_id for (_id, _sym) in all_cds if _CONTRACT_PAT.match(_sym or "")]
    if victim_ids:
        await session.execute(
            sa_delete(WatchlistItem).where(WatchlistItem.id.in_(victim_ids))
        )
    await _sgp_mark_migration(session, _W4_KEY)
    logger.info("Watchlist: one-shot cleanup wave 4 (USDINR bare root) done")


def _sgp_w5_push_extras(
    all_items: list,
    canonical_sort: dict,
    extra_base: int,
) -> None:
    """Step A of wave 5: assign sort_orders above ``extra_base`` to any
    item that is NOT in the canonical list and whose current sort_order is
    below the canonical ceiling. Mutates items in-place."""
    extra_seq = extra_base
    for it in sorted(all_items, key=lambda r: r.sort_order):
        key = (it.tradingsymbol.upper(), it.exchange.upper())
        if key not in canonical_sort and it.sort_order < extra_base:
            it.sort_order = extra_seq
            extra_seq += 10


async def _sgp_wave5_next_adjacency(session, global_row, now: datetime) -> None:
    """Wave 5: enforce canonical sort_order for MARKETS_DEFAULT rows and
    insert missing _NEXT back-month variants adjacent to their roots.
    """
    _W5_KEY = "migrations.pinned_seed_next_variants_v1"
    if await _sgp_migration_done(session, _W5_KEY):
        return
    from backend.api.algo.watchlist_defaults import (
        MARKETS_DEFAULT as _MD,
        markets_default_rows as _mdr,
    )
    canonical_rows = _mdr()
    canonical_sort: dict[tuple[str, str], int] = {
        (r["tradingsymbol"].upper(), r["exchange"].upper()): r["sort_order"]
        for r in canonical_rows
    }
    canonical_max = len(_MD) * 10  # e.g. 23*10 = 230

    all_items = (await session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == global_row.id)
    )).scalars().all()
    existing_keys = {(it.tradingsymbol.upper(), it.exchange.upper()) for it in all_items}

    # Step A: push operator extras above the canonical range.
    extra_base = canonical_max + 100
    _sgp_w5_push_extras(all_items, canonical_sort, extra_base)

    # Step B: stamp canonical sort_orders on existing canonical rows.
    for it in all_items:
        key = (it.tradingsymbol.upper(), it.exchange.upper())
        if key in canonical_sort:
            it.sort_order = canonical_sort[key]

    # Step C: insert missing canonical rows (including all _NEXT variants).
    inserted = 0
    for cr in canonical_rows:
        key = (cr["tradingsymbol"].upper(), cr["exchange"].upper())
        if key in existing_keys:
            continue
        existing_keys.add(key)
        session.add(WatchlistItem(
            watchlist_id=global_row.id,
            tradingsymbol=cr["tradingsymbol"],
            exchange=cr["exchange"],
            sort_order=cr["sort_order"],
            added_at=now,
        ))
        inserted += 1

    await _sgp_mark_migration(session, _W5_KEY)
    logger.info(
        "Watchlist: wave 5 (_NEXT adjacency) done — inserted %d new rows", inserted,
    )


async def _sgp_topup_defaults(session, global_row, now: datetime) -> None:
    """Top up global Pinned with any MARKETS_DEFAULT item not already present.
    Additive only — never removes operator-curated extras.
    """
    from backend.api.algo.watchlist_defaults import markets_default_rows
    canonical: dict[tuple[str, str], int] = {
        (r["tradingsymbol"].upper(), r["exchange"].upper()): r["sort_order"]
        for r in markets_default_rows()
    }
    current_pairs = {
        (r.tradingsymbol.upper(), r.exchange.upper())
        for r in (await session.execute(
            select(WatchlistItem).where(WatchlistItem.watchlist_id == global_row.id)
        )).scalars().all()
    }
    max_sort = (await session.execute(
        select(func.coalesce(func.max(WatchlistItem.sort_order), -1))
        .where(WatchlistItem.watchlist_id == global_row.id)
    )).scalar() or -1
    next_sort = int(max_sort) + 10
    added = 0
    for row in markets_default_rows():
        key = (row["tradingsymbol"].upper(), row["exchange"].upper())
        if key in current_pairs:
            continue
        current_pairs.add(key)
        sort_val = canonical.get(key, next_sort)
        session.add(WatchlistItem(
            watchlist_id=global_row.id,
            tradingsymbol=row["tradingsymbol"],
            exchange=row["exchange"],
            sort_order=sort_val,
            added_at=now,
        ))
        if sort_val == next_sort:
            next_sort += 10
        added += 1
    if added:
        logger.info("Watchlist: topped up global Pinned with %d default symbols", added)


async def seed_global_pinned() -> None:
    """Idempotent: ensure exactly one global 'Pinned' watchlist exists +
    consolidate any legacy per-user Pinned/Default rows into it.

    Called from init_db on every startup. Operator-created (non-pinned)
    rows are untouched. Global Pinned rows have user_id=NULL.
    """
    async with async_session() as session:
        now = datetime.now(timezone.utc)

        # 1+2. Find-or-create global row, then absorb legacy per-user rows.
        global_row = await _sgp_find_or_create_global(session, now)
        await _sgp_migrate_legacy(session, global_row, now)

        # 5a. One-shot cleanup waves — each fires ONCE then is gated by a
        #     settings marker so operator re-adds survive subsequent restarts.
        await _sgp_run_delete_wave(
            session, global_row,
            "migrations.pinned_remove_goldm_usdinr_v1",
            [("GOLDM", "MCX"), ("USDINR", "CDS")],
            "wave 1 (GOLDM/USDINR)",
        )
        await _sgp_run_delete_wave(
            session, global_row,
            "migrations.pinned_remove_mcx_futures_v1",
            [("COPPER", "MCX"), ("CRUDEOIL", "MCX"),
             ("NATURALGAS", "MCX"), ("SILVERM", "MCX")],
            "wave 2 (MCX futures)",
        )
        await _sgp_run_delete_wave(
            session, global_row,
            "migrations.pinned_remove_silver_mcx_v1",
            [("SILVER", "MCX")],
            "wave 3 (SILVER MCX)",
        )
        await _sgp_wave4_usdinr_bare_root(session, global_row)
        await _sgp_wave5_next_adjacency(session, global_row, now)

        # 6. Additive top-up with MARKETS_DEFAULT entries.
        await _sgp_topup_defaults(session, global_row, now)

        await session.commit()


async def _ensure_default_watchlists(session, user_id: int) -> None:
    """No-op now that Pinned is a single shared global row (see
    seed_global_pinned in init_db). Operator-created lists are created
    on demand via POST /api/watchlist/. Kept as a hook so the legacy
    callsites still compile."""
    return


def _wl_info(wl: Watchlist, item_count: int) -> WatchlistInfo:
    return WatchlistInfo(
        id=wl.id, name=wl.name, sort_order=wl.sort_order,
        is_default=wl.is_default, is_pinned=wl.is_pinned,
        is_global=getattr(wl, "is_global", False),
        item_count=item_count,
        created_at=wl.created_at.isoformat() if wl.created_at else "",
        updated_at=wl.updated_at.isoformat() if wl.updated_at else "",
    )


def _item_info(it: WatchlistItem) -> WatchlistItemInfo:
    return WatchlistItemInfo(
        id=it.id, watchlist_id=it.watchlist_id,
        tradingsymbol=it.tradingsymbol, exchange=it.exchange,
        alias=getattr(it, "alias", None),
        sort_order=it.sort_order,
        added_at=it.added_at.isoformat() if it.added_at else "",
    )


async def _expand_root_items_to_futures(items) -> list[WatchlistItemInfo]:
    """For every stored item whose tradingsymbol is a bare MCX commodity
    root (no FUT suffix), replace it with actual contract item(s) resolved
    via the instruments cache.

    MCX commodity roots (GOLD, CRUDEOIL, etc.): expand to front + next
    month (up to 2 rows). Rollover is automatic — the stored root never
    needs to change.

    CDS currency roots (USDINR, etc.): pass through unchanged as bare
    roots. The quote pipeline (_build_quote_key) and SSE subscription
    path each resolve the bare root to the active near-month contract at
    call time — no frontend alias needed. Convention matches MCX bare
    roots: the grid shows "USDINR", not the dated contract name.

    Items that already carry a tradeable contract name (FUT suffix, NSE
    equity, ETF, index) pass through unchanged.

    The expanded items use the parent row's id with a suffix appended as
    a sort key — frontend treats each as a normal item; delete operations
    against the parent id still target the stored bare-root row."""
    from backend.api.algo.derivatives import lookup_mcx_futures_list
    out: list[WatchlistItemInfo] = []
    for it in items:
        sym = (it.tradingsymbol or "").upper()
        exch = (it.exchange or "").upper()
        # Anything already with FUT in its name, or anything on NSE/BSE/
        # NFO/CDS etc., is already tradeable (or a bare CDS root that
        # passes through as-is) — pass through.
        if "FUT" in sym or exch != "MCX":
            out.append(_item_info(it))
            continue
        # MCX commodity roots: front + next month (up to 2 rows).
        # Resolve the front + next month future for this commodity root.
        # Empty list ⇒ instruments cache cold or the root has no listed
        # futures; pass the raw row through so the operator still sees
        # something (worst case the cell renders the bare name until the
        # next API roundtrip).
        futures = await lookup_mcx_futures_list(sym, limit=2)
        if not futures:
            out.append(_item_info(it))
            continue
        for i, fsym in enumerate(futures):
            # Synthetic id pattern: parent_id * 1000 + i. Lets the
            # frontend treat each expansion as a unique row + lets the
            # backend recover the parent id on delete via `id // 1000`.
            out.append(WatchlistItemInfo(
                id=it.id * 1000 + i,
                watchlist_id=it.watchlist_id,
                tradingsymbol=fsym,
                exchange=exch,
                alias=getattr(it, "alias", None),
                sort_order=it.sort_order * 10 + i,
                added_at=it.added_at.isoformat() if it.added_at else "",
            ))
    return out


def _is_designated_role(request) -> bool:
    """True when the JWT payload says role ∈ {admin, designated}.
    Gates writes on the global Pinned row."""
    payload = getattr(request.state, "token_payload", {}) or {}
    role = str(payload.get("role", "")).lower()
    return role in ("admin", "designated")


# ---------------------------------------------------------------------------
# Deleted (Jul 2026): _get_movers_mcx_live + _session_movers_mcx +
# _mcx_fut_map_cache. The unified `get_movers` body in WatchlistController
# now handles NSE + MCX in one pass — see notes there.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_movers helpers (extracted from WatchlistController.get_movers)
# ---------------------------------------------------------------------------

def _movers_probe_market_state(ist_now) -> tuple[bool, bool]:
    """Fetch NSE + MCX holiday sets and probe both markets. Returns
    `(nse_is_open, mcx_is_open)`. Falls back to `set()` on holiday
    fetch failure so a broker-side outage never wedges the movers grid."""
    from datetime import time as _dtime
    from backend.brokers.broker_apis import fetch_holidays
    from backend.shared.helpers.date_time_utils import is_market_open

    try:
        nse_holidays = fetch_holidays("NSE")
    except Exception:
        nse_holidays = set()
    try:
        mcx_holidays = fetch_holidays("MCX")
    except Exception:
        mcx_holidays = set()

    nse_is_open = is_market_open(
        ist_now, nse_holidays,
        market_start=_dtime(9, 15), market_end=_dtime(15, 30),
        exchange="NSE",
    )
    mcx_is_open = is_market_open(
        ist_now, mcx_holidays,
        market_start=_dtime(9, 0), market_end=_dtime(23, 30),
        exchange="MCX",
    )
    return nse_is_open, mcx_is_open


async def _movers_offhours_response(ist_today: str) -> "MoversResponse":
    """Both exchanges closed → return NSE snapshot fallback or empty
    response with logging."""
    global _session_movers
    snap_rows, reason = await _get_movers_now_off_hours()
    if reason == "snapshot":
        snap = await _load_latest_movers_snapshot()
        return MoversResponse(
            movers=snap_rows,
            threshold_pct=MOVER_THRESHOLD_PCT,
            session_date=snap.date.isoformat() if hasattr(snap.date, "isoformat") else str(snap.date),
            captured_at=snap.captured_at.isoformat() if snap else None,
        )
    if reason == "snapshot_missing_off_hours":
        logger.info(
            f"[MOVERS-EMPTY] reason=snapshot_missing_off_hours "
            f"universe_size=0 snapshot_present=False "
            f"session_movers={len(_session_movers)}"
        )
    return MoversResponse(
        movers=[], threshold_pct=MOVER_THRESHOLD_PCT, session_date=ist_today,
    )


def _build_nse_universe(all_items: list, is_mcx_underlying) -> set[str]:
    """Collect NSE equity underlying names that have an active CE/PE chain.
    Returns a set of bare root symbols (e.g. ``{'NIFTY', 'RELIANCE', ...}``)."""
    result: set[str] = set()
    for inst in all_items:
        if inst.t in ("CE", "PE") and inst.u:
            name = inst.u.upper()
            if not is_mcx_underlying(name):
                result.add(name)
    return result


async def _movers_rebuild_universes_if_needed(ist_today: str) -> None:
    """Once-per-IST-day rebuild of NSE + MCX universe caches. Mutates
    module-level `_underlyings_cache`, `_mcx_underlyings_cache`, and
    `_mcx_fut_map`. No-op when both caches are current."""
    from backend.api.cache import get_or_fetch
    from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS as _INST_TTL
    from backend.api.algo.derivatives import is_mcx_underlying

    global _underlyings_cache, _underlyings_cache_date
    global _mcx_underlyings_cache, _mcx_underlyings_cache_date, _mcx_fut_map

    needs_rebuild = (
        _underlyings_cache_date != ist_today or
        _mcx_underlyings_cache_date != ist_today
    )
    if not needs_rebuild:
        return
    try:
        resp = await get_or_fetch("instruments", _fetch_instruments,
                                  ttl_seconds=_INST_TTL)
        all_items = resp.items if resp else []
    except Exception as _exc:
        logger.warning(f"Movers: instruments fetch failed: {_exc}")
        all_items = []

    # NSE eq universe.
    new_nse_set = _build_nse_universe(all_items, is_mcx_underlying)
    if new_nse_set:
        _underlyings_cache = new_nse_set
        _underlyings_cache_date = ist_today

    # MCX universe (bare commodity roots + FUT symbol map).
    new_mcx_set, new_mcx_fut = _build_mcx_universe(all_items)
    if new_mcx_set:
        _mcx_underlyings_cache = new_mcx_set
        _mcx_fut_map = new_mcx_fut
        _mcx_underlyings_cache_date = ist_today

    logger.info(
        f"Movers: universe build — items={len(all_items)} "
        f"nse={len(new_nse_set)} mcx={len(new_mcx_set)} "
        f"mcx_fut={len(new_mcx_fut)}"
    )


def _movers_build_key_to_meta(
    nse_is_open: bool, mcx_is_open: bool,
) -> dict[str, dict]:
    """Assemble the `{broker_key: {underlying, exchange}}` map keyed by
    exchange-open state. NSE keys omitted when NSE is closed; MCX keys
    omitted when MCX is closed. Session-sticky entries from a closed
    exchange still surface via `_combine_movers`."""
    from backend.api.algo.derivatives import underlying_ltp_key
    key_to_meta: dict[str, dict] = {}
    if nse_is_open:
        for name in _underlyings_cache:
            key = underlying_ltp_key(name)
            key_to_meta[key] = {"underlying": name, "exchange": "NSE"}
    if mcx_is_open:
        for root in _mcx_underlyings_cache:
            fut_sym = _mcx_fut_map.get(root)
            if fut_sym:
                key_to_meta[f"MCX:{fut_sym}"] = {
                    "underlying": root, "exchange": "MCX",
                }
    return key_to_meta


async def _movers_fetch_quotes_cached(
    key_to_meta: dict[str, dict],
) -> dict:
    """One `broker.quote()` call over the merged universe, cached for
    30 s. Cache key includes universe size so an NSE-only → NSE+MCX
    transition doesn't reuse a stale batch."""
    import asyncio
    import time as _time
    from backend.api.cache import get_or_fetch as _gof
    from backend.brokers.registry import get_market_data_broker

    _MOVERS_QUOTE_TTL = 30

    async def _fetch_unified_movers_quotes() -> dict:
        keys = list(key_to_meta.keys())
        if not keys:
            return {}
        try:
            broker = get_market_data_broker()
            return await asyncio.to_thread(broker.quote, keys) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Movers unified quote fetch failed: {exc}")
            return {}

    cache_key = (
        f"movers_quotes_{int(_time.time() // _MOVERS_QUOTE_TTL)}"
        f"_{len(key_to_meta)}"
    )
    return await _gof(
        cache_key, _fetch_unified_movers_quotes, ttl_seconds=_MOVERS_QUOTE_TTL,
    )


def _movers_parse_broker_quote(
    kite_key: str,
    exchange: str,
    q: dict,
) -> tuple[float, float, Optional[str]]:
    """Extract ``(broker_ltp, prev_close, row_quote_symbol)`` from a raw
    Kite quote dict.

    ``row_quote_symbol`` is the resolved contract name for MCX rows (e.g.
    ``CRUDEOIL26JUNFUT``) so the frontend can key SSE lookups correctly.
    NSE rows return ``None`` — the bare underlying IS the SSE key."""
    broker_ltp = float(q.get("last_price") or 0.0)
    ohlc = q.get("ohlc") or {}
    prev_close = float(ohlc.get("close") or 0.0)
    row_quote_symbol: Optional[str] = None
    if exchange == "MCX" and kite_key.startswith("MCX:"):
        row_quote_symbol = kite_key[4:]
    return broker_ltp, prev_close, row_quote_symbol


def _movers_update_session_entry(
    underlying: str,
    change_pct: float,
    price: float,
    prev_close: float,
    exchange: str,
    source: str,
    animating: bool,
    row_quote_symbol: Optional[str],
) -> None:
    """Upsert the module-level ``_session_movers`` dict for *underlying*.

    If the underlying is already tracked, refreshes its last_price /
    price_source / is_animating and promotes peak_pct when the current
    move is larger. If it's new AND crosses the threshold, seeds a fresh
    entry. No-op when neither condition holds."""
    from datetime import datetime, timezone as _tz
    global _session_movers
    if underlying in _session_movers:
        entry = _session_movers[underlying]
        entry["last_pct"] = change_pct
        entry["last_price"] = price
        entry["current_price"] = price
        entry["previous_close"] = prev_close
        entry["price_source"] = source
        entry["is_animating"] = animating
        if row_quote_symbol:
            entry["quote_symbol"] = row_quote_symbol
        if abs(change_pct) > abs(entry["peak_pct"]):
            entry["peak_pct"] = change_pct
    elif abs(change_pct) >= MOVER_THRESHOLD_PCT:
        _session_movers[underlying] = {
            "first_seen_at": datetime.now(_tz.utc).isoformat(),
            "peak_pct": change_pct,
            "last_pct": change_pct,
            "last_price": price,
            "current_price": price,
            "previous_close": prev_close,
            "exchange": exchange,
            "price_source": source,
            "is_animating": animating,
            "quote_symbol": row_quote_symbol,
        }


def _movers_process_symbol(
    kite_key: str,
    meta: dict,
    quote_data: dict,
    nse_is_open: bool,
    mcx_is_open: bool,
) -> Optional[tuple[str, dict]]:
    """Per-symbol path: broker quote → resolver dispatch → change_pct.

    Returns `(underlying, live_entry)` for the caller to fold into the
    live snapshot, or `None` when no usable price was resolved. Mutates
    the module-level `_session_movers` dict (last_price / price_source /
    is_animating updates) as a side-effect."""
    from backend.api.helpers.price_resolver import resolve_current_price

    global _session_movers

    underlying = meta["underlying"]
    exchange = meta["exchange"]
    exch_is_open = (nse_is_open if exchange == "NSE" else mcx_is_open)
    q = quote_data.get(kite_key) or {}
    broker_ltp, prev_close, row_quote_symbol = _movers_parse_broker_quote(
        kite_key, exchange, q
    )

    # Resolver dispatch — kept even for the open-exchange branch
    # so tests can verify a single code path for the triad.
    price, source, animating = resolve_current_price(
        exchange_open=exch_is_open,
        live_ltp=(broker_ltp if broker_ltp > 0 else None),
        snapshot_close=None,           # movers don't overlay close
        snapshot_last_ltp=(broker_ltp if broker_ltp > 0 else None),
        settled=False,
    )

    if not price or prev_close == 0.0:
        # No usable price — update sticky last_price if we do have
        # a broker LTP (partial quote), otherwise skip.
        if underlying in _session_movers and broker_ltp:
            _session_movers[underlying]["last_price"] = broker_ltp
            _session_movers[underlying]["price_source"] = source
            _session_movers[underlying]["is_animating"] = animating
        return None

    change_pct = (price - prev_close) / prev_close * 100.0
    live_entry = {
        "peak_pct": change_pct,
        "last_pct": change_pct,
        "last_price": price,
        "current_price": price,
        "previous_close": prev_close,
        "exchange": exchange,
        "price_source": source,
        "is_animating": animating,
        "quote_symbol": row_quote_symbol,
    }

    _movers_update_session_entry(
        underlying, change_pct, price, prev_close,
        exchange, source, animating, row_quote_symbol,
    )
    return underlying, live_entry


def _movers_build_rows(combined: dict[str, dict]) -> list["MoverRow"]:
    """Assemble `MoverRow`s from the `_combine_movers` result and sort
    by absolute change_pct desc."""
    rows: list[MoverRow] = []
    for underlying, entry in combined.items():
        change_pct = entry["last_pct"]
        rows.append(MoverRow(
            tradingsymbol=underlying,
            exchange=entry["exchange"],
            last_price=entry["last_price"],
            previous_close=entry["previous_close"],
            change_pct=change_pct,
            peak_pct=entry.get("peak_pct", change_pct),
            sticky=(underlying in _session_movers
                    and abs(change_pct) < MOVER_THRESHOLD_PCT),
            # Unified animation triad (Jul 2026) — populated from the
            # resolver output stashed on the entry during the loop above.
            # Session-sticky rows carry the last-observed values (may be
            # from an exchange that has since closed → is_animating=False).
            price_source=entry.get("price_source", "live"),
            current_price=entry.get("current_price", entry["last_price"]),
            is_animating=entry.get("is_animating", True),
            quote_symbol=entry.get("quote_symbol") or None,
        ))
    rows.sort(key=lambda r: abs(r.change_pct), reverse=True)
    return rows


def _movers_build_live_rows(
    key_to_meta: dict,
    quote_data: dict,
    nse_is_open: bool,
    mcx_is_open: bool,
    ist_today: str,
) -> tuple[list["MoverRow"], dict]:
    """Build the unified live snapshot dict, combine with session_movers,
    assemble sorted MoverRows, and persist NSE rows to the snapshot store.

    Returns ``(rows, live_snapshot)`` so the caller can log empty-reason
    diagnostics with the live_snapshot size."""
    import asyncio
    global _session_movers

    live_snapshot: dict[str, dict] = {}
    for kite_key, meta in key_to_meta.items():
        result = _movers_process_symbol(
            kite_key, meta, quote_data, nse_is_open, mcx_is_open,
        )
        if result is not None:
            underlying, live_entry = result
            live_snapshot[underlying] = live_entry

    combined: dict[str, dict] = _combine_movers(
        live_snapshot, _session_movers, MOVER_TOP_N,
    )
    rows = _movers_build_rows(combined)

    # Persist NSE-only rows during NSE hours only.
    #
    # The `nse_rows` filter by exchange is not sufficient: when NSE is
    # closed but MCX is still open (15:30–23:30 IST), `_session_movers`
    # still carries NSE sticky entries from the afternoon, so `nse_rows`
    # is non-empty and its UPSERT would overwrite the full-universe row
    # that `_force_movers_snapshot` wrote at the NSE close lifecycle
    # event.  Guard on `nse_is_open` so the close snapshot is preserved
    # intact for the off-hours fallback path.
    if nse_is_open:
        nse_rows = [r for r in rows if r.exchange == "NSE"]
        if nse_rows:
            asyncio.create_task(_save_movers_snapshot(nse_rows, ist_today))

    return rows, live_snapshot


async def _add_item_authorize_and_insert(
    wl_id: int,
    tradingsymbol: str,
    exchange: str,
    alias: Optional[str],
    sort_order_hint: Optional[int],
    username: str,
    request,
) -> "WatchlistItem":
    """Fetch + authorize the watchlist, run cap + dedupe checks, then
    INSERT the new item and commit. Returns the persisted ``WatchlistItem``."""
    async with async_session() as session:
        user_id = await _resolve_user_id(session, username)
        wl_row = await session.execute(
            select(Watchlist).where(
                Watchlist.id == wl_id,
                ((Watchlist.user_id == user_id) | (Watchlist.is_global == True)),
            )
        )
        wl = wl_row.scalar_one_or_none()
        if not wl:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        if wl.is_global and not _is_designated_role(request):
            raise HTTPException(
                status_code=403,
                detail="Pinned watchlist can only be edited by designated partners",
            )
        count_r = await session.execute(
            select(func.count(WatchlistItem.id))
            .where(WatchlistItem.watchlist_id == wl_id)
        )
        if int(count_r.scalar() or 0) >= _MAX_ITEMS_PER_LIST:
            raise HTTPException(
                status_code=409,
                detail=f"Watchlist cap reached ({_MAX_ITEMS_PER_LIST} items)",
            )
        dup_r = await session.execute(
            select(WatchlistItem.id).where(
                WatchlistItem.watchlist_id == wl_id,
                WatchlistItem.tradingsymbol == tradingsymbol,
                WatchlistItem.exchange      == exchange,
            )
        )
        if dup_r.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{exchange}:{tradingsymbol} already in this watchlist",
            )
        max_sort_r = await session.execute(
            select(func.coalesce(func.max(WatchlistItem.sort_order), -1))
            .where(WatchlistItem.watchlist_id == wl_id)
        )
        sort_val = (sort_order_hint if sort_order_hint is not None
                    else int(max_sort_r.scalar() or -1) + 1)
        now = datetime.now(timezone.utc)
        it = WatchlistItem(
            watchlist_id=wl_id,
            tradingsymbol=tradingsymbol, exchange=exchange,
            alias=alias,
            sort_order=sort_val, added_at=now,
        )
        session.add(it)
        wl.updated_at = now
        await session.commit()
    return it


async def _add_item_ticker_subscribe(tradingsymbol: str, exchange: str) -> None:
    """Push the newly added symbol to the KiteTicker for live ticks.
    Non-fatal: ticker subscription failure never blocks the add response."""
    try:
        from backend.api.routes.quote import _resolve_token_for_sym
        from backend.brokers.kite_ticker import get_ticker
        tok = await _resolve_token_for_sym(tradingsymbol, exchange)
        if tok is not None:
            get_ticker().subscribe_with_sym([(tok, tradingsymbol)])
            logger.debug(
                f"Watchlist: subscribed ticker token={tok} for "
                f"{exchange}:{tradingsymbol}"
            )
    except Exception as _te:
        logger.debug(
            f"Watchlist: ticker subscribe skipped for "
            f"{exchange}:{tradingsymbol}: {_te}"
        )


def _apply_reorder_fields(
    it: "WatchlistItem",
    data: "ReorderItemRequest",
) -> None:
    """Apply sort_order and alias updates from a PATCH item request to
    ``it`` in-place. Empty alias string clears the alias."""
    if data.sort_order is not None:
        it.sort_order = int(data.sort_order)
    if data.alias is not None:
        a = data.alias.strip()
        if a and len(a) > 64:
            raise HTTPException(status_code=422, detail="Alias too long")
        it.alias = a or None


async def _apply_rename_fields(
    session,
    wl: "Watchlist",
    wl_id: int,
    user_id: int,
    data: "RenameWatchlistRequest",
) -> None:
    """Apply the mutable fields from a PATCH watchlist request to ``wl``
    in-place. Validates name length; enforces single-default invariant via
    a bulk UPDATE when ``is_default=True`` is requested."""
    if data.name is not None:
        name = (data.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name required")
        if len(name) > 64:
            raise HTTPException(status_code=422, detail="Name too long")
        wl.name = name
    if data.sort_order is not None:
        wl.sort_order = int(data.sort_order)
    if data.is_default is not None:
        if data.is_default:
            from sqlalchemy import update
            await session.execute(
                update(Watchlist)
                .where(Watchlist.user_id == user_id, Watchlist.id != wl_id)
                .values(is_default=False)
            )
        wl.is_default = bool(data.is_default)
    if data.is_pinned is not None:
        wl.is_pinned = bool(data.is_pinned)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class WatchlistController(Controller):
    path = "/api/watchlist"
    # No controller-level guard — Litestar merges controller + handler
    # guards additively, so per-handler relaxation is impossible against
    # a controller-level jwt_guard. Each handler below carries its own
    # guard: reads (list / get / quotes) use auth_or_demo_guard so demo
    # sessions see a synthetic "Markets" list; writes use jwt_guard so
    # only authenticated users can mutate. Movers stays auth_or_demo
    # too (was already overridden).

    @get("/", guards=[auth_or_demo_guard])
    async def list_watchlists(self, request: Request) -> list[WatchlistInfo]:
        """List the shared global Pinned + every user-owned watchlist.
        Demo visitors get the synthetic Markets list (read-only)."""
        if getattr(request.state, "is_demo", False):
            items = _demo_watchlist_items()
            return [WatchlistInfo(
                id=DEMO_WATCHLIST_ID, name=DEMO_WATCHLIST_NAME,
                sort_order=0, is_default=True, is_pinned=True,
                item_count=len(items),
                created_at="", updated_at="",
            )]
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            # Shared global Pinned + per-user lists in a single query.
            result = await session.execute(
                select(Watchlist)
                .where(
                    (Watchlist.is_global == True)
                    | (Watchlist.user_id == user_id)
                )
                .order_by(
                    # Global Pinned always first (is_global true sorts
                    # ahead of false by the negated bool trick).
                    Watchlist.is_global.desc(),
                    Watchlist.sort_order, Watchlist.id,
                )
            )
            wls = result.scalars().all()
            count_q = await session.execute(
                select(WatchlistItem.watchlist_id, func.count(WatchlistItem.id))
                .where(WatchlistItem.watchlist_id.in_([wl.id for wl in wls]))
                .group_by(WatchlistItem.watchlist_id)
            )
            counts: dict[int, int] = {wid: int(cnt) for wid, cnt in count_q.all()}
        return [_wl_info(wl, counts.get(wl.id, 0)) for wl in wls]

    @post("/", guards=[jwt_guard])
    async def create_watchlist(self, data: CreateWatchlistRequest, request: Request) -> WatchlistInfo:
        name = (data.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name required")
        if len(name) > 64:
            raise HTTPException(status_code=422, detail="Name too long (max 64)")
        if name.lower() == "pinned":
            raise HTTPException(
                status_code=409,
                detail="'Pinned' is the shared global list — pick another name",
            )
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            # Dedupe on (user_id, name) — defended by the unique index
            # but checking ahead gives a friendlier 409 message.
            existing = await session.execute(
                select(Watchlist.id).where(
                    Watchlist.user_id == user_id, Watchlist.name == name,
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise HTTPException(status_code=409, detail=f"Watchlist '{name}' already exists")
            now = datetime.now(timezone.utc)
            # Place new lists after every existing one.
            max_sort = await session.execute(
                select(func.coalesce(func.max(Watchlist.sort_order), -1))
                .where(Watchlist.user_id == user_id)
            )
            wl = Watchlist(
                user_id=user_id, name=name,
                sort_order=int(max_sort.scalar() or -1) + 1,
                is_default=False, is_pinned=False,
                created_at=now, updated_at=now,
            )
            session.add(wl)
            await session.commit()
        return _wl_info(wl, 0)

    @get("/{wl_id:int}", guards=[auth_or_demo_guard])
    async def get_watchlist(self, wl_id: int, request: Request) -> WatchlistFull:
        if getattr(request.state, "is_demo", False):
            if wl_id != DEMO_WATCHLIST_ID:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            return WatchlistFull(
                id=DEMO_WATCHLIST_ID, name=DEMO_WATCHLIST_NAME,
                sort_order=0, is_default=True, is_pinned=True,
                created_at="", updated_at="",
                items=_demo_watchlist_items(),
            )
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            wl_row = await session.execute(
                select(Watchlist).where(
                    Watchlist.id == wl_id,
                    # Either the user owns it OR it's the shared global.
                    ((Watchlist.user_id == user_id) | (Watchlist.is_global == True)),
                )
            )
            wl = wl_row.scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            items_row = await session.execute(
                select(WatchlistItem).where(WatchlistItem.watchlist_id == wl_id)
                .order_by(WatchlistItem.sort_order, WatchlistItem.id)
            )
            items = items_row.scalars().all()
        # Expand any bare MCX / CDS commodity root into its actual
        # current + next-month future contracts (display only — DB rows
        # stay as roots so the operator's curated list survives expiry
        # rollovers).
        expanded = await _expand_root_items_to_futures(items)
        return WatchlistFull(
            id=wl.id, name=wl.name, sort_order=wl.sort_order,
            is_default=wl.is_default, is_pinned=wl.is_pinned,
            is_global=getattr(wl, "is_global", False),
            created_at=wl.created_at.isoformat() if wl.created_at else "",
            updated_at=wl.updated_at.isoformat() if wl.updated_at else "",
            items=expanded,
        )

    @patch("/{wl_id:int}", guards=[jwt_guard])
    async def rename_watchlist(
        self, wl_id: int, data: RenameWatchlistRequest, request: Request,
    ) -> WatchlistInfo:
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            wl_row = await session.execute(
                select(Watchlist).where(
                    Watchlist.id == wl_id,
                    ((Watchlist.user_id == user_id) | (Watchlist.is_global == True)),
                )
            )
            wl = wl_row.scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            # Only admin / designated can mutate the shared global row.
            if wl.is_global and not _is_designated_role(request):
                raise HTTPException(
                    status_code=403,
                    detail="Pinned watchlist can only be edited by designated partners",
                )
            await _apply_rename_fields(session, wl, wl_id, user_id, data)
            wl.updated_at = datetime.now(timezone.utc)
            await session.commit()
            # Recount items for the return payload.
            from sqlalchemy import func
            r = await session.execute(
                select(func.count(WatchlistItem.id))
                .where(WatchlistItem.watchlist_id == wl_id)
            )
            count = int(r.scalar() or 0)
        return _wl_info(wl, count)

    @delete("/{wl_id:int}", status_code=200, guards=[jwt_guard])
    async def delete_watchlist(self, wl_id: int, request: Request) -> dict:
        username = _actor_sub(request)
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            wl_row = await session.execute(
                select(Watchlist).where(
                    Watchlist.id == wl_id,
                    ((Watchlist.user_id == user_id) | (Watchlist.is_global == True)),
                )
            )
            wl = wl_row.scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            # Global Pinned is undeletable — would leave every user
            # without their always-present list.
            if wl.is_global:
                raise HTTPException(
                    status_code=403,
                    detail="Shared Pinned watchlist cannot be deleted",
                )
            # Explicitly nuke child items first, then the watchlist.
            # SQLAlchemy without a configured relationship() doesn't
            # always honour the DB-level CASCADE on the FK — operator
            # reported "deleting test is not working. I should be able
            # to delete the watchlist test even if it has a symbol".
            from sqlalchemy import delete as sa_delete
            await session.execute(
                sa_delete(WatchlistItem)
                .where(WatchlistItem.watchlist_id == wl_id)
            )
            await session.delete(wl)
            await session.commit()
        logger.info(f"Watchlist: deleted id={wl_id} by {username!r}")
        return {"detail": f"Watchlist {wl_id} deleted"}

    # ── Movers ────────────────────────────────────────────────────────

    @get("/movers", guards=[auth_or_demo_guard])
    async def get_movers(self, request: Request) -> MoversResponse:
        """Session-sticky movers under the unified animation model (Jul 2026).

        Single pass over the merged NSE + MCX universe. For every symbol:
          • Route the exchange-open flag + broker quote through
            `resolve_current_price`. This is the single decision point that
            positions.py and holdings.py already use — one branch matrix
            across the whole codebase.
          • Compute `change_pct = (current - prev_close) / prev_close * 100`.
          • Update session-sticky dict when the row crosses threshold.

        Two market-state branches (down from three pre-refactor):
          1. AT LEAST ONE exchange open → unified live path. Rows on the
             closed exchange (if any) come from the session-sticky dict
             or the snapshot map. Rows on the open exchange come from a
             fresh broker quote.
          2. BOTH closed → NSE DB snapshot fallback (unchanged).

        Snapshot persistence: NSE-exchange rows only. MCX rows are filtered
        out at the write site so the NSE 15:29 snapshot in movers_snapshots
        isn't overwritten by evening MCX data (which the closed-hours
        fallback would then serve on an equity-context grid before 09:15).
        """
        import asyncio
        from backend.shared.helpers.date_time_utils import timestamp_indian

        global _session_movers, _session_date, _mcx_fut_map

        # Session rollover at IST midnight.
        ist_now = timestamp_indian()
        ist_today = ist_now.date().isoformat()
        if _session_date != ist_today:
            _session_movers = {}
            _mcx_fut_map = {}   # cleared alongside _mcx_underlyings_cache_date buster
            _session_date = ist_today

        # Demo sessions never touch the live broker — serve the same
        # closed-hours snapshot path that anonymous visitors see.
        if getattr(request.state, "is_demo", False):
            return await _movers_offhours_response(ist_today)

        # ── Market-state probe ────────────────────────────────────────
        nse_is_open, mcx_is_open = _movers_probe_market_state(ist_now)

        # Branch 2: BOTH closed → NSE snapshot fallback (unchanged).
        if not nse_is_open and not mcx_is_open:
            return await _movers_offhours_response(ist_today)

        # ── Branch 1: at least one exchange open → unified live path ──

        # Build BOTH universe caches once per calendar day. NSE = underlyings
        # with a CE/PE chain (spot symbols like NIFTY, RELIANCE). MCX = bare
        # commodity roots with a CE/PE chain, keyed off the earliest-expiry
        # FUT tradingsymbol (broker.quote() returns nothing on the bare root).
        await _movers_rebuild_universes_if_needed(ist_today)

        # Build the unified key_to_meta map. Only include NSE keys when
        # NSE is open; MCX keys when MCX is open. When one exchange is
        # closed, session-sticky entries from that exchange stay in
        # `_session_movers` and continue to appear via `_combine_movers`
        # overlay (their `last_price` won't update — that's the intended
        # sticky behaviour).
        key_to_meta = _movers_build_key_to_meta(nse_is_open, mcx_is_open)

        if not key_to_meta and not _session_movers:
            logger.info(
                f"[MOVERS-EMPTY] reason=no_universe "
                f"nse_open={nse_is_open} mcx_open={mcx_is_open} "
                f"nse_cache={len(_underlyings_cache)} "
                f"mcx_cache={len(_mcx_underlyings_cache)}"
            )
            return MoversResponse(
                movers=[], threshold_pct=MOVER_THRESHOLD_PCT, session_date=ist_today,
            )

        # Cached 30 s quote batch. Cache key is universe-scoped so that a
        # transition from NSE-only → NSE+MCX (11:00 IST) doesn't reuse the
        # NSE-only batch. Key components: TTL window + universe-size digest.
        quote_data = await _movers_fetch_quotes_cached(key_to_meta)

        if not quote_data and key_to_meta:
            logger.info(
                f"[MOVERS-EMPTY] reason=broker_quote_empty "
                f"universe_size={len(key_to_meta)} "
                f"session_movers={len(_session_movers)}"
            )
            # Fall through — session_movers may still yield rows.

        rows, live_snapshot = _movers_build_live_rows(
            key_to_meta, quote_data, nse_is_open, mcx_is_open, ist_today,
        )

        if not rows:
            logger.info(
                f"[MOVERS-EMPTY] reason=no_matches "
                f"universe_size={len(key_to_meta)} "
                f"session_movers={len(_session_movers)} "
                f"live_snapshot={len(live_snapshot)}"
            )

        return MoversResponse(
            movers=rows,
            threshold_pct=MOVER_THRESHOLD_PCT,
            session_date=ist_today,
        )

    # ── Items ─────────────────────────────────────────────────────────

    @post("/{wl_id:int}/items", status_code=201, guards=[jwt_guard])
    async def add_item(
        self, wl_id: int, data: AddItemRequest, request: Request,
    ) -> WatchlistItemInfo:
        tradingsymbol = (data.tradingsymbol or "").strip().upper()
        exchange      = (data.exchange      or "").strip().upper()
        if not tradingsymbol or not exchange:
            raise HTTPException(status_code=422, detail="tradingsymbol + exchange required")
        alias = (data.alias or "").strip() or None
        if alias and len(alias) > 64:
            raise HTTPException(status_code=422, detail="Alias too long")
        username = _actor_sub(request)
        it = await _add_item_authorize_and_insert(
            wl_id, tradingsymbol, exchange, alias, data.sort_order,
            username, request,
        )
        await _add_item_ticker_subscribe(tradingsymbol, exchange)
        return _item_info(it)

    @patch("/{wl_id:int}/items/{item_id:int}", guards=[jwt_guard])
    async def reorder_item(
        self, wl_id: int, item_id: int, data: ReorderItemRequest, request: Request,
    ) -> WatchlistItemInfo:
        username = _actor_sub(request)
        # Same synthetic-id strip as remove_item — keeps alias edits
        # working on the expanded MCX / CDS futures.
        real_item_id = item_id // 1000 if item_id >= 1000 else item_id
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            # Two-step fetch — see remove_item for why the auto-joined
            # query mis-filtered the shared global Pinned (user_id is
            # NULL on global rows).
            it = (await session.execute(
                select(WatchlistItem).where(
                    WatchlistItem.id == real_item_id,
                    WatchlistItem.watchlist_id == wl_id,
                )
            )).scalar_one_or_none()
            if not it:
                raise HTTPException(status_code=404, detail="Item not found")
            wl = (await session.execute(
                select(Watchlist).where(Watchlist.id == wl_id)
            )).scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            if wl.user_id != user_id and not wl.is_global:
                raise HTTPException(status_code=404, detail="Item not found")
            if wl.is_global and not _is_designated_role(request):
                raise HTTPException(
                    status_code=403,
                    detail="Pinned watchlist can only be edited by designated partners",
                )
            _apply_reorder_fields(it, data)
            await session.commit()
        return _item_info(it)

    @get("/{wl_id:int}/quotes", guards=[auth_or_demo_guard])
    async def quotes(self, wl_id: int, request: Request) -> WatchlistQuotes:
        """Batched live quotes for every item in the watchlist. 5-second
        in-process cache so multiple concurrent polls share one Kite
        round-trip. The cache is keyed on watchlist_id only — adds /
        removes will be reflected on the next refresh (max 5s lag).
        Demo sessions: only the synthetic Markets list (-1) is valid;
        items are computed from the canonical seed each call (no DB)."""
        import time
        from datetime import datetime, timezone
        if getattr(request.state, "is_demo", False):
            if wl_id != DEMO_WATCHLIST_ID:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            items = _demo_watchlist_items_as_objs()
        else:
            username = _actor_sub(request)
            async with async_session() as session:
                user_id = await _resolve_user_id(session, username)
                # CRITICAL: include `is_global == True` so the operator-
                # facing pinned watchlist (user_id = NULL) matches. Every
                # other route in this file uses `((user_id == user) |
                # (is_global == True))` for this same reason; the /quotes
                # endpoint was the lone holdout — pinned + global lists
                # 404'd from this route silently, so operators saw the
                # pinned grid LTP/values frozen even though the watchlist
                # itself rendered fine (the LIST endpoint correctly
                # matches global). Movers updated because they use a
                # totally separate /watchlist/movers path.
                wl_row = await session.execute(
                    select(Watchlist).where(
                        Watchlist.id == wl_id,
                        ((Watchlist.user_id == user_id)
                         | (Watchlist.is_global == True)),
                    )
                )
                wl = wl_row.scalar_one_or_none()
                if not wl:
                    raise HTTPException(status_code=404, detail="Watchlist not found")
                items_row = await session.execute(
                    select(WatchlistItem).where(WatchlistItem.watchlist_id == wl_id)
                    .order_by(WatchlistItem.sort_order, WatchlistItem.id)
                )
                items = list(items_row.scalars().all())

        # Cache check — same watchlist hit within TTL returns cached.
        now = time.monotonic()
        cached = _QUOTE_CACHE.get(wl_id)
        if cached and (now - cached[0]) < _QUOTE_TTL_SECONDS:
            # Defensive: if the item set changed (operator added/removed
            # between polls), recompute the keys but reuse the broker
            # data we already have.
            if {q.item_id for q in cached[1].items} == {it.id for it in items}:
                return cached[1]

        quotes = await _fetch_quotes(items)
        out = WatchlistQuotes(
            watchlist_id=wl_id,
            refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            items=quotes,
        )
        _QUOTE_CACHE[wl_id] = (now, out)
        return out

    @delete("/{wl_id:int}/items/{item_id:int}", status_code=200, guards=[jwt_guard])
    async def remove_item(
        self, wl_id: int, item_id: int, request: Request,
    ) -> dict:
        username = _actor_sub(request)
        # Synthetic-id detection: when a stored bare MCX / CDS root is
        # expanded into actual future contracts at API time, each
        # expansion gets id = parent_id * 1000 + i. Strip that back so
        # delete on the synthesised row targets the underlying real
        # row. id < 1000 is always real.
        real_item_id = item_id // 1000 if item_id >= 1000 else item_id
        async with async_session() as session:
            user_id = await _resolve_user_id(session, username)
            # Two-step query — explicit row fetch avoids the implicit
            # auto-join SQLAlchemy was building without a configured
            # relationship between WatchlistItem and Watchlist, which
            # was returning zero rows on the global Pinned (user_id is
            # NULL on global). Pull the item + the watchlist
            # independently and combine in Python.
            it = (await session.execute(
                select(WatchlistItem).where(
                    WatchlistItem.id == real_item_id,
                    WatchlistItem.watchlist_id == wl_id,
                )
            )).scalar_one_or_none()
            if not it:
                raise HTTPException(status_code=404, detail="Item not found")
            wl = (await session.execute(
                select(Watchlist).where(Watchlist.id == wl_id)
            )).scalar_one_or_none()
            if not wl:
                raise HTTPException(status_code=404, detail="Watchlist not found")
            # User must own the watchlist OR it's the shared global.
            if wl.user_id != user_id and not wl.is_global:
                raise HTTPException(status_code=404, detail="Item not found")
            if wl.is_global and not _is_designated_role(request):
                raise HTTPException(
                    status_code=403,
                    detail="Pinned watchlist can only be edited by designated partners",
                )
            await session.delete(it)
            await session.commit()
        return {"detail": f"Item {real_item_id} removed"}


# ---------------------------------------------------------------------------
# Public helper — used by user-creation paths to seed defaults eagerly.
# ---------------------------------------------------------------------------

async def seed_default_watchlists_for_user(user_id: int) -> None:
    """Run the same idempotent seed used by the lazy path. Called from
    auth.register / admin.create_user / scripts/manage.py so new users
    have their lists populated before they ever hit the /watchlist
    endpoint."""
    async with async_session() as session:
        await _ensure_default_watchlists(session, user_id)
