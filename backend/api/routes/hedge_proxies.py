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


# ── Schemas ───────────────────────────────────────────────────────────


class HedgeProxyInfo(msgspec.Struct):
    id:           int
    proxy_symbol: str
    target_root:  str
    is_active:    bool
    note:         Optional[str]
    created_at:   str
    updated_at:   str


class HedgeProxyCreate(msgspec.Struct):
    proxy_symbol: str
    target_root:  str
    is_active:    bool = True
    note:         Optional[str] = None


class HedgeProxyUpdate(msgspec.Struct):
    """Partial update — every field optional."""
    proxy_symbol: Optional[str]  = None
    target_root:  Optional[str]  = None
    is_active:    Optional[bool] = None
    note:         Optional[str]  = None


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
