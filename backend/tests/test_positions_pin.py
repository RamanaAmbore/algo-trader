"""Tests for the operator-driven manual position-pin feature.

Covers:
  1. _apply_pin_overlay() — the overlay helper that sets `pin_group_key`
     from active `position_pairs` DB rows, independent of
     _auto_pair_positions()'s ephemeral lot-waterfall (pair_group_key/
     is_orphan/paired_qty/orphan_qty).
  2. POST /api/positions/pin (pin_positions handler, called directly —
     bypassing the Litestar ASGI/guard layer per this repo's existing
     `Controller.method.fn(instance, ...)` convention — see
     test_agent_template_unification.py).
  3. DELETE /api/positions/pin/{id} (unpin_positions handler).
  4. Confirms pair_group_key/is_orphan (the existing auto-pairing fields)
     are completely unaffected by pin presence/absence.
  5. Confirms both routes are admin_guard-protected (source inspection).

DB isolation: a dedicated in-memory SQLite engine + a minimal
`_PositionPair` shim monkeypatched over `backend.api.models.PositionPair`
and `backend.api.database.async_session` — mirrors the pattern used by
test_agent_template_unification.py / test_orders_drafts.py. The route
module imports `async_session`/`PositionPair` freshly inside each
function body at call time, so patching the module attributes is
sufficient (no need to patch positions.py's own namespace).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from litestar.exceptions import HTTPException

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def pin_env():
    """In-memory SQLite `position_pairs` table + PositionPair/async_session
    monkeypatches. Yields (session_factory, PositionPairShim)."""
    from sqlalchemy import Column, Integer, String, Boolean, DateTime
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _PositionPair(_Base):
        __tablename__ = "position_pairs"
        id         = Column(Integer, primary_key=True, autoincrement=True)
        account    = Column(String(32), nullable=False)
        symbol_a   = Column(String(64), nullable=False)
        symbol_b   = Column(String(64), nullable=False)
        created_by = Column(String(64), nullable=False)
        created_at = Column(DateTime, nullable=False,
                             default=lambda: datetime.now(timezone.utc))
        active     = Column(Boolean, nullable=False, default=True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    import backend.api.database as _database

    patches = [
        patch.object(_models, "PositionPair", _PositionPair),
        patch.object(_database, "async_session", factory),
    ]
    for p in patches:
        p.start()

    try:
        yield factory, _PositionPair
    finally:
        for p in patches:
            p.stop()
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


def _fake_request(sub: str = "op1"):
    req = MagicMock()
    req.state.token_payload = {"sub": sub, "role": "admin"}
    return req


def _make_row(account, tradingsymbol, quantity=1, **kwargs):
    from backend.api.schemas import PositionRow
    defaults = dict(
        account=account, tradingsymbol=tradingsymbol, exchange="NFO",
        product="MIS", quantity=quantity, average_price=100.0,
        pnl=0.0, last_price=100.0, unrealised=0.0, realised=0.0,
    )
    return PositionRow(**{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# 1 — _apply_pin_overlay()
# ---------------------------------------------------------------------------

async def test_apply_pin_overlay_empty_rows_noop():
    from backend.api.routes.positions import _apply_pin_overlay
    assert await _apply_pin_overlay([]) == []


async def test_apply_pin_overlay_sets_matching_key_on_both_rows(pin_env):
    factory, _PositionPair = pin_env
    from backend.api.routes.positions import _apply_pin_overlay

    async with factory() as s:
        pair = _PositionPair(
            account="ZG0790", symbol_a="BANKNIFTY25APRFUT",
            symbol_b="NIFTY25APRFUT", created_by="op1", active=True,
        )
        s.add(pair)
        await s.commit()
        pair_id = pair.id

    rows = [
        _make_row("ZG0790", "NIFTY25APRFUT"),
        _make_row("ZG0790", "BANKNIFTY25APRFUT"),
        _make_row("ZG0790", "INFY"),  # unrelated — must stay unpinned
    ]
    out = await _apply_pin_overlay(rows)

    expected_key = f"PIN{pair_id}"
    by_sym = {r.tradingsymbol: r for r in out}
    assert by_sym["NIFTY25APRFUT"].pin_group_key == expected_key
    assert by_sym["BANKNIFTY25APRFUT"].pin_group_key == expected_key
    assert by_sym["INFY"].pin_group_key is None


async def test_apply_pin_overlay_inactive_pair_not_applied(pin_env):
    factory, _PositionPair = pin_env
    from backend.api.routes.positions import _apply_pin_overlay

    async with factory() as s:
        s.add(_PositionPair(
            account="ZG0790", symbol_a="BANKNIFTY25APRFUT",
            symbol_b="NIFTY25APRFUT", created_by="op1", active=False,
        ))
        await s.commit()

    rows = [
        _make_row("ZG0790", "NIFTY25APRFUT"),
        _make_row("ZG0790", "BANKNIFTY25APRFUT"),
    ]
    out = await _apply_pin_overlay(rows)
    assert all(r.pin_group_key is None for r in out)


async def test_apply_pin_overlay_different_account_not_pinned(pin_env):
    factory, _PositionPair = pin_env
    from backend.api.routes.positions import _apply_pin_overlay

    async with factory() as s:
        s.add(_PositionPair(
            account="ZG0790", symbol_a="BANKNIFTY25APRFUT",
            symbol_b="NIFTY25APRFUT", created_by="op1", active=True,
        ))
        await s.commit()

    rows = [_make_row("ZJ6294", "NIFTY25APRFUT")]
    out = await _apply_pin_overlay(rows)
    assert out[0].pin_group_key is None


async def test_apply_pin_overlay_db_failure_degrades_to_noop(pin_env, monkeypatch):
    from backend.api.routes.positions import _apply_pin_overlay
    import backend.api.database as _database

    def _boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(_database, "async_session", _boom)

    rows = [_make_row("ZG0790", "NIFTY25APRFUT")]
    out = await _apply_pin_overlay(rows)
    assert out[0].pin_group_key is None


async def test_apply_pin_overlay_tie_break_lowest_id_wins(pin_env):
    """A symbol pinned in two active pairs — lowest pin id wins."""
    factory, _PositionPair = pin_env
    from backend.api.routes.positions import _apply_pin_overlay

    async with factory() as s:
        p1 = _PositionPair(account="ZG0790", symbol_a="BANKNIFTY25APRFUT",
                            symbol_b="NIFTY25APRFUT", created_by="op1", active=True)
        p2 = _PositionPair(account="ZG0790", symbol_a="NIFTY25APRFUT",
                            symbol_b="INFY", created_by="op1", active=True)
        s.add_all([p1, p2])
        await s.commit()
        lowest_id = min(p1.id, p2.id)

    rows = [_make_row("ZG0790", "NIFTY25APRFUT")]
    out = await _apply_pin_overlay(rows)
    assert out[0].pin_group_key == f"PIN{lowest_id}"


# ---------------------------------------------------------------------------
# 2/4 — pair_group_key / is_orphan untouched by pin presence
# ---------------------------------------------------------------------------

async def test_pin_overlay_does_not_affect_auto_pair_fields(pin_env):
    """Same auto-pair outcome (pair_group_key/is_orphan/paired_qty/
    orphan_qty) with or without an active pin — the two mechanisms are
    orthogonal."""
    from backend.api.routes.positions import _auto_pair_positions, _apply_pin_overlay

    def _build():
        return [
            _make_row("ZG0790", "NIFTY26AUG24500CE", quantity=1),
            _make_row("ZG0790", "NIFTY26AUG24500CE", quantity=-1),
        ]

    # Without any pin.
    rows_no_pin = _auto_pair_positions(_build())

    # With an active pin covering the same two rows.
    factory, _PositionPair = pin_env
    async with factory() as s:
        s.add(_PositionPair(
            account="ZG0790", symbol_a="NIFTY26AUG24500CE",
            symbol_b="NIFTY26AUG24500CE_OTHER", created_by="op1", active=True,
        ))
        await s.commit()

    rows_with_pin = await _apply_pin_overlay(_auto_pair_positions(_build()))

    for a, b in zip(rows_no_pin, rows_with_pin):
        assert a.pair_group_key == b.pair_group_key
        assert a.is_orphan == b.is_orphan
        assert a.paired_qty == b.paired_qty
        assert a.orphan_qty == b.orphan_qty

    # The pin itself did take effect on the matching row (sanity check that
    # the comparison above isn't vacuous because nothing got pinned).
    pinned = next(r for r in rows_with_pin if r.tradingsymbol == "NIFTY26AUG24500CE")
    assert pinned.pin_group_key is not None


# ---------------------------------------------------------------------------
# 3 — POST /api/positions/pin (handler called directly)
# ---------------------------------------------------------------------------

def _controller():
    from backend.api.routes.positions import PositionsController
    return PositionsController.__new__(PositionsController)


async def test_pin_positions_happy_path_sets_overlay_on_get(pin_env, monkeypatch):
    from backend.api.routes.positions import (
        PositionsController, PinPositionsRequest, _apply_pin_overlay,
    )
    import backend.api.routes.positions as _pos_mod

    open_rows_resp = MagicMock()
    open_rows_resp.rows = [
        _make_row("ZG0790", "NIFTY25APRFUT"),
        _make_row("ZG0790", "BANKNIFTY25APRFUT"),
    ]

    async def _fake_resolve(*a, **kw):
        return open_rows_resp

    monkeypatch.setattr(_pos_mod, "_resolve_positions_source", _fake_resolve)

    data = PinPositionsRequest(
        account="ZG0790", symbol_a="NIFTY25APRFUT", symbol_b="BANKNIFTY25APRFUT",
    )
    req = _fake_request("op1")
    result = await PositionsController.pin_positions.fn(_controller(), data, req)

    # Normalized lexicographically (BANKNIFTY... < NIFTY...).
    assert result.symbol_a == "BANKNIFTY25APRFUT"
    assert result.symbol_b == "NIFTY25APRFUT"
    assert result.account == "ZG0790"
    assert result.active is True
    assert result.created_by == "op1"

    # GET-equivalent: overlay now applies the pin to matching rows.
    out = await _apply_pin_overlay(list(open_rows_resp.rows))
    assert out[0].pin_group_key == f"PIN{result.id}"
    assert out[1].pin_group_key == f"PIN{result.id}"


async def test_pin_positions_duplicate_pair_no_second_active_row(pin_env, monkeypatch):
    from backend.api.routes.positions import PositionsController, PinPositionsRequest
    import backend.api.routes.positions as _pos_mod

    open_rows_resp = MagicMock()
    open_rows_resp.rows = [
        _make_row("ZG0790", "NIFTY25APRFUT"),
        _make_row("ZG0790", "BANKNIFTY25APRFUT"),
    ]

    async def _fake_resolve(*a, **kw):
        return open_rows_resp

    monkeypatch.setattr(_pos_mod, "_resolve_positions_source", _fake_resolve)

    req = _fake_request("op1")
    first = await PositionsController.pin_positions.fn(
        _controller(), PinPositionsRequest(
            account="ZG0790", symbol_a="NIFTY25APRFUT", symbol_b="BANKNIFTY25APRFUT",
        ), req,
    )
    # Second call with the symbols in the OPPOSITE order — must normalize
    # to the same unordered pair and not create a second row.
    second = await PositionsController.pin_positions.fn(
        _controller(), PinPositionsRequest(
            account="ZG0790", symbol_a="BANKNIFTY25APRFUT", symbol_b="NIFTY25APRFUT",
        ), req,
    )
    assert first.id == second.id

    factory, _PositionPair = pin_env
    from sqlalchemy import select
    async with factory() as s:
        rows = (await s.execute(
            select(_PositionPair).where(_PositionPair.account == "ZG0790")
        )).scalars().all()
    active_rows = [r for r in rows if r.active]
    assert len(active_rows) == 1, "exactly one active row for the unordered pair"


async def test_pin_positions_phantom_symbol_400(pin_env, monkeypatch):
    from backend.api.routes.positions import PositionsController, PinPositionsRequest
    import backend.api.routes.positions as _pos_mod

    open_rows_resp = MagicMock()
    open_rows_resp.rows = [_make_row("ZG0790", "NIFTY25APRFUT")]  # only one real open pos

    async def _fake_resolve(*a, **kw):
        return open_rows_resp

    monkeypatch.setattr(_pos_mod, "_resolve_positions_source", _fake_resolve)

    data = PinPositionsRequest(
        account="ZG0790", symbol_a="NIFTY25APRFUT", symbol_b="GHOSTSYMBOL",
    )
    with pytest.raises(HTTPException) as exc_info:
        await PositionsController.pin_positions.fn(_controller(), data, _fake_request())
    assert exc_info.value.status_code == 400


async def test_pin_positions_same_symbol_400(pin_env, monkeypatch):
    from backend.api.routes.positions import PositionsController, PinPositionsRequest

    data = PinPositionsRequest(
        account="ZG0790", symbol_a="NIFTY25APRFUT", symbol_b="NIFTY25APRFUT",
    )
    with pytest.raises(HTTPException) as exc_info:
        await PositionsController.pin_positions.fn(_controller(), data, _fake_request())
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# 4 — DELETE /api/positions/pin/{id}
# ---------------------------------------------------------------------------

async def test_unpin_clears_overlay_on_next_fetch(pin_env, monkeypatch):
    from backend.api.routes.positions import (
        PositionsController, PinPositionsRequest, _apply_pin_overlay,
    )
    import backend.api.routes.positions as _pos_mod

    open_rows_resp = MagicMock()
    open_rows_resp.rows = [
        _make_row("ZG0790", "NIFTY25APRFUT"),
        _make_row("ZG0790", "BANKNIFTY25APRFUT"),
    ]

    async def _fake_resolve(*a, **kw):
        return open_rows_resp

    monkeypatch.setattr(_pos_mod, "_resolve_positions_source", _fake_resolve)

    created = await PositionsController.pin_positions.fn(
        _controller(),
        PinPositionsRequest(
            account="ZG0790", symbol_a="NIFTY25APRFUT", symbol_b="BANKNIFTY25APRFUT",
        ),
        _fake_request(),
    )
    pinned_before = await _apply_pin_overlay(list(open_rows_resp.rows))
    assert all(r.pin_group_key is not None for r in pinned_before)

    delete_result = await PositionsController.unpin_positions.fn(
        _controller(), created.id,
    )
    assert delete_result == {"ok": True, "id": created.id}

    pinned_after = await _apply_pin_overlay(list(open_rows_resp.rows))
    assert all(r.pin_group_key is None for r in pinned_after)


async def test_unpin_missing_id_404(pin_env):
    from backend.api.routes.positions import PositionsController

    with pytest.raises(HTTPException) as exc_info:
        await PositionsController.unpin_positions.fn(_controller(), 999999)
    assert exc_info.value.status_code == 404


async def test_unpin_already_inactive_404(pin_env):
    from backend.api.routes.positions import PositionsController
    factory, _PositionPair = pin_env

    async with factory() as s:
        row = _PositionPair(
            account="ZG0790", symbol_a="BANKNIFTY25APRFUT",
            symbol_b="NIFTY25APRFUT", created_by="op1", active=False,
        )
        s.add(row)
        await s.commit()
        row_id = row.id

    with pytest.raises(HTTPException) as exc_info:
        await PositionsController.unpin_positions.fn(_controller(), row_id)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 5 — RBAC: both routes must be admin-guarded
# ---------------------------------------------------------------------------

async def test_pin_and_unpin_routes_are_admin_guarded():
    from backend.api.routes.positions import PositionsController
    from backend.api.auth_guard import admin_guard

    assert admin_guard in (PositionsController.pin_positions.guards or [])
    assert admin_guard in (PositionsController.unpin_positions.guards or [])


# ---------------------------------------------------------------------------
# 6 — schema default + migration wiring
# ---------------------------------------------------------------------------

async def test_position_row_pin_group_key_defaults_to_none():
    from backend.api.schemas import PositionRow
    row = PositionRow(
        account="ZG0790", tradingsymbol="NIFTY25APRFUT", exchange="NFO",
        product="MIS", quantity=1, average_price=100.0, pnl=0.0,
    )
    assert row.pin_group_key is None


async def test_position_pairs_migration_wired_into_init_db():
    """database.py:init_db() must run the position_pairs migration — and
    that migration must emit the partial (WHERE active) unique index, not
    a plain table-wide constraint (a plain constraint would block
    re-creating a pair after a soft-delete)."""
    import inspect
    from backend.api import database as _db

    init_src = inspect.getsource(_db.init_db)
    assert "_migrate_position_pairs_table" in init_src, (
        "_migrate_position_pairs_table must be called from init_db()"
    )

    migration_src = inspect.getsource(_db._migrate_position_pairs_table)
    assert "CREATE TABLE IF NOT EXISTS position_pairs" in migration_src
    assert "WHERE active" in migration_src, (
        "the uniqueness index must be partial (active rows only) so a "
        "soft-deleted pair doesn't block re-creating the same pin"
    )
