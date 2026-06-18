"""
`/api/admin/hedge-proxies/*` — pair-only CRUD for the proxy-hedge
cross-reference table.

Operator: "to start with table can have goldm and gold, with goldbees
cross reference, similarly silverm, silver and silverbees. the
conversion is dynamic, the code should find it based units and market
value and convert into option lots and qty."

Schema simplified to just (proxy_symbol, target_root, is_active, note).
Conversion factor is derived at runtime in the frontend from current
LTPs; the lot count is derived from `effective_qty / target_lot_size`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, delete, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select, text

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session, engine
from backend.api.models import HedgeProxy
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Regression helper ─────────────────────────────────────────────────


# Index / commodity symbols that need an explicit Kite exchange hint
# beyond the default NSE-equity guess. NIFTY 50 + BANK NIFTY trade as
# index instruments on NSE with the "INDICES" naming convention; GOLD /
# SILVER / etc. land on MCX via the front-month future. Anything not in
# this map falls back to NSE-equity (Stage 3 stock proxies).
_TARGET_HINTS: dict[str, tuple[str, str]] = {
    "NIFTY":     ("NSE", "NIFTY 50"),
    "BANKNIFTY": ("NSE", "NIFTY BANK"),
    "FINNIFTY":  ("NSE", "NIFTY FIN SERVICE"),
    "GOLD":      ("MCX", "GOLD"),     # resolves to front-month FUT via instruments search
    "GOLDM":     ("MCX", "GOLDM"),
    "SILVER":    ("MCX", "SILVER"),
    "SILVERM":   ("MCX", "SILVERM"),
}

# MCX commodity targets — the front-month FUT rolls monthly, so a fresh
# contract typically only has 30–60 calendar days of bars available when
# the regression task runs. Cap the window + min-bars guard tighter so
# the regression actually completes on a fresh contract instead of
# aborting at the n < 15 guard for the first month after each roll.
# Operator: "the challenge mcx underlying or not equities they are
# futures which have different expiration different from option
# expiraiton. the root and underlying reference needs to be rolled over
# which is not present in equites" — option (b) per the design doc:
# roll-aware shorter window, no continuous-stitch yet.
_MCX_COMMODITY_ROOTS: set[str] = {
    "GOLD", "GOLDM", "GOLDPETAL", "GOLDGUINEA",
    "SILVER", "SILVERM", "SILVERMIC",
    "CRUDEOIL", "CRUDEOILM",
    "NATURALGAS", "NATGASMINI",
    "COPPER", "ZINC", "ALUMINIUM", "ALUMINI", "NICKEL", "LEAD", "LEADMINI",
    "ZINCMINI", "MENTHAOIL", "COTTON",
}


# Sprint E (audit) — in-process instruments cache. `_compute_regression`
# calls `_resolve_token` TWICE per pair (proxy + target). The background
# task runs N pairs in series, so without caching that's 2N
# `broker.instruments(exchange=…)` calls on every regression sweep —
# each one returns a ~90k-row list. Instruments don't change intraday;
# a 1-hour TTL is more than safe (Kite refreshes the dump nightly).
import time as _time
_INSTRUMENTS_CACHE: dict[str, tuple[float, list]] = {}
_INSTRUMENTS_CACHE_TTL = 3600


def _resolve_token(broker, symbol: str, exchange_hint: str) -> Optional[int]:
    """Resolve `symbol` to a Kite instrument_token via broker.instruments().
    Returns None when the symbol isn't found on the hinted exchange and
    the front-month-future fallback for MCX commodities doesn't match
    either. Best-effort — quiet skip on miss; regression handler logs
    the resolved-pair-count so the operator knows what worked."""
    import re
    cache_key = str(exchange_hint or "")
    cached = _INSTRUMENTS_CACHE.get(cache_key)
    insts: list = []
    if cached and cached[0] > _time.time():
        insts = cached[1]
    else:
        try:
            insts = broker.instruments(exchange=exchange_hint) or []
        except Exception:
            return None
        _INSTRUMENTS_CACHE[cache_key] = (_time.time() + _INSTRUMENTS_CACHE_TTL, insts)
    # Exact match first.
    for inst in insts:
        ts = str(inst.get("tradingsymbol") or "").upper()
        if ts == symbol.upper():
            tk = inst.get("instrument_token")
            return int(tk) if tk else None
    # MCX commodity fallback — front-month future. The MCX tradingsymbol
    # shape is `<ROOT><YY><MON>FUT` (e.g. GOLD26JUNFUT). Match the ROOT
    # EXACTLY by parsing the prefix before the first digit. Operator:
    # "goldm and gold cannot have same root. as they underlying may
    # have different prices based weight and liquidity." The prior
    # `startswith(symbol)` check collapsed GOLD with GOLDM /
    # GOLDPETAL / GOLDGUINEA and SILVER with SILVERM / SILVERMIC,
    # silently letting the wrong contract supply spot + history.
    if exchange_hint == "MCX":
        target_root = symbol.upper()
        candidates = []
        for inst in insts:
            ts = str(inst.get("tradingsymbol") or "").upper()
            if not ts.endswith("FUT"):
                continue
            m = re.match(r"^([A-Z]+)\d", ts)
            if m and m.group(1) == target_root:
                candidates.append(inst)
        candidates.sort(key=lambda i: str(i.get("expiry") or ""))
        if candidates:
            tk = candidates[0].get("instrument_token")
            return int(tk) if tk else None
    return None


def _compute_regression(broker, proxy_symbol: str, target_root: str,
                        days: int = 60) -> tuple[Optional[float], Optional[float], int]:
    """Run a daily-returns regression of proxy vs target. Returns
    (beta, r_squared, sample_size) — beta/r2 are None when either side
    can't be resolved or there aren't enough overlapping bars.

    Math: `proxy_return = α + β × target_return + ε`
          β = Cov(target, proxy) / Var(target)
          R² = correlation²
    """
    # numpy is a runtime dep used elsewhere (sim driver, performance
    # task) — safe to import inline so import_pyc doesn't pull it for
    # the cold-start path on operators who never trigger Stage 3.
    import numpy as np

    p_token = _resolve_token(broker, proxy_symbol, "NSE")
    if not p_token:
        # Stock proxies live on NSE-equity; commodity proxies (GOLDBEES
        # etc.) are tracked ETFs that DO list on NSE so the default is
        # correct for both classes.
        return None, None, 0
    hint_exchange, hint_symbol = _TARGET_HINTS.get(target_root.upper(), ("NSE", target_root))
    t_token = _resolve_token(broker, hint_symbol, hint_exchange)
    if not t_token:
        return None, None, 0

    # MCX commodity targets: the front-month FUT we just resolved was
    # listed at most ~60 days ago (typical contract life is 2-5 months
    # depending on commodity, but only the last ~30 days of bars are
    # reliably available pre-expiry). Clamp the window + min-overlap
    # guard tighter so the regression actually runs on a fresh contract
    # immediately after monthly rollover. Equity targets keep the
    # full 60-day window — index instruments are continuous.
    is_mcx = target_root.upper() in _MCX_COMMODITY_ROOTS
    if is_mcx:
        days = min(days, 30)
        min_overlap = 12     # need ≥ 12 overlapping bars (was 20)
        min_returns = 8      # need ≥ 8 daily returns (was 15)
    else:
        min_overlap = 20
        min_returns = 15

    # Sprint E (audit) — tz-aware UTC; matches the rest of the codebase.
    # Daily-bar `historical_data` ignores the time component so this is
    # behaviourally equivalent today, but a future broker client may
    # validate tz-awareness and reject naive datetimes.
    to_d = datetime.now(timezone.utc)
    from_d = to_d - timedelta(days=days + 30)   # +30 to absorb holidays / weekends
    try:
        p_bars = broker.historical_data(p_token, from_d, to_d, "day") or []
        t_bars = broker.historical_data(t_token, from_d, to_d, "day") or []
    except Exception as e:
        logger.warning(f"hedge-proxy regression: historical_data failed: {e}")
        return None, None, 0

    # Align by date string. Daily candles are unambiguous on calendar.
    def _by_date(bars):
        out = {}
        for b in bars:
            ts = b.get("date") if isinstance(b, dict) else None
            close = b.get("close") if isinstance(b, dict) else None
            if ts is None or close is None:
                continue
            try:
                out[str(ts)[:10]] = float(close)
            except (TypeError, ValueError):
                continue
        return out

    p_map = _by_date(p_bars)
    t_map = _by_date(t_bars)
    common = sorted(set(p_map.keys()) & set(t_map.keys()))[-(days + 1):]
    if len(common) < min_overlap:
        return None, None, len(common)
    p_closes = [p_map[d] for d in common]
    t_closes = [t_map[d] for d in common]
    # Daily returns.
    p_ret = np.diff(np.log(p_closes))
    t_ret = np.diff(np.log(t_closes))
    if len(p_ret) < min_returns or float(np.var(t_ret)) <= 0:
        return None, None, len(p_ret)
    cov = float(np.cov(p_ret, t_ret, ddof=1)[0][1])
    var_t = float(np.var(t_ret, ddof=1))
    beta = cov / var_t if var_t > 0 else None
    # Sprint E (audit) — reject pathological β values. |β| > 5 means
    # one of the bar series has a near-singular outlier (split day, bad
    # tick, fat-finger trade) that's driving the regression off the
    # rails. Bloomberg PRM caps β inputs to [−3, 3] for stability;
    # OptionVue warns on |β| > 2. We're slightly more permissive (5)
    # because leveraged ETF proxies can legitimately overshoot 3, but
    # 5 is the operator-meaningful "this is broken" threshold. Log
    # before rejecting so the operator can see WHY their regression
    # failed in the UI/API trace.
    if beta is not None and abs(beta) > 5.0:
        logger.warning(
            f"hedge-proxy regression: rejecting implausible β={beta:.3f} for "
            f"{proxy_symbol}→{target_root} (n={len(p_ret)}). Likely a bad "
            f"bar in the input series."
        )
        return None, None, len(p_ret)
    r = float(np.corrcoef(p_ret, t_ret)[0][1]) if np.std(p_ret) > 0 and np.std(t_ret) > 0 else 0.0
    r2 = max(0.0, min(1.0, r * r))
    return beta, r2, len(p_ret)


# ── Schemas ───────────────────────────────────────────────────────────


class HedgeProxyInfo(msgspec.Struct):
    id:           int
    proxy_symbol: str
    target_root:  str
    is_active:    bool
    note:         Optional[str]
    # ETF tracking hedges (Stage 2) leave β=None → math treats as 1.0.
    # Stock-vs-index hedges (Stage 3) carry β from the regression
    # endpoint below; the derivatives page multiplies the dynamic
    # market_value / target_spot by β to get the right NIFTY-equivalent
    # for a RELIANCE → NIFTY hedge.
    beta:              Optional[float] = None
    correlation:       float           = 1.0
    regression_at:     Optional[str]   = None     # ISO-8601, NULL if never run
    # Sprint D — last regression failure reason; NULL when the most
    # recent attempt succeeded (or no attempt has run yet). UI uses
    # this to render a warning chip + flag pairs stuck on stale β.
    regression_error:  Optional[str]   = None
    created_at:        str             = ""
    updated_at:        str             = ""


class HedgeProxyCreate(msgspec.Struct):
    proxy_symbol: str
    target_root:  str
    is_active:    bool = True
    note:         Optional[str] = None
    # Sprint E (audit) — `correlation` is no longer an operator-supplied
    # input. The regression endpoint OVERWRITES it with R² on every
    # successful run, so any value set here is silently destroyed.
    # Accepted for back-compat (older clients may still send 1.0) but
    # the field is no longer surfaced on the admin create form.
    correlation:  float = 1.0


class HedgeProxyUpdate(msgspec.Struct):
    """Partial update — every field optional."""
    proxy_symbol: Optional[str]   = None
    target_root:  Optional[str]   = None
    is_active:    Optional[bool]  = None
    note:         Optional[str]   = None
    # Operator-set correlation is overwritten by the regression endpoint.
    # Kept for back-compat (old patches may send it) but cosmetic.
    correlation:  Optional[float] = None


class HedgeProxyResponse(msgspec.Struct):
    rows: list[HedgeProxyInfo]


# ── Migration + bootstrap seed ────────────────────────────────────────


# Default pairs to start with. Operator: "to start with table can have
# goldm and gold, with goldbees cross reference, similarly silverm,
# silver and silverbees." NIFTYBEES + BANKBEES included for symmetry —
# operators on index options use the same proxy-hedge flow.
_SEED_PAIRS: list[tuple[str, str, str]] = [
    ("GOLDBEES",   "GOLD",      "1 GOLDBEES unit tracks gold spot — MCX GOLD 100 g lot"),
    ("GOLDBEES",   "GOLDM",     "1 GOLDBEES unit tracks gold spot — MCX GOLDM 10 g lot"),
    ("SILVERBEES", "SILVER",    "1 SILVERBEES unit tracks silver spot"),
    ("SILVERBEES", "SILVERM",   "1 SILVERBEES unit tracks silver spot"),
    ("NIFTYBEES",  "NIFTY",     "1 NIFTYBEES NAV ≈ NIFTY / 10"),
    ("BANKBEES",   "BANKNIFTY", "1 BANKBEES NAV ≈ BANKNIFTY / 10"),
]


async def seed_hedge_proxies() -> int:
    """One-time migration + bootstrap seed.

    Migration: detect legacy Stage 2 schema (presence of the
    `conversion_kind` column) and DROP TABLE on next boot so init_db
    recreates with the simplified shape. The legacy table held ≤ 9
    seeded rows + any operator-added pairs; the seeder re-inserts the
    seeded defaults below, and operator-added rows from <24h prior
    can be re-entered through /admin/settings in seconds.

    Bootstrap: insert default pairs when the (post-migration) table is
    empty. Idempotent across restarts — non-empty tables are left
    untouched.
    """
    # ── Migration step ────────────────────────────────────────────────
    async with engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'hedge_proxies'"
        ))).all()
        cols = {r[0] for r in rows}
        if cols and "conversion_kind" in cols:
            logger.info("hedge_proxies: legacy schema detected, dropping for migration")
            await conn.execute(text("DROP TABLE IF EXISTS hedge_proxies CASCADE"))
        elif cols:
            # Additive ALTERs — non-destructive, row count intact.
            if "correlation" not in cols:
                logger.info("hedge_proxies: adding correlation column")
                await conn.execute(text(
                    "ALTER TABLE hedge_proxies ADD COLUMN IF NOT EXISTS "
                    "correlation DOUBLE PRECISION NOT NULL DEFAULT 1.0"
                ))
            if "beta" not in cols:
                logger.info("hedge_proxies: adding beta column (Stage 3)")
                await conn.execute(text(
                    "ALTER TABLE hedge_proxies ADD COLUMN IF NOT EXISTS "
                    "beta DOUBLE PRECISION"
                ))
            if "regression_at" not in cols:
                logger.info("hedge_proxies: adding regression_at column (Stage 3)")
                await conn.execute(text(
                    "ALTER TABLE hedge_proxies ADD COLUMN IF NOT EXISTS "
                    "regression_at TIMESTAMP WITH TIME ZONE"
                ))
            if "regression_error" not in cols:
                logger.info("hedge_proxies: adding regression_error column (Sprint D)")
                await conn.execute(text(
                    "ALTER TABLE hedge_proxies ADD COLUMN IF NOT EXISTS "
                    "regression_error VARCHAR(255)"
                ))
    # init_db has already created the new shape if the table is gone —
    # if migration just dropped it, SQLAlchemy's metadata.create_all
    # (run from init_db) needs to re-fire. Easiest path: re-run it here
    # against the model so the simplified table lands before the
    # seeder INSERTs.
    from backend.api.database import Base
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.tables["hedge_proxies"].create(sc, checkfirst=True))

    # ── Bootstrap seed ────────────────────────────────────────────────
    async with async_session() as sess:
        existing = (await sess.execute(select(HedgeProxy.id))).first()
        if existing:
            return 0
        for proxy_symbol, target_root, note in _SEED_PAIRS:
            sess.add(HedgeProxy(
                proxy_symbol=proxy_symbol,
                target_root=target_root,
                is_active=True,
                note=note,
            ))
        await sess.commit()
        logger.info(f"hedge_proxies: seeded {len(_SEED_PAIRS)} default pairs")
        return len(_SEED_PAIRS)


# ── Helpers ───────────────────────────────────────────────────────────


def _to_info(row: HedgeProxy) -> HedgeProxyInfo:
    return HedgeProxyInfo(
        id=row.id,
        proxy_symbol=row.proxy_symbol,
        target_root=row.target_root,
        is_active=row.is_active,
        note=row.note,
        beta=float(row.beta) if row.beta is not None else None,
        correlation=float(row.correlation if row.correlation is not None else 1.0),
        regression_at=row.regression_at.isoformat() if row.regression_at else None,
        regression_error=row.regression_error,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


# ── Routes ────────────────────────────────────────────────────────────


class HedgeProxiesController(Controller):
    path = "/api/admin/hedge-proxies"
    guards = [admin_guard]

    @get("/")
    async def list_proxies(self) -> HedgeProxyResponse:
        async with async_session() as sess:
            rows = (await sess.execute(
                select(HedgeProxy).order_by(HedgeProxy.proxy_symbol, HedgeProxy.target_root)
            )).scalars().all()
        return HedgeProxyResponse(rows=[_to_info(r) for r in rows])

    @get("/{proxy_id:int}")
    async def get_proxy(self, proxy_id: int) -> HedgeProxyInfo:
        async with async_session() as sess:
            row = await sess.get(HedgeProxy, proxy_id)
            if not row:
                raise HTTPException(status_code=404, detail="Not found.")
            return _to_info(row)

    @post("/")
    async def create_proxy(self, data: HedgeProxyCreate) -> HedgeProxyInfo:
        proxy_sym = data.proxy_symbol.strip().upper()
        target = data.target_root.strip().upper()
        if not proxy_sym or not target:
            raise HTTPException(status_code=400, detail="proxy_symbol + target_root required.")
        async with async_session() as sess:
            row = HedgeProxy(
                proxy_symbol=proxy_sym,
                target_root=target,
                is_active=data.is_active,
                note=data.note,
                correlation=float(data.correlation if data.correlation is not None else 1.0),
            )
            sess.add(row)
            try:
                await sess.commit()
            except Exception as exc:
                await sess.rollback()
                raise HTTPException(status_code=409, detail=f"Conflict: {exc}") from exc
            await sess.refresh(row)
            return _to_info(row)

    @patch("/{proxy_id:int}")
    async def update_proxy(self, proxy_id: int, data: HedgeProxyUpdate) -> HedgeProxyInfo:
        async with async_session() as sess:
            row = await sess.get(HedgeProxy, proxy_id)
            if not row:
                raise HTTPException(status_code=404, detail="Not found.")
            if data.proxy_symbol is not None:
                row.proxy_symbol = data.proxy_symbol.strip().upper()
            if data.target_root is not None:
                row.target_root = data.target_root.strip().upper()
            if data.is_active is not None:
                row.is_active = data.is_active
            if data.note is not None:
                row.note = data.note
            if data.correlation is not None:
                row.correlation = float(data.correlation)
            try:
                await sess.commit()
            except Exception as exc:
                await sess.rollback()
                raise HTTPException(status_code=409, detail=f"Conflict: {exc}") from exc
            await sess.refresh(row)
            return _to_info(row)

    @post("/{proxy_id:int}/compute")
    async def compute_regression(self, proxy_id: int) -> HedgeProxyInfo:
        """Run a 60-day daily-returns regression for this pair and
        write the resulting β + R² back to the row. Operator-triggered
        via the "Compute β" button in /admin/settings; Stage 4 will
        also run this from a periodic background task."""
        async with async_session() as sess:
            row = await sess.get(HedgeProxy, proxy_id)
            if not row:
                raise HTTPException(status_code=404, detail="Not found.")
            try:
                from backend.shared.brokers.registry import get_price_broker
                broker = get_price_broker()
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"No broker available: {exc}") from exc
            beta, r2, n = await asyncio.to_thread(
                _compute_regression, broker, row.proxy_symbol, row.target_root, 60,
            )
            if beta is None:
                # Sprint D — record the failure on the row so the UI
                # surfaces it (operator can tell "tried 3 days ago,
                # failed" apart from "computed 3 days ago OK"). Still
                # raise so the manual-trigger API caller sees the 422.
                row.regression_error = (
                    f"too few overlapping bars (n={n}, need ≥ 15)"
                )
                row.regression_at = datetime.now(timezone.utc)
                await sess.commit()
                raise HTTPException(
                    status_code=422,
                    detail=f"Regression failed — n={n} overlapping bars (need ≥ 15). "
                           "Verify both symbols exist on the broker.")
            row.beta = float(beta)
            row.correlation = float(r2 if r2 is not None else 1.0)
            row.regression_at = datetime.now(timezone.utc)
            row.regression_error = None     # success — clear stale failure marker
            await sess.commit()
            await sess.refresh(row)
            logger.info(
                f"hedge-proxy regression: {row.proxy_symbol}→{row.target_root} "
                f"β={beta:.4f} R²={r2:.3f} n={n}")
            return _to_info(row)

    @delete("/{proxy_id:int}", status_code=204)
    async def delete_proxy(self, proxy_id: int) -> None:
        async with async_session() as sess:
            row = await sess.get(HedgeProxy, proxy_id)
            if not row:
                raise HTTPException(status_code=404, detail="Not found.")
            await sess.delete(row)
            await sess.commit()
