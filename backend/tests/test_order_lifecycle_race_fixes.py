"""
Regression tests for the 2026-09-27 order-lifecycle race-condition audit
fixes (chase / postback / ledger / TP-arm bundle).

Findings covered:
  #1 — TP double-arm race (_opp_arm_tp_persist_row locks the PARENT row
       before its existence check) + partial-fill TP qty bug (filled_qty
       threaded through instead of the original order's full quantity).
  #2/#3 — status moving backwards / duplicate postback double-fires
       (ALGO_ORDER_FINAL_STATUSES write-refuse guard + SELECT ... FOR
       UPDATE locking on every status-writer path).
  #4 — live fills via Dhan/Groww postback never reached the strategy lot
       ledger (only the Kite path called _pb_write_ledger_fills).
  #5 — TP orders always placed NRML regardless of the parent's real
       product (parent_product now threaded from both callers).
  #6 — chase's fill path attached templates (armed exit GTTs)
       unconditionally, unlike the postback path's offsetting-position
       check.

Also verifies: the FINAL-status guard must NOT block a genuine late
FILLED arriving after CANCELLED/CANCEL_FAILED/UNFILLED (a failed cancel
can still have a broker-side order resting live that fills later) — only
FILLED/REJECTED are truly final. Admin reconcile gets the FOR UPDATE lock
but deliberately NOT the write-refuse guard (it is the repair path).
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_MODELS_SRC = Path("backend/api/models.py").read_text()
_POSTBACK_SRC = Path("backend/api/routes/orders_postback.py").read_text()
_CHASE_SRC = Path("backend/api/algo/chase.py").read_text()
_ORDERS_PLACE_SRC = Path("backend/api/routes/orders_place.py").read_text()
_ORDERS_SRC = Path("backend/api/routes/orders.py").read_text()


# ─────────────────────────────────────────────────────────────────────────
# models.py — FINAL vs TERMINAL status sets
# ─────────────────────────────────────────────────────────────────────────

def test_final_statuses_is_narrower_than_terminal():
    from backend.api.models import ALGO_ORDER_TERMINAL_STATUSES, ALGO_ORDER_FINAL_STATUSES
    assert ALGO_ORDER_FINAL_STATUSES == {"FILLED", "REJECTED"}
    assert ALGO_ORDER_FINAL_STATUSES < ALGO_ORDER_TERMINAL_STATUSES
    # CANCELLED/CANCEL_FAILED/UNFILLED must NOT be in the write-refuse set —
    # a broker-side order can still be resting live in those states.
    for s in ("CANCELLED", "CANCEL_FAILED", "UNFILLED"):
        assert s not in ALGO_ORDER_FINAL_STATUSES, (
            f"{s} must stay OUT of ALGO_ORDER_FINAL_STATUSES — a late genuine "
            f"FILLED from that state must still be applied"
        )


def test_postback_guards_use_final_not_terminal_set():
    """Both postback guard functions must import ALGO_ORDER_FINAL_STATUSES,
    not the broader ALGO_ORDER_TERMINAL_STATUSES — using the terminal set
    would wrongly refuse a late FILLED after a failed cancel."""
    assert "ALGO_ORDER_FINAL_STATUSES" in _POSTBACK_SRC
    # Guard against silent regression back to the terminal set.
    assert "if _r.status in ALGO_ORDER_TERMINAL_STATUSES" not in _POSTBACK_SRC


def test_chase_terminal_guard_uses_final_not_terminal_set():
    assert "ALGO_ORDER_FINAL_STATUSES" in _CHASE_SRC


# ─────────────────────────────────────────────────────────────────────────
# _sync_apply_row_status (Dhan/Groww path) — pure attribute-based guard
# ─────────────────────────────────────────────────────────────────────────

def _row(status: str, **extra) -> SimpleNamespace:
    base = dict(id=1, status=status, fill_price=None, filled_at=None,
                detail=None, created_at=None)
    base.update(extra)
    return SimpleNamespace(**base)


class TestSyncApplyRowStatus:
    def test_refuses_overwrite_from_filled(self):
        from backend.api.routes.orders_postback import _sync_apply_row_status
        r = _row("FILLED")
        changed = _sync_apply_row_status(
            r, new_status="CANCELLED", price=100.0, broker_id="dhan",
            status="CANCELLED", status_message="",
        )
        assert changed is False
        assert r.status == "FILLED", "a FILLED row must never be overwritten"

    def test_refuses_overwrite_from_rejected(self):
        from backend.api.routes.orders_postback import _sync_apply_row_status
        r = _row("REJECTED")
        changed = _sync_apply_row_status(
            r, new_status="FILLED", price=100.0, broker_id="dhan",
            status="TRADED", status_message="",
        )
        assert changed is False
        assert r.status == "REJECTED"

    @pytest.mark.parametrize("stale_status", ["CANCELLED", "CANCEL_FAILED", "UNFILLED"])
    def test_late_fill_still_applies_from_non_final_states(self, stale_status):
        """A cancel that failed (or a row marked cancelled/unfilled) can
        still have a broker-side order resting live — a late genuine
        FILLED must be applied, not refused."""
        from backend.api.routes.orders_postback import _sync_apply_row_status
        r = _row(stale_status)
        changed = _sync_apply_row_status(
            r, new_status="FILLED", price=150.25, broker_id="dhan",
            status="TRADED", status_message="",
        )
        assert changed is True
        assert r.status == "FILLED"
        assert r.fill_price == 150.25

    def test_noop_when_same_status(self):
        from backend.api.routes.orders_postback import _sync_apply_row_status
        r = _row("OPEN")
        changed = _sync_apply_row_status(
            r, new_status="OPEN", price=None, broker_id="dhan",
            status="OPEN", status_message="",
        )
        assert changed is False


class TestPbApplyStatusToRow:
    def test_refuses_overwrite_from_filled(self):
        from backend.api.routes.orders_postback import _pb_apply_status_to_row
        r = _row("FILLED")
        changed = _pb_apply_status_to_row(r, new_status="CANCELLED", price=100.0)
        assert changed is False
        assert r.status == "FILLED"

    @pytest.mark.parametrize("stale_status", ["CANCELLED", "CANCEL_FAILED", "UNFILLED"])
    def test_late_fill_still_applies_from_non_final_states(self, stale_status):
        from backend.api.routes.orders_postback import _pb_apply_status_to_row
        r = _row(stale_status)
        changed = _pb_apply_status_to_row(r, new_status="FILLED", price=99.5)
        assert changed is True
        assert r.status == "FILLED"


# ─────────────────────────────────────────────────────────────────────────
# FOR UPDATE locking — structural checks on every status-writer row-fetch,
# plus a real dialect-compile sanity check that `.with_for_update()`
# actually emits `FOR UPDATE` SQL (not just a no-op on some backends).
# ─────────────────────────────────────────────────────────────────────────

def test_with_for_update_compiles_to_real_lock_clause():
    from sqlalchemy import select as _sel
    from sqlalchemy.dialects import postgresql
    from backend.api.models import AlgoOrder
    stmt = _sel(AlgoOrder).where(AlgoOrder.id == 1).with_for_update()
    compiled = str(stmt.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in compiled


class TestForUpdateLockingPresent:
    def test_sync_algo_order_rows_locks_its_select(self):
        from backend.api.routes.orders_postback import _sync_algo_order_rows
        src = inspect.getsource(_sync_algo_order_rows)
        assert ".with_for_update()" in src

    def test_pb_event_kite_locks_its_select(self):
        from backend.api.routes.orders_postback import _pb_event_kite
        src = inspect.getsource(_pb_event_kite)
        assert ".with_for_update()" in src

    def test_chase_terminal_update_db_locks_its_selects(self):
        from backend.api.algo.chase import _chase_terminal_update_db
        src = inspect.getsource(_chase_terminal_update_db)
        assert src.count(".with_for_update()") >= 2, (
            "both the algo_order_id lookup and the broker_order_id "
            "fallback lookup must be locked"
        )

    def test_admin_reconcile_locks_its_select_but_has_no_refuse_guard(self):
        # Locked (serializes against postback/chase on the same rows)...
        assert (
            'AlgoOrder.status.in_(["OPEN", "CANCEL_FAILED"])' in _ORDERS_SRC
            and ".with_for_update()" in _ORDERS_SRC
        )
        # ...but must NOT gain the FINAL-status write-refuse guard — this
        # is the repair path that corrects stuck rows from broker truth.
        assert "_rco_reconcile_active_rows" in _ORDERS_SRC
        _rco_src = _ORDERS_SRC[_ORDERS_SRC.index("def _rco_reconcile_active_rows"):]
        _rco_src = _rco_src[:_rco_src.index("\ndef _rco_")] if "\ndef _rco_" in _rco_src[20:] else _rco_src
        assert "ALGO_ORDER_FINAL_STATUSES" not in _rco_src


# ─────────────────────────────────────────────────────────────────────────
# #4 — ledger write must fire on the Dhan/Groww path too
# ─────────────────────────────────────────────────────────────────────────

def test_sync_algo_order_rows_writes_ledger_fills():
    from backend.api.routes.orders_postback import _sync_algo_order_rows
    src = inspect.getsource(_sync_algo_order_rows)
    assert "_pb_write_ledger_fills" in src, (
        "Dhan/Groww postback path must write FIFO ledger entries for "
        "filled rows — previously only the Kite path did this"
    )


def test_chase_terminal_update_db_writes_ledger_fills():
    from backend.api.algo.chase import _chase_terminal_update_db
    src = inspect.getsource(_chase_terminal_update_db)
    assert "_pb_write_ledger_fills" in src, (
        "chase's own terminal-fill path must write FIFO ledger entries too"
    )


def test_admin_reconcile_writes_ledger_fills():
    assert "_pb_write_ledger_fills" in _ORDERS_SRC, (
        "admin reconcile must write FIFO ledger entries for rows it "
        "flips to FILLED from broker truth"
    )


@pytest.mark.asyncio
async def test_sync_algo_order_rows_ledger_write_actually_invoked():
    """Behavioral check (not just source grep): a filled row drives a
    real call into the ledger-write helper."""
    from backend.api.routes import orders_postback as m

    mock_row = MagicMock()
    mock_row.id = 42
    mock_row.status = "OPEN"
    mock_row.broker_order_id = "bo-1"

    _rows_result = MagicMock()
    _rows_result.scalars.return_value.all.return_value = [mock_row]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_rows_result)
    mock_session.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch.object(m, "_sync_apply_row_status", return_value=True), \
         patch.object(m, "_pb_write_ledger_fills", new=AsyncMock()) as mock_ledger, \
         patch.object(m, "_write_event", new=AsyncMock(), create=True):
        await m._sync_algo_order_rows(
            broker_id="dhan", order_id="bo-1", status="TRADED",
            price=101.0, status_message="", qty=50,
        )

    mock_ledger.assert_awaited_once()
    _, called_rows = mock_ledger.await_args.args
    assert called_rows == [mock_row]


# ─────────────────────────────────────────────────────────────────────────
# #1 — TP double-arm race: parent row locked BEFORE the existence check
# ─────────────────────────────────────────────────────────────────────────

def test_opp_arm_tp_persist_row_locks_parent_before_existence_check():
    from backend.api.routes.orders_place import _opp_arm_tp_persist_row
    src = inspect.getsource(_opp_arm_tp_persist_row)
    lock_idx = src.index(".with_for_update()")
    existing_idx = src.index("existing = ")
    assert lock_idx < existing_idx, (
        "the parent row must be locked BEFORE the existing-child count "
        "check, so a second concurrent caller blocks until the first's "
        "TP insert commits and its own count then sees it"
    )


@pytest.mark.asyncio
async def test_opp_arm_tp_persist_row_skips_when_child_already_exists():
    from backend.api.routes.orders_place import _opp_arm_tp_persist_row

    mock_parent = MagicMock()
    mock_parent.quantity = 100
    mock_parent.id = 7

    _parent_result = MagicMock()
    _parent_result.scalar_one_or_none.return_value = mock_parent

    _existing_result = MagicMock()
    _existing_result.scalar_one.return_value = 1   # a TP child already exists

    execute_returns = iter([_parent_result, _existing_result])
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(side_effect=lambda *a, **kw: next(execute_returns))
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await _opp_arm_tp_persist_row(
            7, "ZG0790", "NIFTY24APR25000CE", "NFO", "BUY",
            100.0, 0.05, None, "live",
        )
    assert result is None
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_opp_arm_tp_persist_row_uses_filled_qty_not_parent_quantity():
    """#1 bundle — partial-fill TP qty bug: a partial fill must arm TP for
    the ACTUAL filled quantity, not the parent's original full quantity."""
    from backend.api.routes.orders_place import _opp_arm_tp_persist_row

    mock_parent = MagicMock()
    mock_parent.quantity = 300   # original full order size
    mock_parent.id = 9

    _parent_result = MagicMock()
    _parent_result.scalar_one_or_none.return_value = mock_parent

    _existing_result = MagicMock()
    _existing_result.scalar_one.return_value = 0

    execute_returns = iter([_parent_result, _existing_result])
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(side_effect=lambda *a, **kw: next(execute_returns))

    added_rows = []
    mock_session.add = MagicMock(side_effect=lambda row: added_rows.append(row))
    mock_session.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await _opp_arm_tp_persist_row(
            9, "ZG0790", "NIFTY24APR25000CE", "NFO", "BUY",
            100.0, 0.05, None, "live",
            filled_qty=75,   # only 75 of 300 filled so far
        )
    assert result is not None
    assert added_rows[0].quantity == 75, (
        "TP child must be sized to the actual filled qty (75), not the "
        "parent's original full order size (300)"
    )


@pytest.mark.asyncio
async def test_opp_arm_tp_persist_row_falls_back_to_parent_quantity_when_no_filled_qty():
    from backend.api.routes.orders_place import _opp_arm_tp_persist_row

    mock_parent = MagicMock()
    mock_parent.quantity = 300
    mock_parent.id = 11

    _parent_result = MagicMock()
    _parent_result.scalar_one_or_none.return_value = mock_parent
    _existing_result = MagicMock()
    _existing_result.scalar_one.return_value = 0

    execute_returns = iter([_parent_result, _existing_result])
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(side_effect=lambda *a, **kw: next(execute_returns))
    added_rows = []
    mock_session.add = MagicMock(side_effect=lambda row: added_rows.append(row))
    mock_session.commit = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session):
        result = await _opp_arm_tp_persist_row(
            11, "ZG0790", "NIFTY24APR25000CE", "NFO", "BUY",
            100.0, 0.05, None, "live",
        )
    assert result is not None
    assert added_rows[0].quantity == 300


# ─────────────────────────────────────────────────────────────────────────
# #5 — parent_product / filled_qty threaded from both TP-arm callers
# ─────────────────────────────────────────────────────────────────────────

def test_pb_dispatch_take_profit_arm_passes_product_and_filled_qty():
    from backend.api.routes.orders_postback import _pb_dispatch_take_profit_arm
    src = inspect.getsource(_pb_dispatch_take_profit_arm)
    assert "parent_product=" in src
    assert "filled_qty=" in src


def test_chase_maybe_fire_auto_tp_passes_product_and_filled_qty():
    from backend.api.algo.chase import _ch_maybe_fire_auto_tp
    src = inspect.getsource(_ch_maybe_fire_auto_tp)
    assert 'parent_product=snap["product"]' in src
    assert 'filled_qty=snap["filled_quantity"]' in src


def test_arm_take_profit_accepts_filled_qty_and_forwards_it():
    from backend.api.routes.orders_place import _arm_take_profit
    sig = inspect.signature(_arm_take_profit)
    assert "filled_qty" in sig.parameters
    src = inspect.getsource(_arm_take_profit)
    assert "filled_qty=filled_qty" in src


# ─────────────────────────────────────────────────────────────────────────
# #6 — chase's template-attach fires the offsetting-position guard
# ─────────────────────────────────────────────────────────────────────────

def _snap(**over) -> dict:
    base = dict(
        id=5, target_pct=None, target_abs=None, parent_order_id=None,
        template_id=3, account="ZG0790", symbol="NIFTY24APR25000CE",
        exchange="NFO", transaction_type="SELL", product="NRML",
        mode="live", filled_quantity=50, quantity=50,
        intent="", is_close_intent=False,
    )
    base.update(over)
    return base


class TestChaseTemplateAttachOffsettingGuard:
    @pytest.mark.asyncio
    async def test_skips_when_offsetting_position(self):
        from backend.api.algo import chase as m
        with patch("backend.api.routes.orders_place._is_offsetting_position",
                   new=AsyncMock(return_value=True)), \
             patch("backend.api.routes.orders._fire_template_attach_on_fill",
                   new=AsyncMock()) as mock_fire:
            await m._ch_check_and_fire_template_attach(_snap(), 100.0)
        mock_fire.assert_not_called()

    @pytest.mark.asyncio
    async def test_fires_when_not_offsetting(self):
        from backend.api.algo import chase as m
        with patch("backend.api.routes.orders_place._is_offsetting_position",
                   new=AsyncMock(return_value=False)), \
             patch("backend.api.routes.orders._fire_template_attach_on_fill",
                   new=AsyncMock()) as mock_fire:
            await m._ch_check_and_fire_template_attach(_snap(), 100.0)
        mock_fire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_on_explicit_close_intent_without_broker_call(self):
        from backend.api.algo import chase as m
        with patch("backend.api.routes.orders_place._is_offsetting_position",
                   new=AsyncMock()) as mock_offset, \
             patch("backend.api.routes.orders._fire_template_attach_on_fill",
                   new=AsyncMock()) as mock_fire:
            await m._ch_check_and_fire_template_attach(_snap(intent="close"), 100.0)
        mock_offset.assert_not_called()
        mock_fire.assert_not_called()

    def test_snapshot_captures_intent_fields(self):
        from backend.api.algo.chase import _chase_snapshot_algo_row
        row = SimpleNamespace(
            id=1, target_pct=None, target_abs=None, parent_order_id=None,
            template_id=None, account="A", symbol="S", exchange="NFO",
            transaction_type="BUY", product="NRML", mode="live",
            filled_quantity=0, quantity=10, intent="close",
        )
        snap = _chase_snapshot_algo_row(row, "bo-1")
        assert snap["intent"] == "close"
        assert snap["is_close_intent"] is False   # getattr default, not a model column


# ─────────────────────────────────────────────────────────────────────────
# _chase_terminal_update_db — FINAL-status guard behavior
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chase_terminal_update_db_refuses_overwrite_from_filled():
    from backend.api.algo import chase as m

    mock_row = MagicMock()
    mock_row.id = 3
    mock_row.status = "FILLED"
    mock_row.agent_id = None

    _result = MagicMock()
    _result.scalar_one_or_none.return_value = mock_row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()

    with patch("backend.api.algo.chase._async_session", return_value=mock_session):
        await m._chase_terminal_update_db(
            algo_order_id=3, broker_order_id="bo-3", outcome="chase_cancelled",
            attempts=2, final_price=None, error="raced",
        )

    mock_session.commit.assert_not_called()
    assert mock_row.status == "FILLED", "must never move away from FILLED"


@pytest.mark.asyncio
async def test_chase_terminal_update_db_applies_late_fill_from_cancel_failed():
    from backend.api.algo import chase as m

    mock_row = MagicMock()
    mock_row.id = 4
    mock_row.status = "CANCEL_FAILED"
    mock_row.agent_id = None
    mock_row.attempts = 1
    mock_row.strategy_id = None   # no strategy attached — ledger write short-circuits

    _result = MagicMock()
    _result.scalar_one_or_none.return_value = mock_row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()

    with patch("backend.api.algo.chase._async_session", return_value=mock_session):
        await m._chase_terminal_update_db(
            algo_order_id=4, broker_order_id="bo-4", outcome="chase_fill",
            attempts=3, final_price=120.5, error=None,
        )

    # Status-mutation commit + the (short-circuited, no-strategy) ledger
    # write's own unconditional follow-up commit.
    assert mock_session.commit.call_count == 2
    assert mock_row.status == "FILLED"


@pytest.mark.asyncio
async def test_chase_terminal_update_db_ledger_write_actually_invoked():
    from backend.api.algo import chase as m
    from backend.api.routes import orders_postback as pb_mod

    mock_row = MagicMock()
    mock_row.id = 6
    mock_row.status = "OPEN"
    mock_row.agent_id = None

    _result = MagicMock()
    _result.scalar_one_or_none.return_value = mock_row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()

    with patch("backend.api.algo.chase._async_session", return_value=mock_session), \
         patch.object(pb_mod, "_pb_write_ledger_fills", new=AsyncMock()) as mock_ledger:
        await m._chase_terminal_update_db(
            algo_order_id=6, broker_order_id="bo-6", outcome="chase_fill",
            attempts=1, final_price=88.0, error=None,
        )

    mock_ledger.assert_awaited_once_with(mock_session, [mock_row])


@pytest.mark.asyncio
async def test_admin_reconcile_writes_ledger_for_reconciled_fills():
    """Behavioral check that list_active_chases' ledger-write call
    actually fires for rows _rco_reconcile_active_rows flips to FILLED —
    not just a source grep."""
    from backend.api.routes.orders import OrdersController
    from backend.api.routes import orders_postback as pb_mod

    mock_filled_row = MagicMock()
    mock_filled_row.id = 77

    _rows_result = MagicMock()
    _rows_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_rows_result)
    mock_session.commit = AsyncMock()

    mock_request = MagicMock()

    controller = OrdersController.__new__(OrdersController)

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.api.routes.orders._chase_snapshot_paper_open_ids",
               return_value=set()), \
         patch("backend.api.routes.orders._chase_snapshot_broker_status_by_id",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders._rco_reconcile_active_rows",
               return_value=([], [mock_filled_row], True)), \
         patch("backend.api.routes.orders._fetch_child_order_ids",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders._maybe_fire_template_attach_for_reconcile"), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True), \
         patch.object(pb_mod, "_pb_write_ledger_fills", new=AsyncMock()) as mock_ledger:
        await OrdersController.list_active_chases.fn(controller, request=mock_request)

    mock_ledger.assert_awaited_once_with(mock_session, [mock_filled_row])
