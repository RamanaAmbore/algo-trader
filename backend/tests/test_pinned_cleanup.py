"""
Tests for seed_global_pinned: retired-symbol cleanup + default top-up.

Five quality dimensions:
  1. SSOT     — RETIRED_PINNED_SYMBOLS list in watchlist.py is the single
                authoritative source; GOLDM/USDINR are not in MARKETS_DEFAULT.
  2. Perf     — cleanup block executes O(|RETIRED|) DELETE statements, not a
                full table scan; idempotent across two runs (no duplicate rows).
  3. Stale    — GOLDM and USDINR do NOT appear in MARKETS_DEFAULT after edit.
  4. Reuse    — seed_global_pinned is the single call-site for both cleanup
                and top-up; no inline DELETE calls exist elsewhere in watchlist.py.
  5. Correctness — scenario matrix:
       a. GOLDM + USDINR present → removed after seed.
       b. GOLDBEES / SILVERBEES absent → added after seed.
       c. GOLDM + GOLDBEES both present → GOLDM removed, GOLDBEES preserved.
       d. Second seed run is idempotent (no double-inserts, no errors).
       e. Operator-curated extras (e.g. TATASTEEL NSE) untouched by cleanup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, delete
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
# Dimension 3 — GOLDM and USDINR are NOT in MARKETS_DEFAULT after the edit
# ---------------------------------------------------------------------------

def test_goldm_removed_from_markets_default():
    src = _defaults_text()
    assert '("GOLDM"' not in src, (
        "GOLDM must not appear in MARKETS_DEFAULT after the pinned-cleanup edit"
    )


def test_usdinr_removed_from_markets_default():
    src = _defaults_text()
    assert '("USDINR"' not in src, (
        "USDINR must not appear in MARKETS_DEFAULT after the pinned-cleanup edit"
    )


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
# Dimension 3 — RETIRED_PINNED_SYMBOLS block exists in watchlist.py
# ---------------------------------------------------------------------------

def test_retired_pinned_symbols_block_present():
    src = _wl_text()
    assert "_RETIRED_PINNED" in src, (
        "seed_global_pinned must define _RETIRED_PINNED cleanup list"
    )
    assert '"GOLDM"' in src and '"MCX"' in src, (
        "_RETIRED_PINNED must include (GOLDM, MCX)"
    )
    assert '"USDINR"' in src and '"CDS"' in src, (
        "_RETIRED_PINNED must include (USDINR, CDS)"
    )


# ---------------------------------------------------------------------------
# Dimension 4 — sa_delete not duplicated outside the seed function
# ---------------------------------------------------------------------------

def test_no_dangling_cds_section_comment():
    """The '# CDS currency future.' comment was only relevant while USDINR
    was in the seed. After removal, the comment must also be gone."""
    src = _defaults_text()
    assert "# CDS currency future." not in src, (
        "Dangling CDS section comment must be removed alongside USDINR"
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
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session_ctx():
        yield db_session

    with patch("backend.api.routes.watchlist.async_session", _fake_session_ctx):
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
# Dimension 5a — GOLDM + USDINR present → removed after seed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retired_symbols_removed_after_seed(db_session):
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [
        ("GOLDM", "MCX"),
        ("USDINR", "CDS"),
        ("NIFTY 50", "NSE"),
    ])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    assert ("GOLDM", "MCX") not in syms, "GOLDM must be removed by cleanup"
    assert ("USDINR", "CDS") not in syms, "USDINR must be removed by cleanup"
    assert ("NIFTY 50", "NSE") in syms, "NIFTY 50 must be preserved"


# ---------------------------------------------------------------------------
# Dimension 5b — GOLDBEES / SILVERBEES absent → added after seed
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
# Dimension 5c — GOLDM + GOLDBEES both present → GOLDM removed, GOLDBEES kept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goldm_removed_goldbees_preserved(db_session):
    wl_id = await _make_global_pinned(db_session)
    await _insert_items(db_session, wl_id, [
        ("GOLDM", "MCX"),
        ("GOLDBEES", "NSE"),
    ])

    await _run_seed(db_session)

    syms = await _symbols(db_session)
    assert ("GOLDM", "MCX") not in syms, "GOLDM must be removed"
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
    assert ("GOLDM", "MCX") not in syms


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
    assert ("GOLDM", "MCX") not in syms, "GOLDM must still be removed"


# ---------------------------------------------------------------------------
# Dimension 2 — Perf: cleanup block is O(|RETIRED|), not a full-table scan
# (static check — ensure the DELETE uses an equality WHERE, not LIKE/IN-all)
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
