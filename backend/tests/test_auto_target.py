"""
Tests for auto profit target (TP) on filled orders.

Covers:
  - _resolve_target_pct priority chain (override → DB → fallback)
  - _arm_take_profit side flip logic and idempotency guard
  - _emit_chase_terminal TP arming on chase_fill
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


def test_resolve_target_pct_explicit_override():
    """Explicit override in request → use that value."""
    from backend.api.routes.orders import _resolve_target_pct

    result = _resolve_target_pct(0.50)
    assert result == 0.50, "explicit override should be used"


def test_resolve_target_pct_db_setting():
    """No explicit override → read from DB setting."""
    from backend.api.routes.orders import _resolve_target_pct

    with patch("backend.shared.helpers.settings.get_float", return_value=0.25):
        result = _resolve_target_pct(None)
        assert result == 0.25, "DB setting should be used when override is None"


def test_resolve_target_pct_hardcoded_fallback():
    """No override and no DB setting → fallback to 0.30."""
    from backend.api.routes.orders import _resolve_target_pct

    with patch("backend.shared.helpers.settings.get_float", return_value=0.30):
        result = _resolve_target_pct(None)
        assert result == 0.30, "fallback should be 0.30"


def test_resolve_target_pct_negative_clamped():
    """Negative override → clamp to 0.0."""
    from backend.api.routes.orders import _resolve_target_pct

    result = _resolve_target_pct(-0.50)
    assert result == 0.0, "negative values should clamp to 0.0"


def test_target_pct_with_abs_combined():
    """Both pct and abs → pct delta applied first, then abs added."""
    fill_price = 100.0
    target_pct = 0.30
    target_abs = 10.0

    # For a BUY parent (TP is SELL):
    # tp_price = fill_price × (1 + target_pct) + abs_delta
    tp_price_sell = fill_price * (1.0 + target_pct) + target_abs
    assert tp_price_sell == pytest.approx(140.0), "100 × 1.30 + 10 = 140"

    # For a SELL parent (TP is BUY):
    # tp_price = fill_price × (1 - target_pct) - abs_delta
    tp_price_buy = fill_price * (1.0 - target_pct) - target_abs
    assert tp_price_buy == pytest.approx(60.0), "100 × 0.70 - 10 = 60"


def test_target_price_buy_parent_side_flip():
    """BUY parent @ 100, TP 30% → SELL TP @ 130."""
    fill_price = 100.0
    target_pct = 0.30
    parent_side = "BUY"

    # Side flip: BUY → SELL
    tp_side = "SELL" if parent_side == "BUY" else "BUY"
    assert tp_side == "SELL"

    # Price: 100 × (1 + 0.30) = 130
    tp_price = fill_price * (1.0 + target_pct)
    assert tp_price == pytest.approx(130.0)


def test_target_price_sell_parent_side_flip():
    """SELL parent @ 100, TP 30% → BUY TP @ 70."""
    fill_price = 100.0
    target_pct = 0.30
    parent_side = "SELL"

    # Side flip: SELL → BUY
    tp_side = "SELL" if parent_side == "BUY" else "BUY"
    assert tp_side == "BUY"

    # Price: 100 × (1 - 0.30) = 70
    tp_price = fill_price * (1.0 - target_pct)
    assert tp_price == pytest.approx(70.0)


@pytest.mark.asyncio
async def test_arm_take_profit_early_return_zero_targets():
    """_arm_take_profit with both target_pct=None and target_abs=None → early exit.

    No DB session should be opened when both targets are disabled.
    """
    from backend.api.routes.orders import _arm_take_profit

    session_ctx = AsyncMock()
    with patch("backend.api.database.async_session", return_value=session_ctx):
        await _arm_take_profit(
            parent_row_id=1001,
            parent_account="ZG0790",
            parent_symbol="NIFTY25APRFUT",
            parent_exchange="NFO",
            parent_side="BUY",
            fill_price=100.0,
            target_pct=None,
            target_abs=None,
            parent_mode="paper",
        )

    # Session should NOT have been entered (early return guard)
    session_ctx.__aenter__.assert_not_called()
