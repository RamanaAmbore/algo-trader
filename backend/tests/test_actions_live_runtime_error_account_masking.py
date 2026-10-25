"""2026-10 notification-masking audit, Wave 2 bundled item —
`actions_live.py`'s two `RuntimeError`s raised when the AlgoOrder
pre-persist write fails (`_action_place_order` and
`_action_live_close_position`) embedded the raw account via `{account}`.
Mask at the source — these can surface through `write_audit_event`'s
summary text, which is not a logger call Wave 1's centralized filter
would catch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZJ6294"


@pytest.mark.asyncio
async def test_place_order_pre_persist_failure_masks_account():
    import backend.api.algo.actions_live as actions_live

    agent = MagicMock(slug="test-agent", id=1)
    params = {
        "account": RAW_ACCOUNT, "symbol": "NIFTY26OCTFUT", "exchange": "NFO",
        "side": "BUY", "quantity": 50, "price": 100.0, "product": "NRML",
    }

    with patch.object(actions_live, "run_preflight", AsyncMock(return_value={"ok": True})), \
         patch.object(actions_live, "_place_order_write_intent", AsyncMock(return_value=None)), \
         patch.object(actions_live, "_on_algo_order_write_failure", AsyncMock()):
        with pytest.raises(RuntimeError) as ei:
            await actions_live._action_place_order(agent, {}, params)

    msg = str(ei.value)
    assert RAW_ACCOUNT not in msg
    assert mask_account(RAW_ACCOUNT) in msg


@pytest.mark.asyncio
async def test_close_position_pre_persist_failure_masks_account():
    import backend.api.algo.actions as actions
    import backend.api.algo.actions_live as actions_live

    agent = MagicMock(slug="test-agent", id=1)
    params = {
        "account": RAW_ACCOUNT, "symbol": "NIFTY26OCTFUT", "exchange": "NFO",
        "side": "SELL", "quantity": 50, "price": 100.0, "product": "NRML",
    }

    with patch.object(actions_live, "_al_close_resolve_params",
                       return_value=(RAW_ACCOUNT, "NIFTY26OCTFUT", "NFO",
                                     50, "SELL", "NRML")), \
         patch.object(actions_live, "_al_close_resolve_position",
                      AsyncMock(return_value=("SELL", 50, "NRML"))), \
         patch.object(actions_live, "_al_close_fetch_broker_ltp",
                      AsyncMock(return_value=(MagicMock(), 100.0))), \
         patch.object(actions_live, "run_preflight", AsyncMock(return_value={"ok": True})), \
         patch.object(actions, "_write_live_order", AsyncMock(return_value=None)), \
         patch.object(actions_live, "_on_algo_order_write_failure", AsyncMock()):
        with pytest.raises(RuntimeError) as ei:
            await actions_live._action_live_close_position(agent, {}, params)

    msg = str(ei.value)
    assert RAW_ACCOUNT not in msg
    assert mask_account(RAW_ACCOUNT) in msg
