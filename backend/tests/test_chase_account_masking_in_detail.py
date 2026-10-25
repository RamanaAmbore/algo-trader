"""2026-10 notification-masking audit, Wave 2 bundled item —
`chase.py`'s two `result.detail` sites that literally interpolate
`{account}` (`_ch_build_cancel_unconfirmed_abort` and the operator-kill
post-replace path inside `_ch_post_replace_kill_check`) built the message
with the raw account. `result.detail` reaches a plain `RuntimeError`
re-raise in `actions_live.py` with no further masking — mask at the
source, same pattern as the pre-existing `_ch_abort_ambiguous_place_timeout`
fix (O11).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.api.algo.chase as chase
from backend.api.algo.chase import ChaseConfig, ChaseResult, ChaseStatus
from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZJ6294"


@pytest.mark.asyncio
async def test_cancel_unconfirmed_abort_masks_account_in_detail():
    result = ChaseResult()
    emit = MagicMock()
    with patch.object(chase, "_emit_chase_terminal", AsyncMock()), \
         patch.object(chase, "_ch_mark_ambiguous_abort"):
        out = await chase._ch_build_cancel_unconfirmed_abort(
            result, "NIFTY25JULFUT", RAW_ACCOUNT, "BUY", 50, 50, 2,
            "OID1", algo_order_id=7, emit=emit,
        )

    assert out.status == ChaseStatus.FAILED
    assert RAW_ACCOUNT not in out.detail
    assert mask_account(RAW_ACCOUNT) in out.detail


@pytest.mark.asyncio
async def test_post_replace_kill_check_failed_cancel_masks_account_in_detail():
    result = ChaseResult()
    emit = MagicMock()
    cfg = ChaseConfig(exchange="NFO")

    with patch.object(chase, "is_killed", return_value=True), \
         patch.object(chase, "is_order_killed", return_value=False), \
         patch.object(chase, "_run", AsyncMock(side_effect=RuntimeError("cancel failed"))), \
         patch.object(chase, "_ch_mark_ambiguous_abort"), \
         patch.object(chase, "_emit_chase_terminal", AsyncMock()):
        killed = await chase._ch_post_replace_kill_check(
            RAW_ACCOUNT, "OID1", cfg, result, "NIFTY25JULFUT", "BUY", 50,
            2, 7, emit,
        )

    assert killed is True
    assert result.status == ChaseStatus.FAILED
    assert RAW_ACCOUNT not in result.detail
    assert mask_account(RAW_ACCOUNT) in result.detail
