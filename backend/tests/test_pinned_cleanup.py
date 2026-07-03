"""
Tests for seed_global_pinned: retired-symbol cleanup + default top-up.

Five quality dimensions:
  1. SSOT     — MARKETS_DEFAULT contains exactly the canonical 16 symbols.
                NATURALGAS is NOT seeded (still excluded). Migration marker
                keys are the SSOT for cleanup waves.
  2. Perf     — each wave is O(|wave|) targeted DELETEs (filter by symbol+exch),
                not a full-table scan or truncation.
  3. Stale    — NATURALGAS absent from MARKETS_DEFAULT (only non-restored MCX root).
                SILVER/SILVERM/GOLD/GOLDM/CRUDEOIL/COPPER/USDINR present as
                first-class bare roots.
  4. Reuse    — seed_global_pinned is the single call-site for cleanup + top-up.
  5. Correctness — scenario matrix:
       a. GOLDM + USDINR present AND migration markers not set → wave 1 deletes
          then top-up re-adds them (net: still present, markers recorded).
       b. MCX bare roots present AND wave-2 marker not set → wave 2 deletes
          then top-up re-adds COPPER/CRUDEOIL/SILVERM; NATURALGAS removed and
          NOT re-added (not in MARKETS_DEFAULT).
       c. GOLDBEES / SILVERBEES absent → added after seed (top-up).
       d. Second seed run is idempotent (no double-inserts, no errors).
       e. Operator-curated extras (e.g. TATASTEEL NSE) untouched by cleanup.
       f. Migration is one-shot: marker present → DELETE not re-executed (so
          operator-re-added symbols survive).
       g. Operator re-adds NATURALGAS after wave-2 cleanup → preserved on next boot.
       h. Empty global Pinned → 16-item Pinned after seed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Source paths for static (SSOT / Stale / Reuse) checks
# ---------------------------------------------------------------------------

_DEFAULTS_SRC = Path(__file__).parent.parent / "api" / "algo" / "watchlist_defaults.py"
_WL_SRC       = Path(__file__).parent.parent / "api" / "routes" / "watchlist.py"


def _defaults_text() -> str:
    return _DEFAULTS_SRC.read_text(encoding="utf-8")


def _wl_text() -> str:
    return _WL_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dimension 3 — NATURALGAS still NOT in MARKETS_DEFAULT (only excluded root)
# ---------------------------------------------------------------------------

def test_naturalgas_removed_from_markets_default():
    src = _defaults_text()
    assert '("NATURALGAS"' not in src, (
        "NATURALGAS must not appear in MARKETS_DEFAULT — still excluded"
    )


# ---------------------------------------------------------------------------
# Dimension 3 — 7 restored bare roots ARE in MARKETS_DEFAULT
# ---------------------------------------------------------------------------

def test_silver_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("SILVER", "MCX") in MARKETS_DEFAULT, "SILVER MCX must be present in MARKETS_DEFAULT"


def test_silverm_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("SILVERM", "MCX") in MARKETS_DEFAULT, "SILVERM MCX must be present in MARKETS_DEFAULT"


def test_gold_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("GOLD", "MCX") in MARKETS_DEFAULT, "GOLD MCX must be present in MARKETS_DEFAULT"


def test_goldm_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("GOLDM", "MCX") in MARKETS_DEFAULT, "GOLDM MCX must be present in MARKETS_DEFAULT"


def test_crudeoil_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("CRUDEOIL", "MCX") in MARKETS_DEFAULT, "CRUDEOIL MCX must be present in MARKETS_DEFAULT"


def test_copper_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("COPPER", "MCX") in MARKETS_DEFAULT, "COPPER MCX must be present in MARKETS_DEFAULT"


def test_usdinr_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("USDINR", "CDS") in MARKETS_DEFAULT, "USDINR CDS must be present in MARKETS_DEFAULT"


# ---------------------------------------------------------------------------
# Dimension 1 — GOLDBEES and SILVERBEES ARE in MARKETS_DEFAULT (SSOT)
# ---------------------------------------------------------------------------

def test_goldbees_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("GOLDBEES", "NSE") in MARKETS_DEFAULT, (
        "GOLDBEES NSE must be present in MARKETS_DEFAULT"
    )


def test_silverbees_in_markets_default():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert ("SILVERBEES", "NSE") in MARKETS_DEFAULT, (
        "SILVERBEES NSE must be present in MARKETS_DEFAULT"
    )


# ---------------------------------------------------------------------------
# Dimension 1 — MARKETS_DEFAULT has exactly 16 entries
# ---------------------------------------------------------------------------

def test_markets_default_count():
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    assert len(MARKETS_DEFAULT) == 16, (
        f"MARKETS_DEFAULT must have 16 entries; got {len(MARKETS_DEFAULT)}"
    )


# ---------------------------------------------------------------------------
# Dimension 3 — migration marker keys exist in watchlist.py (SSOT check)
#               Markers stay recorded as historical audit — must NOT be removed
# ---------------------------------------------------------------------------

def test_wave1_migration_key_present():
    src = _wl_text()
    assert "migrations.pinned_remove_goldm_usdinr_v1" in src, (
        "Wave-1 migration key must be present in seed_global_pinned"
    )
    assert '"GOLDM"' in src and '"MCX"' in src, (
        "Wave 1 cleanup must reference (GOLDM, MCX)"
    )
    assert '"USDINR"' in src and '"CDS"' in src, (
        "Wave 1 cleanup must reference (USDINR, CDS)"
    )


def test_wave2_migration_key_present():
    src = _wl_text()
    assert "migrations.pinned_remove_mcx_futures_v1" in src, (
        "Wave-2 migration key must be present in seed_global_pinned"
    )
    assert '"COPPER"' in src, "Wave 2 cleanup must reference COPPER"
    assert '"CRUDEOIL"' in src, "Wave 2 cleanup must reference CRUDEOIL"
    assert '"NATURALGAS"' in src, "Wave 2 cleanup must reference NATURALGAS"
    assert '"SILVERM"' in src, "Wave 2 cleanup must reference SILVERM"


# ---------------------------------------------------------------------------
# Dimension 4 — sa_delete not duplicated outside the seed function
# ---------------------------------------------------------------------------

def test_cleanup_uses_targeted_deletes():
    """Each retired symbol is deleted with a specific tradingsymbol + exchange
    filter, not a bulk DELETE-all or table truncation."""
    src = _wl_text()
    # Both conditions must be in the WHERE clause path
    assert "WatchlistItem.tradingsymbol == _sym" in src, (
        "Cleanup DELETE must filter by tradingsymbol"
    )
    assert "WatchlistItem.exchange == _exch" in src, (
        "Cleanup DELETE must filter by exchange"
    )


# ---------------------------------------------------------------------------
# Lightweight in-memory SQLite fixtures for behavioural tests
# ---------------------------------------------------------------------------

class _Base(DeclarativeBase):
    pass


class _Watchlist(_Base):
    __tablename__ = "watchlists"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_wl_user_name"),)
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, nullable=True)
    name       = Column(String(64), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    is_default = Column(Boolean, nullable=False, default=False)
    is_pinned  = Column(Boolean, nullable=False, default=False)
    is_global  = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))


class _WatchlistItem(_Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint(
        "watchlist_id", "exchange", "tradingsymbol",
        name="uq_wl_item_unique",
    ),)
    id            = Column(Integer, primary_key=True, autoincrement=True)
    watchlist_id  = Column(Integer, ForeignKey("watchlists.id", ondelete="CASCADE"),
                           nullable=False)
    tradingsymbol = Column(String(64), nullable=False)
    exchange      = Column(String(8),  nullable=False)
    alias         = Column(String(64), nullable=True)
    sort_order    = Column(Integer, nullable=False, default=0)
    added_at      = Column(DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))


class _Setting(_Base):
    """Mirrors the production Setting model for migration-marker reads/writes."""
    __tablename__ = "settings"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    category      = Column(String(32), nullable=False)
    key           = Column(String(128), unique=True, nullable=False)
    value_type    = Column(String(16), nullable=False)
    value         = Column(Text, nullable=False, default="")
    default_value = Column(Text, nullable=False, default="")
    description   = Column(Text, nullable=False, default="")
    updated_at    = Column(DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# Helper: run seed_global_pinned against the in-memory DB
# ---------------------------------------------------------------------------

async def _run_seed(db_session: AsyncSession) -> None:
    """
    Call seed_global_pinned with its DB wired to our in-memory SQLite
    session factory so we can assert against the live rows.
    The production Setting model is patched to our _Setting stub so the
    migration-marker reads/writes land in the same in-memory DB.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session_ctx():
        yield db_session

    with (
        patch("backend.api.routes.watchlist.async_session", _fake_session_ctx),
        patch("backend.api.models.Setting", _Setting),
    ):
        from backend.api.routes.watchlist import seed_global_pinned
        await seed_global_pinned()


async def _symbols(db_session: AsyncSession) -> set[tuple[str, str]]:
    from sqlalchemy import select
    rows = (await db_session.execute(select(_WatchlistItem))).scalars().all()
    return {(r.tradingsymbol, r.exchange) for r in rows}


async def _insert_items(db_session: AsyncSession, wl_id: int, items: list[tuple[str, str]]) -> None:
    now = datetime.now(timezone.utc)
    for i, (sym, exch) in enumerate(items):
        db_session.add(_WatchlistItem(
            watchlist_id=wl_id, tradingsymbol=sym, exchange=exch,
            sort_order=i, added_at=now,
        ))
    await db_session.flush()


async def _make_global_pinned(db_session: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    wl = _Watchlist(
        user_id=None, name="Pinned", sort_order=0,
        is_default=True, is_pinned=True, is_global=True,
        created_at=now, updated_at=now,
    )
    db_session.add(wl)
    await db_session.flush()
    return wl.id


# ---------------------------------------------------------------------------
# Dimension 5a — GOLDM + USDINR present, wave-1 not run → wave fires, but
# top-up immediately re-adds them (both are back in MARKETS_DEFAULT)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retired_symbols_restored_after_seed(db_session):
    """Wave-1 cleanup fires (removes GOLDM/USDINR), then top-up re-adds
    them because they are now in MARKETS_DEFAULT. Net: they survive."""
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [
        ("GOLDM", "MCX"),
        ("USDINR", "CDS"),
        ("NIFTY 50", "NSE"),
    ])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    # Restored in MARKETS_DEFAULT → top-up re-adds after wave-1 delete
    assert ("GOLDM", "MCX") in syms, "GOLDM must be present (restored to MARKETS_DEFAULT)"
    assert ("USDINR", "CDS") in syms, "USDINR must be present (restored to MARKETS_DEFAULT)"
    assert ("NIFTY 50", "NSE") in syms, "NIFTY 50 must be preserved"


# ---------------------------------------------------------------------------
# Dimension 5b (wave 2) — MCX bare roots removed by wave, then re-added.
# NATURALGAS is NOT in MARKETS_DEFAULT so it stays removed.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcx_roots_restored_naturalgas_removed(db_session):
    """Wave-2 fires and deletes all MCX roots including NATURALGAS.
    Top-up re-adds COPPER/CRUDEOIL/SILVERM (back in MARKETS_DEFAULT)
    but NATURALGAS stays removed (still excluded)."""
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [
        ("COPPER",      "MCX"),
        ("CRUDEOIL",    "MCX"),
        ("NATURALGAS",  "MCX"),
        ("SILVERM",     "MCX"),
        ("NIFTY 50",    "NSE"),
    ])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    # Restored symbols come back via top-up
    for sym in ("COPPER", "CRUDEOIL", "SILVERM"):
        assert (sym, "MCX") in syms, f"{sym} MCX must be present (restored to MARKETS_DEFAULT)"
    # NATURALGAS remains excluded
    assert ("NATURALGAS", "MCX") not in syms, "NATURALGAS MCX must remain removed"
    assert ("NIFTY 50", "NSE") in syms, "NIFTY 50 must be preserved"


# ---------------------------------------------------------------------------
# Dimension 5c — GOLDBEES / SILVERBEES absent → added after seed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goldbees_silverbees_added_when_missing(db_session):
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [("NIFTY 50", "NSE")])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    assert ("GOLDBEES", "NSE") in syms, "GOLDBEES must be top-upped by seed"
    assert ("SILVERBEES", "NSE") in syms, "SILVERBEES must be top-upped by seed"


# ---------------------------------------------------------------------------
# Dimension 5c (ext) — GOLDM and GOLDBEES both present: both survive
# GOLDM is now first-class (in MARKETS_DEFAULT) so the wave-1 delete fires
# then top-up re-adds it. GOLDBEES is unaffected throughout.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goldm_and_goldbees_both_preserved(db_session):
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [
        ("GOLDM", "MCX"),
        ("GOLDBEES", "NSE"),
    ])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    assert ("GOLDM", "MCX") in syms, "GOLDM must be present (restored)"
    assert ("GOLDBEES", "NSE") in syms, "GOLDBEES must survive cleanup"


# ---------------------------------------------------------------------------
# Dimension 5d — Second run is idempotent (no double-inserts, no errors)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seed_is_idempotent(db_session):
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [("GOLDM", "MCX")])

    await _run_seed(db_session)
    await _run_seed(db_session)  # second run

    from sqlalchemy import select, func
    count = (await db_session.execute(
        select(func.count()).select_from(_WatchlistItem).where(
            _WatchlistItem.watchlist_id == wl_id
        )
    )).scalar()

    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    # Expect exactly len(MARKETS_DEFAULT) items — no duplicates
    assert count == len(MARKETS_DEFAULT), (
        f"Expected {len(MARKETS_DEFAULT)} items after idempotent seed, got {count}"
    )

    syms = await _symbols(db_session)
    assert ("GOLDM", "MCX") in syms, "GOLDM must be present after idempotent seed"


# ---------------------------------------------------------------------------
# Dimension 5e — Operator-curated extras are NOT removed by cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_operator_extras_untouched(db_session):
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [
        ("TATASTEEL", "NSE"),
        ("GOLDM", "MCX"),
    ])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    assert ("TATASTEEL", "NSE") in syms, "Operator-curated TATASTEEL must not be removed"
    assert ("GOLDM", "MCX") in syms, "GOLDM must be present (restored)"


# ---------------------------------------------------------------------------
# Dimension 5f — Migration is one-shot: marker present → DELETE not re-run
# (operator re-added symbol preserved when migration marker already set)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wave2_migration_is_one_shot(db_session):
    """If wave-2 marker is already set, a subsequent seed does NOT delete
    CRUDEOIL even if it exists in the pinned list (operator re-added it)."""
    from sqlalchemy import select as sa_select

    wl_id = await _make_global_pinned(db_session)

    # Pre-set wave-2 marker → simulate "migration already ran"
    now_ts = datetime.now(timezone.utc)
    db_session.add(_Setting(
        category="migrations",
        key="migrations.pinned_remove_mcx_futures_v1",
        value_type="string",
        value="1",
        default_value="0",
        description="pre-seeded for test",
        updated_at=now_ts,
    ))
    await db_session.flush()

    # Also pre-set wave-1 marker so it doesn't interfere
    db_session.add(_Setting(
        category="migrations",
        key="migrations.pinned_remove_goldm_usdinr_v1",
        value_type="string",
        value="1",
        default_value="0",
        description="pre-seeded for test",
        updated_at=now_ts,
    ))
    await db_session.flush()

    # Operator manually re-added CRUDEOIL after a previous cleanup
    await _insert_items(db_session, wl_id, [("CRUDEOIL", "MCX")])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    assert ("CRUDEOIL", "MCX") in syms, (
        "Operator re-added CRUDEOIL must survive seed when wave-2 marker is already set"
    )


# ---------------------------------------------------------------------------
# Dimension 5g — Operator re-adds NATURALGAS after cleanup → preserved on
# next boot (NATURALGAS is NOT in MARKETS_DEFAULT so stays once marker set)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_operator_readd_after_cleanup_preserved(db_session):
    """Full lifecycle: seed removes NATURALGAS (wave 2 fires + not in
    MARKETS_DEFAULT so top-up doesn't re-add), operator re-adds it, next
    boot (marker set) leaves it in place."""
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [
        ("NATURALGAS", "MCX"),
        ("NIFTY 50",   "NSE"),
    ])

    # First boot — wave 2 fires, removes NATURALGAS; top-up does NOT re-add it
    await _run_seed(db_session)
    syms_after_first = await _symbols(db_session)
    assert ("NATURALGAS", "MCX") not in syms_after_first, (
        "NATURALGAS must be removed on first boot"
    )

    # Operator manually re-adds NATURALGAS
    await _insert_items(db_session, wl_id, [("NATURALGAS", "MCX")])

    # Second boot — marker already set, NATURALGAS survives
    await _run_seed(db_session)
    syms_after_second = await _symbols(db_session)
    assert ("NATURALGAS", "MCX") in syms_after_second, (
        "Operator re-added NATURALGAS must survive second boot (marker already set)"
    )


# ---------------------------------------------------------------------------
# Dimension 5h — Empty global Pinned → exactly 16 items after seed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_pinned_seeds_to_16_items(db_session):
    """Fresh DB with no items: seed must produce exactly 16 canonical entries."""
    await _make_global_pinned(db_session)

    await _run_seed(db_session)

    from sqlalchemy import select, func
    from backend.api.algo.watchlist_defaults import MARKETS_DEFAULT
    count = (await db_session.execute(
        select(func.count()).select_from(_WatchlistItem)
    )).scalar()
    assert count == 16, f"Expected 16 items from empty seed, got {count}"
    assert len(MARKETS_DEFAULT) == 16
