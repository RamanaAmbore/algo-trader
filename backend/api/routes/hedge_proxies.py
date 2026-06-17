"""
`/api/admin/hedge-proxies/*` — CRUD for the proxy-hedge cross-reference
table powering the /admin/derivatives Underlying picker + Legs proxy
leg.

Operator: "somewhere there should be some cross reference between root
and instrument … don't want to hard code. these tables can have
multiple columns with parameter values. the conversion can be static,
dynamic. the correlation can be 0 to 1. for goldbees, and silverbees
it is one. there could be more parameters. in future, ai can generate
this info. there should be a panel to enter in the current admin
settings pages. the code should use this table."

Bootstrap seed runs on first boot when the table is empty — inserts
~6 known ETF proxy pairs (GOLDBEES → GOLD/GOLDM/…, NIFTYBEES → NIFTY,
etc.) with `source='seeded'` so operators can distinguish bootstrap
rows from their own additions. Empty after a manual purge stays empty
— the seeder only fires on a genuinely empty table.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, delete, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import HedgeProxy
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────


class HedgeProxyInfo(msgspec.Struct):
    id:              int
    proxy_symbol:    str
    target_root:     str
    conversion_kind: str            # 'dynamic' | 'static' | 'beta'
    static_factor:   Optional[float]
    beta:            Optional[float]
    correlation:     float          # 0..1
    kind:            str            # 'units' | 'shares'
    note:            Optional[str]
    source:          str            # 'seeded' | 'operator' | 'ai'
    is_active:       bool
    created_at:      str
    updated_at:      str


class HedgeProxyCreate(msgspec.Struct):
    proxy_symbol:    str
    target_root:     str
    conversion_kind: str = "dynamic"
    static_factor:   Optional[float] = None
    beta:            Optional[float] = None
    correlation:     float = 1.0
    kind:            str = "units"
    note:            Optional[str] = None
    is_active:       bool = True


class HedgeProxyUpdate(msgspec.Struct):
    """Partial update. Every field optional — operator can edit a
    single value (correlation, conversion_kind, …) without re-typing
    the rest."""
    proxy_symbol:    Optional[str]    = None
    target_root:     Optional[str]    = None
    conversion_kind: Optional[str]    = None
    static_factor:   Optional[float]  = None
    beta:            Optional[float]  = None
    correlation:     Optional[float]  = None
    kind:            Optional[str]    = None
    note:            Optional[str]    = None
    is_active:       Optional[bool]   = None


class HedgeProxyResponse(msgspec.Struct):
    rows: list[HedgeProxyInfo]


# ── Bootstrap seed ────────────────────────────────────────────────────


# The default pairs we used to ship in `$lib/data/hedgeProxies.js`
# (Stage 1). Inserted on first boot when the table is empty so the
# frontend gets the same defaults out of the box, but the operator
# can edit / delete / extend them via /admin/settings.
_SEED_PAIRS: list[dict] = [
    dict(proxy_symbol="GOLDBEES",   target_root="GOLD",       kind="units",
         note="1 GOLDBEES unit ≈ 0.01 g gold (MCX GOLD 100 g lot)"),
    dict(proxy_symbol="GOLDBEES",   target_root="GOLDM",      kind="units",
         note="1 GOLDBEES unit ≈ 0.01 g gold (MCX GOLDM 10 g lot)"),
    dict(proxy_symbol="GOLDBEES",   target_root="GOLDPETAL",  kind="units",
         note="1 GOLDBEES unit ≈ 0.01 g gold (MCX GOLDPETAL 1 g lot)"),
    dict(proxy_symbol="GOLDBEES",   target_root="GOLDGUINEA", kind="units",
         note="1 GOLDBEES unit ≈ 0.01 g gold (MCX GOLDGUINEA 8 g lot)"),
    dict(proxy_symbol="SILVERBEES", target_root="SILVER",     kind="units",
         note="1 SILVERBEES unit ≈ 0.01 g silver-equivalent"),
    dict(proxy_symbol="SILVERBEES", target_root="SILVERM",    kind="units",
         note="1 SILVERBEES unit ≈ 0.01 g silver-equivalent"),
    dict(proxy_symbol="SILVERBEES", target_root="SILVERMIC",  kind="units",
         note="1 SILVERBEES unit ≈ 0.01 g silver-equivalent"),
    dict(proxy_symbol="NIFTYBEES",  target_root="NIFTY",      kind="shares",
         note="1 NIFTYBEES NAV ≈ NIFTY index / 10"),
    dict(proxy_symbol="BANKBEES",   target_root="BANKNIFTY",  kind="shares",
         note="1 BANKBEES NAV ≈ BANKNIFTY index / 10"),
]


async def seed_hedge_proxies() -> int:
    """Insert default proxy pairs when the table is empty. Returns the
    count of rows inserted (0 when table already has entries, so the
    seeder is idempotent across boots).

    Called from `on_startup` after init_db — same pattern as
    seed_settings / seed_agents.
    """
    async with async_session() as sess:
        existing = (await sess.execute(select(HedgeProxy.id))).first()
        if existing:
            return 0
        for row in _SEED_PAIRS:
            sess.add(HedgeProxy(
                proxy_symbol=row["proxy_symbol"],
                target_root=row["target_root"],
                conversion_kind="dynamic",
                correlation=1.0,
                kind=row["kind"],
                note=row["note"],
                source="seeded",
                is_active=True,
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
        conversion_kind=row.conversion_kind,
        static_factor=row.static_factor,
        beta=row.beta,
        correlation=row.correlation,
        kind=row.kind,
        note=row.note,
        source=row.source,
        is_active=row.is_active,
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
        if data.conversion_kind not in ("dynamic", "static", "beta"):
            raise HTTPException(status_code=400, detail="conversion_kind must be dynamic/static/beta.")
        if not (0.0 <= data.correlation <= 1.0):
            raise HTTPException(status_code=400, detail="correlation must be in [0, 1].")
        async with async_session() as sess:
            row = HedgeProxy(
                proxy_symbol=proxy_sym,
                target_root=target,
                conversion_kind=data.conversion_kind,
                static_factor=data.static_factor,
                beta=data.beta,
                correlation=data.correlation,
                kind=data.kind,
                note=data.note,
                source="operator",
                is_active=data.is_active,
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
            if data.conversion_kind is not None:
                if data.conversion_kind not in ("dynamic", "static", "beta"):
                    raise HTTPException(status_code=400, detail="conversion_kind must be dynamic/static/beta.")
                row.conversion_kind = data.conversion_kind
            if data.static_factor is not None:
                row.static_factor = data.static_factor
            if data.beta is not None:
                row.beta = data.beta
            if data.correlation is not None:
                if not (0.0 <= data.correlation <= 1.0):
                    raise HTTPException(status_code=400, detail="correlation must be in [0, 1].")
                row.correlation = data.correlation
            if data.kind is not None:
                row.kind = data.kind
            if data.note is not None:
                row.note = data.note
            if data.is_active is not None:
                row.is_active = data.is_active
            try:
                await sess.commit()
            except Exception as exc:
                await sess.rollback()
                raise HTTPException(status_code=409, detail=f"Conflict: {exc}") from exc
            await sess.refresh(row)
            return _to_info(row)

    @delete("/{proxy_id:int}", status_code=204)
    async def delete_proxy(self, proxy_id: int) -> None:
        async with async_session() as sess:
            row = await sess.get(HedgeProxy, proxy_id)
            if not row:
                raise HTTPException(status_code=404, detail="Not found.")
            await sess.delete(row)
            await sess.commit()
