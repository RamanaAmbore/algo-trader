"""
Tests for close-intent verification (_is_offsetting_position,
_verify_close_intent, _ticket_enforce_lot_and_fat_finger,
_ticket_check_mcx_size_cap) and per-broker fill status recognition
(_broker_is_fill_status).

Bracket/Template-GTT removal (T3.1, 2026-11) removed every test class
in this file that covered backend.api.algo.template_attach or the
deleted reconcile/postback template-attach gates
(_opl_reconcile_attach_eligible, _pb_wants_template_attach,
_ticket_strip_template_on_close) — TestTpLimitOffset,
TestFullFillGateReconcile, TestFullFillGatePostback,
TestScaleOutLotRounding, TestWingSkippedReason, TestFillPriceAssertion,
TestWingHardRejectOI, TestStaleLockEvictionWarning,
TestValidateGttTriggers, and the reconcile/postback/template-clearing
tests inside what used to be TestCloseOffsetGate. The surviving
close-intent-verification coverage below is unaffected by that removal
— none of it depends on a template or a bracket.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ─── #13: per-broker fill status ─────────────────────────────────────────────

class TestBrokerFillStatuses:
    """Finding #13 — per-broker fill status recognition."""

    def test_kite_complete_is_fill(self):
        from backend.api.routes.orders_postback import _broker_is_fill_status
        assert _broker_is_fill_status("kite", "COMPLETE") is True

    def test_dhan_traded_is_fill(self):
        from backend.api.routes.orders_postback import _broker_is_fill_status
        assert _broker_is_fill_status("dhan", "TRADED") is True

    def test_dhan_complete_not_fill(self):
        from backend.api.routes.orders_postback import _broker_is_fill_status
        # Dhan sends TRADED, not COMPLETE, for fills.
        assert _broker_is_fill_status("dhan", "COMPLETE") is False

    def test_unknown_broker_defaults_to_complete(self):
        from backend.api.routes.orders_postback import _broker_is_fill_status
        assert _broker_is_fill_status("acme_broker", "COMPLETE") is True
        assert _broker_is_fill_status("acme_broker", "TRADED") is False

    def test_sync_algo_order_rows_maps_traded_to_filled_for_dhan(self):
        from backend.api.routes.orders_postback import _BROKER_STATUS_MAP, _broker_is_fill_status
        # Verify that TRADED is NOT in the canonical status map (it's broker-specific)
        assert _BROKER_STATUS_MAP.get("TRADED") is None
        # But Dhan path should recognise it
        assert _broker_is_fill_status("dhan", "TRADED") is True


class TestCloseOffsetGate:
    """Close-intent and offsetting-position verification —
    `_is_offsetting_position`, `_verify_close_intent`, and the
    `_ticket_enforce_lot_and_fat_finger`/`_ticket_check_mcx_size_cap`
    safety-cap bypass they gate. Generic to every order, not
    Bracket/template-specific.
    """

    # ── Fix B: _is_offsetting_position ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_sell_against_long_is_offsetting(self):
        """SELL with net_qty > 0 → _is_offsetting_position returns True."""
        import pandas as pd
        from backend.api.routes.orders_place import _is_offsetting_position

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE",
            "quantity": 50,
            "account": "ZG0001",
        }])
        with patch(
            "backend.api.routes.orders_place._is_offsetting_position.__module__",
            create=True,
        ):
            with patch(
                "backend.brokers.broker_apis.fetch_positions",
                return_value=[df],
            ):
                result = await _is_offsetting_position(
                    sym="NIFTY25JUL24000CE",
                    exchange="NFO",
                    side="SELL",
                    account="ZG0001",
                )
        assert result is True

    @pytest.mark.asyncio
    async def test_buy_against_short_is_offsetting(self):
        """BUY with net_qty < 0 → _is_offsetting_position returns True."""
        import pandas as pd
        from backend.api.routes.orders_place import _is_offsetting_position

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE",
            "quantity": -50,
            "account": "ZG0001",
        }])
        with patch(
            "backend.brokers.broker_apis.fetch_positions",
            return_value=[df],
        ):
            result = await _is_offsetting_position(
                sym="NIFTY25JUL24000CE",
                exchange="NFO",
                side="BUY",
                account="ZG0001",
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_buy_with_no_existing_position_not_offsetting(self):
        """Symbol not in positions → _is_offsetting_position returns False."""
        import pandas as pd
        from backend.api.routes.orders_place import _is_offsetting_position

        df = pd.DataFrame([{
            "tradingsymbol": "OTHER_SYMBOL",
            "quantity": 50,
            "account": "ZG0001",
        }])
        with patch(
            "backend.brokers.broker_apis.fetch_positions",
            return_value=[df],
        ):
            result = await _is_offsetting_position(
                sym="NIFTY25JUL24000CE",
                exchange="NFO",
                side="BUY",
                account="ZG0001",
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_position_fetch_exception_fails_open(self):
        """fetch_positions raises → _is_offsetting_position returns False (fail-open)."""
        from backend.api.routes.orders_place import _is_offsetting_position

        with patch(
            "backend.brokers.broker_apis.fetch_positions",
            side_effect=RuntimeError("broker unavailable"),
        ):
            result = await _is_offsetting_position(
                sym="NIFTY25JUL24000CE",
                exchange="NFO",
                side="SELL",
                account="ZG0001",
            )
        assert result is False

    # ── 2026-09 council audit fix: _verify_close_intent (magnitude check) ─────
    # The devil's-advocate lens found classifyIntent() (frontend) decides
    # 'close' from SIGN alone with no magnitude check, and the server used to
    # trust that flag directly to bypass G2's 5-lot fat-finger cap. These
    # tests cover the new server-side sign+magnitude verification.

    @pytest.mark.asyncio
    async def test_verify_close_intent_true_when_qty_within_position(self):
        """SELL 50 against a held LONG of 50 → magnitude matches exactly, verified True."""
        import pandas as pd
        from backend.api.routes.orders_place import _verify_close_intent

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE", "quantity": 50, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
            result = await _verify_close_intent(
                sym="NIFTY25JUL24000CE", exchange="NFO", side="SELL",
                account="ZG0001", contracts=50,
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_close_intent_false_when_qty_exceeds_position(self):
        """THE bug this fixes: SELL 500 against a held LONG of only 1 —
        sign agrees (would classify 'close' client-side) but magnitude
        vastly exceeds the real position. Must return False so the caller
        falls back to the full G2 fat-finger cap instead of bypassing it."""
        import pandas as pd
        from backend.api.routes.orders_place import _verify_close_intent

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE", "quantity": 1, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
            result = await _verify_close_intent(
                sym="NIFTY25JUL24000CE", exchange="NFO", side="SELL",
                account="ZG0001", contracts=500,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_close_intent_true_when_qty_less_than_position(self):
        """Partial close — SELL 20 against a held LONG of 50 — is a
        legitimate close of a subset; magnitude check is <=, not ==."""
        import pandas as pd
        from backend.api.routes.orders_place import _verify_close_intent

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE", "quantity": 50, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
            result = await _verify_close_intent(
                sym="NIFTY25JUL24000CE", exchange="NFO", side="SELL",
                account="ZG0001", contracts=20,
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_close_intent_false_on_sign_mismatch(self):
        """BUY against a held LONG is not a close at all (same-direction
        add), regardless of magnitude — must return False."""
        import pandas as pd
        from backend.api.routes.orders_place import _verify_close_intent

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE", "quantity": 50, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
            result = await _verify_close_intent(
                sym="NIFTY25JUL24000CE", exchange="NFO", side="BUY",
                account="ZG0001", contracts=10,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_close_intent_false_no_position(self):
        """No matching position row at all → never verified, fails closed."""
        import pandas as pd
        from backend.api.routes.orders_place import _verify_close_intent

        df = pd.DataFrame([{
            "tradingsymbol": "OTHER_SYMBOL", "quantity": 50, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
            result = await _verify_close_intent(
                sym="NIFTY25JUL24000CE", exchange="NFO", side="SELL",
                account="ZG0001", contracts=1,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_close_intent_fails_closed_on_fetch_error(self):
        """Position-fetch error → fails CLOSED (False), unlike
        _is_offsetting_position's fail-OPEN design — this function gates a
        safety-cap bypass, so inability to verify must never grant it."""
        from backend.api.routes.orders_place import _verify_close_intent

        with patch(
            "backend.brokers.broker_apis.fetch_positions",
            side_effect=RuntimeError("broker unavailable"),
        ):
            result = await _verify_close_intent(
                sym="NIFTY25JUL24000CE", exchange="NFO", side="SELL",
                account="ZG0001", contracts=1,
            )
        assert result is False

    # ── 2026-09 council audit fix: _ticket_enforce_lot_and_fat_finger no ──────
    # longer trusts a client-claimed intent='close' directly for the G2
    # bypass — it must be independently verified via _verify_close_intent.

    @pytest.mark.asyncio
    async def test_enforce_fat_finger_rejects_unverified_close_claim_over_cap(self):
        """Client claims intent='close' with 10 lots (>5-lot cap), but the
        server can't verify it (position fetch says no matching position)
        — must raise the 400 fat-finger rejection, not bypass it."""
        from backend.api.schemas import TicketOrderRequest
        from backend.api.routes.orders_place import _ticket_enforce_lot_and_fat_finger
        from litestar.exceptions import HTTPException
        import pandas as pd

        data = TicketOrderRequest(
            mode="live", side="SELL", tradingsymbol="NIFTY25JUL24000CE",
            quantity=1, exchange="NFO", account="ZG0001", intent="close",
        )
        empty_df = pd.DataFrame([{
            "tradingsymbol": "OTHER_SYMBOL", "quantity": 50, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[empty_df]):
            with pytest.raises(HTTPException) as exc_info:
                await _ticket_enforce_lot_and_fat_finger(
                    data, account="ZG0001", sym="NIFTY25JUL24000CE",
                    contracts=10 * 50, lot_size=50,  # 10 lots
                )
        assert exc_info.value.status_code == 400
        assert "5-lot safety cap" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_enforce_fat_finger_allows_verified_close_over_cap(self):
        """Client claims intent='close', server verifies it against a real,
        sufficiently large position — the >5-lot cap bypass is legitimately
        granted (this is the correct, intended close-of-large-position case)."""
        from backend.api.schemas import TicketOrderRequest
        from backend.api.routes.orders_place import _ticket_enforce_lot_and_fat_finger
        import pandas as pd

        data = TicketOrderRequest(
            mode="live", side="SELL", tradingsymbol="NIFTY25JUL24000CE",
            quantity=1, exchange="NFO", account="ZG0001", intent="close",
        )
        real_df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE", "quantity": 500, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[real_df]):
            # Must not raise — 10 lots (500 contracts) is within the real
            # 500-contract long position being closed.
            await _ticket_enforce_lot_and_fat_finger(
                data, account="ZG0001", sym="NIFTY25JUL24000CE",
                contracts=10 * 50, lot_size=50,
            )

    # ── 2026-09-27 audit deepened: verified intent must propagate to EVERY ────
    # downstream consumer of data.intent, not just this function's own local
    # decision. MCX/NCO is the critical case: G2 above is unconditionally
    # exempt for MCX/NCO, so `_ticket_check_mcx_size_cap` (the ticket's ONLY
    # size guard for that exchange) was the sole thing standing between a
    # claimed-but-unverified 'close' and an unlimited-size live MCX order —
    # and it read the raw client claim directly, independent of this
    # function's own verification. These tests cover the fix: this function
    # now mutates `data.intent` in place to the verified result, so any
    # downstream reader of the SAME `data` object sees the verified value.

    @pytest.mark.asyncio
    async def test_enforce_normalizes_data_intent_to_none_when_unverified(self):
        """Claimed close, verification fails (no matching position) —
        data.intent must be scrubbed to None, not left as the raw 'close'
        claim, so no downstream reader can be fooled by it."""
        from backend.api.schemas import TicketOrderRequest
        from backend.api.routes.orders_place import _ticket_enforce_lot_and_fat_finger
        import pandas as pd

        data = TicketOrderRequest(
            mode="live", side="SELL", tradingsymbol="NIFTY25JUL24000CE",
            quantity=1, exchange="NFO", account="ZG0001", intent="close",
        )
        empty_df = pd.DataFrame([{
            "tradingsymbol": "OTHER_SYMBOL", "quantity": 50, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[empty_df]):
            # 2 lots stays under the 5-lot cap so this call doesn't raise —
            # isolates the intent-normalization behavior from the cap check.
            await _ticket_enforce_lot_and_fat_finger(
                data, account="ZG0001", sym="NIFTY25JUL24000CE",
                contracts=2 * 50, lot_size=50,
            )
        assert data.intent is None

    @pytest.mark.asyncio
    async def test_enforce_normalizes_data_intent_to_close_when_verified(self):
        """Claimed close, verification succeeds — data.intent stays 'close'
        so legitimate downstream bypasses (MCX cap, preflight G2) still work."""
        from backend.api.schemas import TicketOrderRequest
        from backend.api.routes.orders_place import _ticket_enforce_lot_and_fat_finger
        import pandas as pd

        data = TicketOrderRequest(
            mode="live", side="SELL", tradingsymbol="NIFTY25JUL24000CE",
            quantity=1, exchange="NFO", account="ZG0001", intent="close",
        )
        real_df = pd.DataFrame([{
            "tradingsymbol": "NIFTY25JUL24000CE", "quantity": 500, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[real_df]):
            await _ticket_enforce_lot_and_fat_finger(
                data, account="ZG0001", sym="NIFTY25JUL24000CE",
                contracts=10 * 50, lot_size=50,
            )
        assert data.intent == "close"

    @pytest.mark.asyncio
    async def test_mcx_cap_rejects_unverified_close_claim_end_to_end(self):
        """THE vulnerability this closes: an MCX order claiming intent='close'
        for 30 lots (> 20-lot cap) against NO real matching position. G2 is
        unconditionally exempt for MCX, so _ticket_check_mcx_size_cap is the
        ONLY guard — before this fix it trusted the raw claim and let this
        through uncapped. Runs both functions in the real request order
        (enforce_lot_and_fat_finger first, exactly as ticket_order_handler
        does) against the SAME data object to prove the normalization
        actually reaches the second function."""
        from backend.api.schemas import TicketOrderRequest
        from backend.api.routes.orders_place import (
            _ticket_enforce_lot_and_fat_finger, _ticket_check_mcx_size_cap,
        )
        from litestar.exceptions import HTTPException
        import pandas as pd

        data = TicketOrderRequest(
            mode="live", side="SELL", tradingsymbol="CRUDEOIL25JULFUT",
            quantity=1, exchange="MCX", account="ZG0001", intent="close",
        )
        empty_df = pd.DataFrame([{
            "tradingsymbol": "OTHER_SYMBOL", "quantity": 50, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[empty_df]):
            # MCX is exempt from G2 regardless of verification outcome, so
            # this call never raises — but it MUST normalize data.intent.
            await _ticket_enforce_lot_and_fat_finger(
                data, account="ZG0001", sym="CRUDEOIL25JULFUT",
                contracts=30 * 100, lot_size=100,  # 30 lots, cap is 20
            )
        assert data.intent is None  # unverified claim scrubbed

        with pytest.raises(HTTPException) as exc_info:
            _ticket_check_mcx_size_cap(
                data, sym="CRUDEOIL25JULFUT", contracts=30 * 100, lot_size=100,
            )
        assert exc_info.value.status_code == 422
        assert "20-lot safety limit" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_mcx_cap_allows_verified_close_claim_end_to_end(self):
        """Same MCX 30-lot order, but against a REAL matching 30+ lot
        position — legitimately verified close, cap must be bypassed."""
        from backend.api.schemas import TicketOrderRequest
        from backend.api.routes.orders_place import (
            _ticket_enforce_lot_and_fat_finger, _ticket_check_mcx_size_cap,
        )
        import pandas as pd

        data = TicketOrderRequest(
            mode="live", side="SELL", tradingsymbol="CRUDEOIL25JULFUT",
            quantity=1, exchange="MCX", account="ZG0001", intent="close",
        )
        real_df = pd.DataFrame([{
            "tradingsymbol": "CRUDEOIL25JULFUT", "quantity": 3000, "account": "ZG0001",
        }])
        with patch("backend.brokers.broker_apis.fetch_positions", return_value=[real_df]):
            await _ticket_enforce_lot_and_fat_finger(
                data, account="ZG0001", sym="CRUDEOIL25JULFUT",
                contracts=30 * 100, lot_size=100,
            )
        assert data.intent == "close"

        # Must not raise — legitimately verified close bypasses the cap.
        _ticket_check_mcx_size_cap(
            data, sym="CRUDEOIL25JULFUT", contracts=30 * 100, lot_size=100,
        )


    @pytest.mark.asyncio
    async def test_override_only_new_open_keeps_overrides(self):
        """Sanity check the other direction: a genuine new open (not
        offsetting) must keep its override fields intact."""
        from backend.api.schemas import TicketOrderRequest

        data = TicketOrderRequest(
            mode="live",
            side="BUY",
            tradingsymbol="NIFTY25JUL24000CE",
            quantity=1,
            exchange="NFO",
            account="ZG0001",
            template_id=None,
            tp_pct_override=30.0,
        )

        captured = {}

        async def _fake_validate(d, req):
            return "BUY", "NIFTY25JUL24000CE", 50, 50

        def _fake_account(d):
            return "ZG0001"

        async def _fake_offsetting(sym, exchange, side, account):
            return False

        async def _fake_enforce(d, acc, sym, qty, ls):
            captured["data_at_enforce"] = d

        async def _fake_gate(d, sym):
            pass

        async def _fake_capacity(*a, **kw):
            pass

        with patch("backend.api.routes.orders_place._ticket_validate_input",
                   new=_fake_validate), \
             patch("backend.api.routes.orders_place._ticket_validate_account",
                   new=_fake_account), \
             patch("backend.api.routes.orders_place._is_offsetting_position",
                   new=_fake_offsetting), \
             patch("backend.api.routes.orders_place._ticket_enforce_lot_and_fat_finger",
                   new=_fake_enforce), \
             patch("backend.api.routes.orders_place._ticket_gate_market_hours_and_align_price",
                   new=_fake_gate), \
             patch("backend.api.routes.orders_place._enforce_capacity_guard",
                   new=_fake_capacity), \
             patch("backend.shared.helpers.settings.get_bool", return_value=False), \
             patch("backend.shared.helpers.utils.config",
                   {"deploy_branch": "dev"}), \
             patch("backend.api.routes.orders_place._ticket_place_live",
                   new_callable=AsyncMock, return_value=None), \
             patch("backend.api.routes.orders_place._ticket_place_paper",
                   new_callable=AsyncMock, return_value=None):
            from backend.api.routes.orders_place import ticket_order_handler
            await ticket_order_handler(data, MagicMock())

        assert captured["data_at_enforce"].tp_pct_override == 30.0
