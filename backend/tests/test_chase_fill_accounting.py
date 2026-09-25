"""Regression tests for the chase fill-accounting fixes (C2/C3/C4).

C2 — chase.py's cancel-and-replace loop tracked "already filled" as
    `quantity - remaining_qty` (a chase-WIDE total) and diffed it against
    `filled_qty` (the CURRENT order's own from-zero fill) as if the two
    were on the same basis. They aren't, and treating them as if they
    were double-counted fills across cancel-and-replace cycles — a
    3-lot (225 qty) order could silently balloon to 300 filled.

C3 — the MCX/NCO lots→contracts reverse-translate applied to EVERY
    broker gated only by exchange, but Groww already reports MCX
    `filled_quantity` in contracts (unlike Kite/Dhan, which report
    lots). Applying the translate to a Groww fill count inflated it by
    lot_size×.

C4 — service-restart recovery (`backend.api.background.recover_live_chases`)
    always restarted a chase at the full original `quantity`, ignoring
    `filled_quantity` and never checking whether the pre-restart resting
    order was still live at the broker — risking a full duplicate order
    alongside an orphan.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────
# C2 — end-to-end 225 → 300 regression (full chase_order loop, stateful
# fake broker keyed by order id so cancel/place/status all interact
# realistically across cancel-and-replace cycles).
# ─────────────────────────────────────────────────────────────────────────

class _FakeMcxFreeBroker:
    """Stateful fake broker for a plain NFO chase (no lots translation).

    Orders are tracked in `self.orders` keyed by broker order id.
    `fill_script` maps the Nth placed order (1-indexed) to how much of
    THAT order's own placed quantity eventually trades — mirrors the
    incident's own numbers (each of the 3 orders in the 225-qty chase
    independently fills 75).
    """

    def __init__(self, fill_script: dict[int, int]):
        self.fill_script = fill_script
        self.orders: dict[str, dict] = {}
        self._n = 0
        self.placed_qtys: list[int] = []

    def normalise_qty(self, exchange, qty, lot_size):
        return qty

    def quote(self, keys):
        key = keys[0]
        return {key: {"depth": {
            "buy":  [{"price": 100.00, "quantity": 500}],
            "sell": [{"price": 100.10, "quantity": 500}],
        }}}

    def place_order(self, **kwargs):
        self._n += 1
        oid = f"order_{self._n}"
        placed_qty = int(kwargs["quantity"])
        self.placed_qtys.append(placed_qty)
        filled = min(self.fill_script.get(self._n, 0), placed_qty)
        self.orders[oid] = {"placed_qty": placed_qty, "filled": filled, "cancelled": False}
        return oid

    def cancel_order(self, order_id, variety="regular", exchange=""):
        o = self.orders.get(order_id)
        if o is not None:
            o["cancelled"] = True

    def order_status(self, order_id):
        o = self.orders.get(order_id)
        if o is None:
            return {}
        if o["cancelled"]:
            status = "CANCELLED"
        elif o["placed_qty"] > 0 and o["filled"] >= o["placed_qty"]:
            status = "COMPLETE"
        else:
            status = "OPEN"
        return {
            "status": status,
            "quantity": o["placed_qty"],
            "filled_quantity": o["filled"],
            "average_price": 100.05,
        }


@pytest.mark.asyncio
async def test_c2_225_to_300_regression_fixed():
    """3-lot (225) chase, each of 3 successive orders independently fills
    75 of its OWN placed size. Pre-fix this produced order sizes
    [225, 150, 150] (double-counted, chase-wide intent 300). Post-fix
    the sizes must be [225, 150, 75] — cumulative fill converges to
    exactly 225, never overshoots."""
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    broker = _FakeMcxFreeBroker(fill_script={1: 75, 2: 75, 3: 75})
    cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=6)

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._get_broker_registry", return_value=broker),
    ):
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY24DECFUT",
            transaction_type="BUY",
            quantity=225,
            cfg=cfg,
        )

    assert broker.placed_qtys == [225, 150, 75], (
        f"C2 regression — expected order sizes [225, 150, 75] (converging "
        f"to exactly 225 filled), got {broker.placed_qtys}. A size of 150 "
        f"on the 3rd order means the delta math double-counted the 2nd "
        f"order's own fill as if it were a chase-wide total (the 225→300 "
        f"incident)."
    )
    assert result.status == ChaseStatus.FILLED
    assert result.attempts == 3
    total_actually_filled = sum(o["filled"] for o in broker.orders.values())
    assert total_actually_filled == 225, (
        f"Total contracts actually traded across all 3 orders must be "
        f"exactly 225 (3 lots), got {total_actually_filled}"
    )


@pytest.mark.asyncio
async def test_c2_cumulative_persisted_to_db_matches_true_total():
    """The cumulative value passed to `_record_partial_fill` at every
    step (and on final completion) must always be the TRUE chase-wide
    running total, never a per-order value re-interpreted as cumulative."""
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    broker = _FakeMcxFreeBroker(fill_script={1: 75, 2: 75, 3: 75})
    cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=6)

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._get_broker_registry", return_value=broker),
        patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock) as mock_rec,
    ):
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY24DECFUT",
            transaction_type="BUY",
            quantity=225,
            cfg=cfg,
            algo_order_id=99,
        )

    assert result.status == ChaseStatus.FILLED
    cumulative_values = [call.args[1] for call in mock_rec.call_args_list]
    # Every recorded cumulative value must be monotonically non-decreasing
    # and never exceed the true total (225) — the pre-fix bug could both
    # regress (stale re-add) and overshoot past `quantity`.
    assert cumulative_values, "expected at least one _record_partial_fill call"
    assert all(v <= 225 for v in cumulative_values), (
        f"cumulative filled_quantity written to DB must never exceed the "
        f"true total 225, got {cumulative_values}"
    )
    assert cumulative_values == sorted(cumulative_values), (
        f"cumulative filled_quantity must be monotonically non-decreasing, "
        f"got {cumulative_values}"
    )
    assert cumulative_values[-1] == 225, (
        f"final recorded cumulative must equal the true total 225, "
        f"got {cumulative_values[-1]}"
    )


@pytest.mark.asyncio
async def test_c2_late_fill_race_before_cancel_is_captured():
    """A fill that lands on the resting order strictly between the LAST
    regular poll and the cancel-and-replace must still be folded into
    cumulative_filled — not lost, and not causing the next order to be
    oversized."""
    from backend.api.algo.chase import _ch_capture_late_fill, ChaseConfig

    cfg = ChaseConfig(exchange="NFO")

    async def _fake_run(fn, *args):
        # Simulate: the order fully filled (100/100) by the time the
        # POST-cancel status read happens, even though the regular poll
        # earlier in the same attempt only saw 40/100.
        return {"status": "CANCELLED", "quantity": 100, "filled_quantity": 100, "average_price": 101.0}

    with (
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock),
    ):
        cumulative, current_order_filled, remaining, avg_price = await _ch_capture_late_fill(
            account="ACC1", order_id="O1", cfg=cfg, symbol="NIFTY24DECFUT",
            quantity=100, cumulative_filled=40, current_order_filled=40,
            algo_order_id=None,
        )

    assert cumulative == 100, f"late fill (40→100) must be captured, got cumulative={cumulative}"
    assert remaining == 0
    assert avg_price == 101.0


# ─────────────────────────────────────────────────────────────────────────
# C3 — Groww MCX filled_quantity must NOT be reverse-translated (already
# contracts); Kite/Dhan MUST be (they report lots).
# ─────────────────────────────────────────────────────────────────────────

class TestC3BrokerGatedMcxTranslate:
    def test_groww_mcx_not_translated(self):
        from backend.api.algo.chase import _ch_reverse_translate_mcx_filled, ChaseConfig

        cfg = ChaseConfig(exchange="MCX")
        with (
            patch("backend.brokers.registry._broker_id_for", return_value="groww"),
            patch("backend.api.algo.chase._lot_size_sync", return_value=100),
        ):
            out = _ch_reverse_translate_mcx_filled(cfg, "CRUDEOIL24DECFUT", "G1", 1)

        assert out == 1, (
            f"Groww already reports MCX filled_quantity in CONTRACTS — "
            f"must pass through unchanged, got {out} (lot_size×1 would be 100)"
        )

    def test_kite_mcx_translated(self):
        from backend.api.algo.chase import _ch_reverse_translate_mcx_filled, ChaseConfig

        cfg = ChaseConfig(exchange="MCX")
        with (
            patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"),
            patch("backend.api.algo.chase._lot_size_sync", return_value=100),
        ):
            out = _ch_reverse_translate_mcx_filled(cfg, "CRUDEOIL24DECFUT", "ZG0790", 1)

        assert out == 100, (
            f"Kite reports MCX filled_quantity in LOTS — must reverse-translate "
            f"to contracts (1 lot × 100 lot_size = 100), got {out}"
        )

    def test_dhan_mcx_translated(self):
        from backend.api.algo.chase import _ch_reverse_translate_mcx_filled, ChaseConfig

        cfg = ChaseConfig(exchange="MCX")
        with (
            patch("backend.brokers.registry._broker_id_for", return_value="dhan"),
            patch("backend.api.algo.chase._lot_size_sync", return_value=100),
        ):
            out = _ch_reverse_translate_mcx_filled(cfg, "CRUDEOIL24DECFUT", "D1", 1)

        assert out == 100, f"Dhan reports MCX filled_quantity in LOTS, got {out}"

    def test_non_mcx_exchange_never_translated(self):
        from backend.api.algo.chase import _ch_reverse_translate_mcx_filled, ChaseConfig

        cfg = ChaseConfig(exchange="NFO")
        with patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"):
            out = _ch_reverse_translate_mcx_filled(cfg, "NIFTY24DECFUT", "ZG0790", 50)
        assert out == 50


@pytest.mark.asyncio
async def test_c3_groww_mcx_partial_fill_via_poll_status_not_inflated():
    """End-to-end through _chase_poll_status: a Groww MCX order reporting
    filled_quantity=100 (already contracts) must be read as 100 —
    NOT reverse-translated to 10,000 (100 × lot_size)."""
    from backend.api.algo.chase import _chase_poll_status, ChaseResult, ChaseConfig

    cfg = ChaseConfig(exchange="MCX", interval_seconds=0)
    result = ChaseResult(account="G1", symbol="CRUDEOIL24DECFUT",
                          transaction_type="BUY", quantity=200)
    result.initial_price = 100.0

    def _fake_order_status(account, order_id):
        return {"status": "OPEN", "filled_quantity": 100, "average_price": 100.0}

    async def _fake_run(fn, *args):
        return fn(*args)

    with (
        patch("backend.api.algo.chase._order_status", side_effect=_fake_order_status),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.brokers.registry._broker_id_for", return_value="groww"),
        patch("backend.api.algo.chase._lot_size_sync", return_value=100),
        patch("backend.api.algo.chase._record_partial_fill", new_callable=AsyncMock),
    ):
        signal, remaining_qty, cumulative_filled, current_order_filled = await _chase_poll_status(
            account="G1", current_order_id="GO1", cfg=cfg,
            symbol="CRUDEOIL24DECFUT", transaction_type="BUY",
            quantity=200, result=result, attempt=1, remaining_qty=200,
            algo_order_id=None, emit=lambda *a, **k: None,
            cumulative_filled=0, current_order_filled=0,
        )

    assert cumulative_filled == 100, (
        f"Groww MCX fill must be read as 100 contracts (unconverted), "
        f"got {cumulative_filled} — a value of 10000 means the lots "
        f"reverse-translate was wrongly applied to Groww"
    )
    assert remaining_qty == 100


# ─────────────────────────────────────────────────────────────────────────
# C4 — service-restart recovery: derive already_filled correctly and
# cancel an orphaned resting order before restarting the chase.
# ─────────────────────────────────────────────────────────────────────────

class TestC4RecoveryAlreadyFilled:
    @pytest.mark.asyncio
    async def test_orphaned_resting_order_derives_correct_already_filled_and_cancels(self):
        """Advisor-corrected C4 scenario: DB shows 75 filled (from order
        A, before the restart). The resting order B was placed for the
        remaining 150 and (unknown to the DB) filled another 75 of its
        OWN size before the crash. The naive `max(db_filled, live_filled)`
        would give max(75, 75) = 75 (WRONG — reproduces the C2 bug).
        The correct derivation uses B's own unfilled remainder:
        already_filled = row.quantity - (live_order_qty - live_filled)
                        = 225 - (150 - 75) = 150.
        """
        from backend.api.background import _recover_chase_already_filled
        from backend.api.algo.chase import ChaseConfig

        row = SimpleNamespace(
            id=1, account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            quantity=225, filled_quantity=75, broker_order_id="B2",
        )
        cfg = ChaseConfig(exchange="NFO", variety="regular")

        def _fake_order_status(account, order_id):
            assert order_id == "B2"
            return {"status": "OPEN", "quantity": 150, "filled_quantity": 75, "average_price": 100.0}

        mock_cancel = MagicMock()

        with (
            patch("backend.api.algo.chase._order_status", side_effect=_fake_order_status),
            patch("backend.api.algo.chase._cancel_order", mock_cancel),
        ):
            already_filled, already_filled_price = await _recover_chase_already_filled(row, cfg)

        assert already_filled == 150, (
            f"expected the CORRECT C4 derivation (150), a value of 75 means "
            f"the recovery math repeated the C2 bug (treating the resting "
            f"order's own from-zero fill as if it were already the "
            f"chase-wide total); got {already_filled}"
        )
        assert already_filled_price == 100.0, (
            "average_price from the live order_status read must be "
            "returned alongside the quantity — it feeds the "
            "already-filled-at-start finalize path's final_price, which "
            "gates whether template-attach/auto-TP fire"
        )
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args.args[0] == "ACC1"
        assert mock_cancel.call_args.args[1] == "B2"

    @pytest.mark.asyncio
    async def test_mcx_kite_recovery_reverse_translates_both_qty_and_filled(self):
        """Council devil's-advocate regression: on MCX/NCO via a
        lots-convention broker (Kite/Dhan), `order_status` reports BOTH
        `quantity` and `filled_quantity` in LOTS — not just
        `filled_quantity`. Reverse-translating only `filled_quantity`
        (leaving `quantity` raw in lots) and then subtracting them mixes
        lots and contracts.

        1-lot CRUDEOIL (lot_size=100) resting order, 0 filled at restart:
        broker reports quantity=1 (lot), filled_quantity=0. Correct
        already_filled = 100 - (100 - 0) = 0 (nothing filled yet — the
        recovered chase should restart at the FULL original size, not
        99% of it).
        """
        from backend.api.background import _recover_chase_already_filled
        from backend.api.algo.chase import ChaseConfig

        row = SimpleNamespace(
            id=6, account="ACC1", symbol="CRUDEOIL24DECFUT", exchange="MCX",
            quantity=100, filled_quantity=0, broker_order_id="B6",
        )
        cfg = ChaseConfig(exchange="MCX", variety="regular")

        def _fake_order_status(account, order_id):
            assert order_id == "B6"
            return {"status": "OPEN", "quantity": 1, "filled_quantity": 0, "average_price": 5000.0}

        mock_cancel = MagicMock()

        with (
            patch("backend.api.algo.chase._order_status", side_effect=_fake_order_status),
            patch("backend.api.algo.chase._cancel_order", mock_cancel),
            patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"),
            patch("backend.api.algo.chase._lot_size_sync", return_value=100),
        ):
            already_filled, _ = await _recover_chase_already_filled(row, cfg)

        assert already_filled == 0, (
            f"expected 0 (nothing filled — both quantity and filled_quantity "
            f"must be reverse-translated from lots to contracts before "
            f"subtracting); a unit-mismatch bug (quantity left in lots=1, "
            f"filled_quantity in contracts=0) would derive "
            f"100 - (1 - 0) = 99, silently abandoning 99% of the position "
            f"on restart; got {already_filled}"
        )

    @pytest.mark.asyncio
    async def test_mcx_kite_recovery_partial_fill_both_translated(self):
        """Same MCX/Kite scenario as above but with a genuine partial fill:
        2-lot CRUDEOIL (200 contracts) resting order, 1 lot (100 contracts)
        filled at restart. Broker reports quantity=2 (lots),
        filled_quantity=1 (lots). Correct already_filled =
        200 - (200 - 100) = 100."""
        from backend.api.background import _recover_chase_already_filled
        from backend.api.algo.chase import ChaseConfig

        row = SimpleNamespace(
            id=7, account="ACC1", symbol="CRUDEOIL24DECFUT", exchange="MCX",
            quantity=200, filled_quantity=0, broker_order_id="B7",
        )
        cfg = ChaseConfig(exchange="MCX", variety="regular")

        def _fake_order_status(account, order_id):
            return {"status": "OPEN", "quantity": 2, "filled_quantity": 1, "average_price": 5000.0}

        with (
            patch("backend.api.algo.chase._order_status", side_effect=_fake_order_status),
            patch("backend.api.algo.chase._cancel_order", MagicMock()),
            patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"),
            patch("backend.api.algo.chase._lot_size_sync", return_value=100),
        ):
            already_filled, already_filled_price = await _recover_chase_already_filled(row, cfg)

        assert already_filled == 100, (
            f"expected 100 contracts (1 lot filled × lot_size 100); got {already_filled}"
        )
        assert already_filled_price == 5000.0

    @pytest.mark.asyncio
    async def test_terminal_resting_order_not_cancelled_again(self):
        """A resting order that already COMPLETEd before the restart must
        not be cancelled a second time (broker would reject/no-op, but we
        should not even attempt it)."""
        from backend.api.background import _recover_chase_already_filled
        from backend.api.algo.chase import ChaseConfig

        row = SimpleNamespace(
            id=2, account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            quantity=225, filled_quantity=150, broker_order_id="B3",
        )
        cfg = ChaseConfig(exchange="NFO", variety="regular")

        def _fake_order_status(account, order_id):
            return {"status": "COMPLETE", "quantity": 75, "filled_quantity": 75, "average_price": 100.0}

        mock_cancel = MagicMock()

        with (
            patch("backend.api.algo.chase._order_status", side_effect=_fake_order_status),
            patch("backend.api.algo.chase._cancel_order", mock_cancel),
        ):
            already_filled, already_filled_price = await _recover_chase_already_filled(row, cfg)

        assert already_filled == 225
        assert already_filled_price == 100.0
        mock_cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_unverifiable_status_skips_row_not_fallback_to_db(self):
        """When the live order_status call fails outright, recovery must
        return None (skip this row) rather than silently trusting the
        DB's filled_quantity — a stale/unverifiable read could be hiding
        a still-resting orphaned order at the broker."""
        from backend.api.background import _recover_chase_already_filled
        from backend.api.algo.chase import ChaseConfig

        row = SimpleNamespace(
            id=3, account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            quantity=225, filled_quantity=75, broker_order_id="B4",
        )
        cfg = ChaseConfig(exchange="NFO", variety="regular")

        def _raise_order_status(account, order_id):
            raise RuntimeError("broker timeout")

        with patch("backend.api.algo.chase._order_status", side_effect=_raise_order_status):
            already_filled = await _recover_chase_already_filled(row, cfg)

        assert already_filled is None

    @pytest.mark.asyncio
    async def test_empty_status_response_skips_row(self):
        """An empty `{}` response (Kite's order_history miss shape) must
        also be treated as unverifiable — never fall back to the DB."""
        from backend.api.background import _recover_chase_already_filled
        from backend.api.algo.chase import ChaseConfig

        row = SimpleNamespace(
            id=4, account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            quantity=225, filled_quantity=75, broker_order_id="B5",
        )
        cfg = ChaseConfig(exchange="NFO", variety="regular")

        with patch("backend.api.algo.chase._order_status", return_value={}):
            already_filled = await _recover_chase_already_filled(row, cfg)

        assert already_filled is None

    @pytest.mark.asyncio
    async def test_no_broker_order_id_uses_db_filled_quantity(self):
        """No resting order recorded at all — nothing to reconcile or
        cancel; the DB value is the only available truth."""
        from backend.api.background import _recover_chase_already_filled
        from backend.api.algo.chase import ChaseConfig

        row = SimpleNamespace(
            id=5, account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            quantity=225, filled_quantity=75, fill_price=210.5,
            broker_order_id=None,
        )
        cfg = ChaseConfig(exchange="NFO", variety="regular")

        already_filled, already_filled_price = await _recover_chase_already_filled(row, cfg)
        assert already_filled == 75
        assert already_filled_price == 210.5, (
            "no live order to query — must fall back to the DB's own "
            "fill_price, not a hardcoded 0.0"
        )

    @pytest.mark.asyncio
    async def test_recovered_already_filled_seeds_chase_order_remaining_qty(self):
        """Integration seam: chase_order's FIRST placed order size must be
        `quantity - already_filled`, not the full original quantity."""
        from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

        broker = _FakeMcxFreeBroker(fill_script={1: 75})
        cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=3)

        with (
            patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
            patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
            patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
            patch("backend.api.algo.chase._get_broker_registry", return_value=broker),
        ):
            await chase_order(
                account="ACC1", symbol="NIFTY24DECFUT",
                transaction_type="BUY", quantity=225,
                cfg=cfg, already_filled=150,
            )

        assert broker.placed_qtys[0] == 75, (
            f"recovery seeded already_filled=150 of 225 — first order must "
            f"size at the remaining 75, got {broker.placed_qtys[0]}"
        )

    @pytest.mark.asyncio
    async def test_recovery_already_fully_filled_places_no_order(self):
        """If recovery derives already_filled == quantity (the resting
        order actually finished before recovery ran), chase_order must
        finalize as FILLED WITHOUT placing any new order (a 0-qty order
        would trip a BrokerInputError)."""
        from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

        broker = _FakeMcxFreeBroker(fill_script={})
        cfg = ChaseConfig(exchange="NFO", interval_seconds=0, max_attempts=3)

        with (
            patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
            patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
            patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
            patch("backend.api.algo.chase._get_broker_registry", return_value=broker),
        ):
            result = await chase_order(
                account="ACC1", symbol="NIFTY24DECFUT",
                transaction_type="BUY", quantity=225,
                cfg=cfg, already_filled=225, already_filled_price=187.5,
            )

        assert broker.placed_qtys == [], "no order should be placed when already fully filled"
        assert result.status == ChaseStatus.FILLED
        assert result.fill_price == 187.5, (
            "council architect regression: a hardcoded 0.0 fill_price on "
            "the already-filled-at-start finalize path silently disables "
            "_chase_terminal_fire_fill_hooks (gated on `if not final_price`), "
            "skipping auto-TP/template-attach for a recovered position that "
            "IS live at the broker; got fill_price={}".format(result.fill_price)
        )
