"""
Tests for the WS-subscribe-on-fill gap fix (2026-10).

Background: CLAUDE.md documents an "immediate subscribe on fill" mechanism
for the primary Kite/Dhan/Groww postback paths (resolve instrument_token +
call the ticker's subscribe). A separate audit (commit 0b862409) found
THREE additional backend paths that can mark an AlgoOrder row FILLED
without going through that primary flow:

  1. GET /api/orders/ (`list_orders`) — a pure 15s-TTL broker-order-book
     passthrough with zero AlgoOrder mutation (verified: not a gap site
     for a ticker-subscribe fix, since nothing ever transitions state
     there to hook into — see report for rationale).
  2. `_task_open_order_watchdog` (background.py) -> `_rco_reconcile_account`
     -> `_rco_reconcile_one_row` (orders.py) — the 5-min Dhan/Groww
     reconcile sweep. CONFIRMED GAP — fixed.
  3. Admin `/algo/reconcile` sweep (`reconcile_algo_orders`) and the
     per-card `/{broker_order_id}/reconcile` (`reconcile_single_order`).
     CONFIRMED GAP — fixed (both).

Two more gaps were found during the same audit pass, not in the original
list of three:

  4. chase's own terminal-fill update
     (`chase.py:_chase_terminal_update_db`) — the mechanism Dhan/Groww
     rely on when postback delivery isn't configured. CONFIRMED GAP —
     fixed.
  5. the ticket-placement success path for Dhan/Groww
     (`orders_place.py:_opp_live_handle_success`) — its own docstring
     already documents that `_positions_refresh_after_fill` is the ONLY
     fill-adjacent mechanism scheduled there, but it only ever refreshed
     position DATA, never the ticker subscription. CONFIRMED GAP — fixed.

A deeper latent bug was also found in the *existing* "primary path"
mechanism itself: it called the ticker's bare `subscribe()`, which
registers the token with the live socket but does NOT populate
`_token_to_sym`/`_sym_to_token` — so each tick publishes to the SSE bus
with `sym=""` and every symbol-keyed frontend consumer silently drops it
until the next `_task_performance` cycle's own `subscribe_with_sym()`
backfills the mapping. This fully explains "lags up to 5 minutes" even
through the documented-working path, and reproduces on BOTH ticker
flavours `get_ticker()` can return: the in-process `TickerManager` (dev,
`RAMBOQ_USE_CONN_SERVICE` unset) AND the conn-service-mode
`MmapTickReader` (prod) — whose own bare `subscribe()` forwards an
empty symbol over UDS too (`mmap_ticker.py`). Fixed by switching every
site to `subscribe_with_sym()`.

All sites now funnel through one shared helper,
`backend.api.routes.orders._subscribe_filled_pairs`.
"""

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ===========================================================================
# Core helper — _subscribe_filled_pairs
# ===========================================================================

class TestSubscribeFilledPairsHelper:
    """Functional tests for the shared kick+resolve+subscribe_with_sym
    helper that every gap-fix call site now delegates to."""

    @pytest.mark.asyncio
    async def test_resolves_token_and_subscribes_with_sym(self):
        from backend.api.routes.orders import _subscribe_filled_pairs

        mock_ticker = MagicMock()
        tok_map = {("NIFTY24800CE", "NFO"): 12345}

        with (
            patch("backend.api.background.kick_performance") as mock_kick,
            patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker),
            patch(
                "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                new=AsyncMock(return_value=tok_map),
            ),
        ):
            await _subscribe_filled_pairs([("NIFTY24800CE", "NFO")])

        mock_kick.assert_called_once()
        mock_ticker.subscribe_with_sym.assert_called_once_with(
            [(12345, "NIFTY24800CE")]
        )

    @pytest.mark.asyncio
    async def test_empty_pairs_still_kicks_but_does_not_subscribe(self):
        from backend.api.routes.orders import _subscribe_filled_pairs

        mock_ticker = MagicMock()
        with (
            patch("backend.api.background.kick_performance") as mock_kick,
            patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker),
        ):
            await _subscribe_filled_pairs([])

        mock_kick.assert_called_once()
        mock_ticker.subscribe_with_sym.assert_not_called()

    @pytest.mark.asyncio
    async def test_unresolved_token_skips_subscribe_but_still_kicks(self):
        from backend.api.routes.orders import _subscribe_filled_pairs

        mock_ticker = MagicMock()
        with (
            patch("backend.api.background.kick_performance") as mock_kick,
            patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker),
            patch(
                "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                new=AsyncMock(return_value={}),
            ),
        ):
            await _subscribe_filled_pairs([("UNKNOWNSYM", "NFO")])

        mock_kick.assert_called_once()
        mock_ticker.subscribe_with_sym.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_raises_on_ticker_failure(self):
        """Best-effort — a broken ticker singleton must never propagate
        an exception into a caller's commit/response path."""
        from backend.api.routes.orders import _subscribe_filled_pairs

        with (
            patch("backend.api.background.kick_performance"),
            patch(
                "backend.brokers.kite_ticker.get_ticker",
                side_effect=RuntimeError("boom"),
            ),
        ):
            await _subscribe_filled_pairs([("X", "NFO")])  # must not raise

    @pytest.mark.asyncio
    async def test_instrument_map_fetched_once_per_exchange(self):
        """Multiple pairs on the same exchange must share one
        get_or_fetch_instruments() call, not one per pair."""
        from backend.api.routes.orders import _subscribe_filled_pairs

        mock_ticker = MagicMock()
        tok_map = {("A", "NFO"): 1, ("B", "NFO"): 2}
        fetch_mock = AsyncMock(return_value=tok_map)

        with (
            patch("backend.api.background.kick_performance"),
            patch("backend.brokers.kite_ticker.get_ticker", return_value=mock_ticker),
            patch(
                "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                new=fetch_mock,
            ),
        ):
            await _subscribe_filled_pairs([("a", "nfo"), ("b", "nfo")])

        fetch_mock.assert_called_once_with("NFO")
        mock_ticker.subscribe_with_sym.assert_called_once()
        batch = mock_ticker.subscribe_with_sym.call_args[0][0]
        assert set(batch) == {(1, "A"), (2, "B")}


# ===========================================================================
# _rco_reconcile_one_row — captures (symbol, exchange) the fix depends on
# ===========================================================================

class TestReconcileOneRowCapturesSymbolExchange:
    """Pure-logic test: confirms the row object appended to `_attach_queue`
    on a FILLED transition carries `.symbol`/`.exchange`, which is exactly
    what the watchdog + admin-sweep gap fixes read to build the
    (sym, exch) pairs handed to `_subscribe_filled_pairs`."""

    def test_filled_transition_appends_row_with_symbol_and_exchange(self):
        from backend.api.routes.orders import _rco_reconcile_one_row

        row = SimpleNamespace(
            broker_order_id="BO123", status="OPEN", detail="",
            symbol="NIFTY24800CE", exchange="NFO", quantity=50,
            fill_price=None, filled_quantity=0, filled_at=None,
        )
        by_id = {"BO123": {"status": "COMPLETE", "average_price": 150.0}}
        attach_queue: list = []

        updated, missing = _rco_reconcile_one_row(row, by_id, attach_queue)

        assert (updated, missing) == (1, 0)
        assert row.status == "FILLED"
        assert attach_queue == [row]
        assert (row.symbol, row.exchange) == ("NIFTY24800CE", "NFO")

    def test_non_filled_transition_does_not_populate_attach_queue(self):
        from backend.api.routes.orders import _rco_reconcile_one_row

        row = SimpleNamespace(
            broker_order_id="BO456", status="OPEN", detail="",
            symbol="INFY", exchange="NSE", quantity=10,
            fill_price=None, filled_quantity=0, filled_at=None,
        )
        by_id = {"BO456": {"status": "CANCELLED"}}
        attach_queue: list = []

        updated, missing = _rco_reconcile_one_row(row, by_id, attach_queue)

        assert updated == 1
        assert row.status == "CANCELLED"
        assert attach_queue == []  # nothing to subscribe for a non-fill


# ===========================================================================
# Groww postback — no-regression check, now delegates to the shared helper
# ===========================================================================

class TestGrowwPostbackDelegatesToHelper:
    @pytest.mark.asyncio
    async def test_groww_complete_fill_resolves_kite_exchange_and_delegates(self):
        from backend.api.routes import orders as orders_mod

        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        with patch.object(
            orders_mod, "_subscribe_filled_pairs",
            new=AsyncMock(side_effect=_fake_subscribe),
        ):
            await orders_mod._groww_handle_complete_fill(
                {"exchange": "NSE_FNO"}, "INFY24800CE",
            )

        assert captured == [[("INFY24800CE", "NFO")]], (
            "Groww's NSE_FNO must still normalise to Kite's NFO before "
            "being handed to the shared subscribe helper (no regression "
            "from the refactor onto _subscribe_filled_pairs)"
        )


# ===========================================================================
# Structural checks — every gap-fix call site wires into the shared helper
# ===========================================================================

def _source_of(obj) -> str:
    """inspect.getsource, unwrapping Litestar's route-handler descriptor
    (`.fn`) when `obj` is a decorated handler rather than a plain
    function/method."""
    return inspect.getsource(getattr(obj, "fn", obj))


class TestDhanPostbackGapFix:
    def test_dhan_inline_fill_block_delegates_to_shared_helper(self):
        src = Path("backend/api/routes/orders.py").read_text()
        block_start = src.index("# On a completed Dhan fill")
        block_end = src.index("\n\n", block_start)
        block = src[block_start:block_end]
        assert "_subscribe_filled_pairs(" in block, (
            "Dhan postback's completed-fill block must delegate to "
            "_subscribe_filled_pairs (kick + resubscribe)"
        )


class TestAdminSweepGapFix:
    def test_reconcile_algo_orders_subscribes_newly_filled_rows(self):
        from backend.api.routes.orders import OrdersController

        src = _source_of(OrdersController.reconcile_algo_orders)
        assert "_subscribe_filled_pairs(" in src, (
            "Admin /algo/reconcile sweep must call _subscribe_filled_pairs "
            "for rows that just transitioned to FILLED via _attach_queue"
        )
        assert "_attach_queue" in src


class TestPerCardReconcileGapFix:
    def test_reconcile_single_order_subscribes_on_fill(self):
        from backend.api.routes.orders import OrdersController

        src = _source_of(OrdersController.reconcile_single_order)
        assert "_subscribe_filled_pairs(" in src, (
            "Per-card reconcile endpoint must call _subscribe_filled_pairs "
            "when a row transitions to FILLED"
        )
        # Must gate on r.status == "FILLED" directly, not solely on
        # _attach_after_commit (which is False when the broker order dict
        # lacks average_price — see _rco_reconcile_apply_target — and
        # would otherwise silently miss some genuine FILLED transitions).
        sub_pos = src.index("_subscribe_filled_pairs(")
        preceding = src[:sub_pos]
        last_if = preceding.rfind("if ")
        gate_line = preceding[last_if:sub_pos]
        assert 'r.status == "FILLED"' in gate_line, (
            "The subscribe-on-fill call must be gated on r.status == "
            "'FILLED' directly, not only on _attach_after_commit"
        )


class TestWatchdogGapFix:
    def test_open_order_watchdog_subscribes_newly_filled_rows(self):
        import backend.api.background as bg

        src = _source_of(bg._task_open_order_watchdog)
        assert "_subscribe_filled_pairs(" in src, (
            "_task_open_order_watchdog must call _subscribe_filled_pairs "
            "for rows reconciled to FILLED, instead of waiting for the "
            "next 5-minute cycle"
        )
        assert "attach_queue" in src


class TestChaseTerminalGapFix:
    def test_chase_terminal_update_db_subscribes_on_fill(self):
        import backend.api.algo.chase as chase_mod

        src = _source_of(chase_mod._chase_terminal_update_db)
        assert "_subscribe_filled_pairs(" in src, (
            "chase's own terminal-fill update must call "
            "_subscribe_filled_pairs — chase is the primary fill-detection "
            "path when Dhan/Groww postback delivery isn't configured"
        )
        fill_pos = src.index('if _new_status == "FILLED":')
        assert src.index("_subscribe_filled_pairs(", fill_pos) > fill_pos, (
            "the subscribe call must be inside the FILLED branch"
        )


# ===========================================================================
# Kite primary postback — subscribe_with_sym fix + no regression
# ===========================================================================

class TestKitePostbackSubscribeWithSym:
    def test_source_uses_subscribe_with_sym_not_bare_subscribe(self):
        """Regression guard: the primary Kite postback path's own
        immediate-subscribe call must use subscribe_with_sym (which
        populates the symbol->token map SSE/getSnapshot(sym) consumers
        need), not the bare subscribe() that silently leaves ticks
        symbol-unaddressable until the next perf cycle."""
        src = Path("backend/api/routes/orders_postback.py").read_text()
        func_start = src.index("async def kite_postback_handler")
        func_src = src[func_start:]
        sub_pos = func_src.index("get_ticker().subscribe")
        call_text = func_src[sub_pos:sub_pos + 40]
        assert call_text.startswith("get_ticker().subscribe_with_sym("), (
            f"expected subscribe_with_sym(...), got: {call_text!r}"
        )

    def test_complete_fill_subscribes_with_sym_correct_token_and_symbol(self):
        """Functional: a COMPLETE postback with instrument_token must call
        get_ticker().subscribe_with_sym([(token, SYMBOL)])."""
        from backend.api.routes.orders_postback import kite_postback_handler

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "order_id": "ORD001",
            "order_timestamp": "2026-08-23 10:00:00",
            "checksum": "abc",
            "user_id": "UID123",
            "status": "COMPLETE",
            "tradingsymbol": "reliance",
            "transaction_type": "BUY",
            "quantity": 10,
            "average_price": 2800.0,
            "status_message": "",
            "instrument_token": 738561,
        })

        mock_ticker = MagicMock()

        with (
            patch("backend.api.routes.orders_postback._pb_verify_signature",
                  new=AsyncMock(return_value=True)),
            patch("backend.api.routes.orders_postback._pb_write_audit"),
            patch("backend.api.routes.orders_postback.asyncio"),
            patch("backend.api.routes.orders._postback_broadcast_fanout",
                  MagicMock(), create=True),
            patch("backend.api.background.kick_performance", MagicMock()),
            patch("backend.brokers.kite_ticker.get_ticker",
                  return_value=mock_ticker),
        ):
            try:
                asyncio.run(kite_postback_handler(mock_request))
            except Exception:
                pass  # fanout import may fail in test isolation; subscribe
                      # call happens before the return, so it still fires

        mock_ticker.subscribe_with_sym.assert_called_once_with(
            [(738561, "RELIANCE")]
        )

    def test_no_subscribe_call_when_status_not_complete(self):
        from backend.api.routes.orders_postback import kite_postback_handler

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={
            "order_id": "ORD002",
            "order_timestamp": "2026-08-23 10:00:00",
            "checksum": "abc",
            "user_id": "UID123",
            "status": "CANCELLED",
            "tradingsymbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": 10,
            "average_price": 0,
            "status_message": "",
            "instrument_token": 738561,
        })

        mock_ticker = MagicMock()

        with (
            patch("backend.api.routes.orders_postback._pb_verify_signature",
                  new=AsyncMock(return_value=True)),
            patch("backend.api.routes.orders_postback._pb_write_audit"),
            patch("backend.api.routes.orders_postback.asyncio"),
            patch("backend.api.routes.orders._postback_broadcast_fanout",
                  MagicMock(), create=True),
            patch("backend.api.background.kick_performance", MagicMock()),
            patch("backend.brokers.kite_ticker.get_ticker",
                  return_value=mock_ticker),
        ):
            try:
                asyncio.run(kite_postback_handler(mock_request))
            except Exception:
                pass

        mock_ticker.subscribe_with_sym.assert_not_called()


# ===========================================================================
# Functional tests — gap sites invoked for real (not just source-inspected)
# ===========================================================================

class TestPerCardReconcileFunctional:
    """Call reconcile_single_order directly (unwrapping Litestar's `.fn`)
    with a fully-mocked DB session + broker-order lookup."""

    @pytest.mark.asyncio
    async def test_fill_without_average_price_still_subscribes(self):
        """The case _attach_after_commit=False (no average_price on the
        broker order dict) is exactly where a gate on _attach_after_commit
        alone would have missed the fill — r.status == 'FILLED' must
        still trigger the subscribe."""
        from backend.api.routes.orders import OrdersController
        from backend.api.schemas import ReconcileSingleRequest

        fake_row = SimpleNamespace(
            status="OPEN", symbol="NIFTY24800CE", exchange="NFO",
            quantity=50, fill_price=None, filled_quantity=0,
            filled_at=None, detail="",
        )

        mock_scalars = MagicMock()
        mock_scalars.first.return_value = fake_row
        mock_exec_result = MagicMock()
        mock_exec_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_exec_result)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        bo_no_avg_price = {"status": "COMPLETE"}  # no average_price key

        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        with (
            patch("backend.api.routes.orders.is_admin_request", return_value=True),
            patch(
                "backend.api.routes.orders._rco_fetch_broker_order",
                new=AsyncMock(return_value=(bo_no_avg_price, "COMPLETE", "FILLED")),
            ),
            patch("backend.api.database.async_session", return_value=mock_session),
            patch("backend.api.routes.orders.invalidate"),
            patch("backend.api.routes.orders._maybe_fire_template_attach_for_reconcile"),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
        ):
            data = ReconcileSingleRequest(account="ZG0790")
            result = await OrdersController.reconcile_single_order.fn(
                MagicMock(), "BO1", MagicMock(), data,
            )

        assert fake_row.status == "FILLED"
        assert result["updated"] is True
        assert captured == [[("NIFTY24800CE", "NFO")]], (
            "subscribe-on-fill must fire even when the broker order dict "
            "has no average_price"
        )

    @pytest.mark.asyncio
    async def test_non_fill_update_does_not_subscribe(self):
        from backend.api.routes.orders import OrdersController
        from backend.api.schemas import ReconcileSingleRequest

        fake_row = SimpleNamespace(
            status="OPEN", symbol="INFY", exchange="NSE",
            quantity=5, fill_price=None, filled_quantity=0,
            filled_at=None, detail="",
        )
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = fake_row
        mock_exec_result = MagicMock()
        mock_exec_result.scalars.return_value = mock_scalars
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_exec_result)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        with (
            patch("backend.api.routes.orders.is_admin_request", return_value=True),
            patch(
                "backend.api.routes.orders._rco_fetch_broker_order",
                new=AsyncMock(return_value=({"status": "CANCELLED"}, "CANCELLED", "CANCELLED")),
            ),
            patch("backend.api.database.async_session", return_value=mock_session),
            patch("backend.api.routes.orders.invalidate"),
            patch("backend.api.routes.orders._maybe_fire_template_attach_for_reconcile"),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
        ):
            data = ReconcileSingleRequest(account="ZG0790")
            await OrdersController.reconcile_single_order.fn(
                MagicMock(), "BO2", MagicMock(), data,
            )

        assert fake_row.status == "CANCELLED"
        assert captured == []


class TestAdminSweepFunctional:
    @pytest.mark.asyncio
    async def test_admin_sweep_subscribes_rows_that_just_filled(self):
        from backend.api.routes.orders import OrdersController

        fake_row = SimpleNamespace(
            account="ZG0790", status="FILLED", symbol="INFY", exchange="NSE",
        )

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [fake_row]
        mock_exec_result = MagicMock()
        mock_exec_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_exec_result)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _fake_reconcile_account(acct, acct_rows, attach_queue):
            attach_queue.append(fake_row)
            return 1, 0

        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        with (
            patch("backend.api.routes.orders.is_admin_request", return_value=True),
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.api.routes.orders._rco_reconcile_account",
                new=AsyncMock(side_effect=_fake_reconcile_account),
            ),
            patch("backend.api.routes.orders.invalidate"),
            patch("backend.api.routes.orders._maybe_fire_template_attach_for_reconcile"),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
        ):
            result = await OrdersController.reconcile_algo_orders.fn(MagicMock(), MagicMock())

        assert result["updated"] == 1
        assert captured == [[("INFY", "NSE")]]


class TestWatchdogFunctional:
    @pytest.mark.asyncio
    async def test_watchdog_subscribes_rows_reconciled_to_filled(self):
        import backend.api.background as bg

        fake_row = SimpleNamespace(account="ZG0790", symbol="TCS", exchange="NSE")

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [fake_row]
        mock_exec_result = MagicMock()
        mock_exec_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_exec_result)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _fake_reconcile_account(acct, acct_rows, attach_queue):
            attach_queue.append(fake_row)
            return 1, 0

        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        sleep_calls = {"n": 0}

        async def _fake_sleep(_secs):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise asyncio.CancelledError()

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.api.routes.orders._rco_reconcile_account",
                new=AsyncMock(side_effect=_fake_reconcile_account),
            ),
            patch(
                "backend.api.routes.orders_place._maybe_fire_template_attach_for_reconcile",
            ),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
            patch("asyncio.sleep", new=_fake_sleep),
        ):
            with pytest.raises(asyncio.CancelledError):
                await bg._task_open_order_watchdog()

        assert captured == [[("TCS", "NSE")]]


class TestChaseTerminalFunctional:
    @pytest.mark.asyncio
    async def test_terminal_fill_subscribes(self):
        import backend.api.algo.chase as chase_mod

        fake_row = SimpleNamespace(
            id=1, status="OPEN", symbol="NIFTY24800CE", exchange="NFO",
            quantity=50, fill_price=None, filled_quantity=0, filled_at=None,
            detail="", attempts=0, agent_id=None,
            target_pct=None, target_abs=None, parent_order_id=None,
            template_id=None, account="ZG0790", transaction_type="BUY",
            product="NRML", mode="live", intent="", is_close_intent=False,
        )

        mock_scalar_result = MagicMock()
        mock_scalar_result.scalar_one_or_none.return_value = fake_row
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_scalar_result)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        outcome_key = next(
            k for k, v in chase_mod._OUTCOME_TO_STATUS.items() if v == "FILLED"
        )

        with (
            patch.object(chase_mod, "_async_session", return_value=mock_session),
            patch(
                "backend.api.routes.orders_postback._pb_write_ledger_fills",
                new=AsyncMock(),
            ),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
        ):
            await chase_mod._chase_terminal_update_db(
                algo_order_id=1, broker_order_id="BO1", outcome=outcome_key,
                attempts=2, final_price=150.0, error=None,
            )

        assert fake_row.status == "FILLED"
        assert captured == [[("NIFTY24800CE", "NFO")]]

    @pytest.mark.asyncio
    async def test_already_filled_row_does_not_resubscribe(self):
        """Final-status guard: a row already FILLED must not re-subscribe
        on a late/racing chase terminal update."""
        import backend.api.algo.chase as chase_mod

        fake_row = SimpleNamespace(
            id=2, status="FILLED", symbol="TCS", exchange="NSE",
            quantity=10, fill_price=100.0, filled_quantity=10,
            filled_at=None, detail="", attempts=1, agent_id=None,
            target_pct=None, target_abs=None, parent_order_id=None,
            template_id=None, account="ZG0790", transaction_type="BUY",
            product="NRML", mode="live", intent="", is_close_intent=False,
        )
        mock_scalar_result = MagicMock()
        mock_scalar_result.scalar_one_or_none.return_value = fake_row
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_scalar_result)
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        outcome_key = next(
            k for k, v in chase_mod._OUTCOME_TO_STATUS.items() if v == "FILLED"
        )

        with (
            patch.object(chase_mod, "_async_session", return_value=mock_session),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
        ):
            await chase_mod._chase_terminal_update_db(
                algo_order_id=2, broker_order_id="BO2", outcome=outcome_key,
                attempts=1, final_price=100.0, error=None,
            )

        assert captured == [], (
            "already-FILLED row must not re-subscribe — the FINAL-status "
            "guard refuses to re-mutate it, so the subscribe call (which "
            "sits inside that same mutation branch) must not fire either"
        )


# ===========================================================================
# Fifth gap (found during audit, not in the original 3): ticket-placement
# success path for Dhan/Groww (orders_place.py:_opp_live_handle_success)
# ===========================================================================

class TestTicketPlacementDhanGrowwGapFix:
    """For Dhan/Groww, _positions_refresh_after_fill is scheduled from the
    ticket-success path because postback delivery is unreliable/manually
    configured for those brokers — but it only ever refreshed position
    DATA, never the ticker subscription. Fixed by firing
    _subscribe_filled_pairs alongside it, scoped the same way."""

    @pytest.mark.asyncio
    async def test_dhan_ticket_success_subscribes_instrument(self):
        from backend.api.routes import orders_place as op_mod

        data = SimpleNamespace(source="ticket", exchange="NFO")
        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        with (
            patch("backend.brokers.registry._broker_id_for", return_value="dhan"),
            patch(
                "backend.api.routes.orders._positions_refresh_after_fill",
                new=AsyncMock(),
            ),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
            patch(
                "backend.api.algo.agent_engine.record_manual_event",
                new=AsyncMock(),
            ),
            patch("backend.api.routes.orders_helpers._clear_rejections"),
        ):
            await op_mod._opp_live_handle_success(
                data, "DHAN001", "NIFTY24800CE", "BUY", 50,
                "BO999", False, "bk_key_1", algo_order_id=None,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert captured == [[("NIFTY24800CE", "NFO")]]

    @pytest.mark.asyncio
    async def test_kite_ticket_success_does_not_double_subscribe(self):
        """Kite's own postback already does this immediately on a real
        fill — firing it again speculatively at placement time for Kite
        would just double the broker/instrument-store round-trip."""
        from backend.api.routes import orders_place as op_mod

        data = SimpleNamespace(source="ticket", exchange="NFO")
        captured: list = []

        async def _fake_subscribe(pairs):
            captured.append(pairs)

        with (
            patch("backend.brokers.registry._broker_id_for", return_value="kite"),
            patch(
                "backend.api.routes.orders._subscribe_filled_pairs",
                new=AsyncMock(side_effect=_fake_subscribe),
            ),
            patch(
                "backend.api.algo.agent_engine.record_manual_event",
                new=AsyncMock(),
            ),
            patch("backend.api.routes.orders_helpers._clear_rejections"),
        ):
            await op_mod._opp_live_handle_success(
                data, "ZG0790", "RELIANCE", "BUY", 10,
                "BO1000", False, "bk_key_2", algo_order_id=None,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert captured == []
