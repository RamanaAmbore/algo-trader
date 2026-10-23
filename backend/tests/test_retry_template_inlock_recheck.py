"""test_retry_template_inlock_recheck.py

Covers audit finding T13 (2026-10-23 template/bracket audit, cluster 2A):
`_rco_run_template_attach` (backend/api/routes/orders.py) never re-checked
`attached_gtts_json` INSIDE its own per-row lock. The retry route's own
precheck (`_retry_precheck_row`) reads `attached_gtts_json` BEFORE the lock
is acquired — if a concurrent fill-triggered attach
(`_fire_template_attach_on_fill`, orders_place.py) wins the lock first and
persists `attached_gtts_json` in the window between that precheck and the
retry path actually acquiring the lock, the retry path used to proceed to
call `apply_template_to_order` a SECOND time, placing a duplicate live
GTT/wing set and orphaning the first one.

Fix: `_rco_run_template_attach` now re-reads `attached_gtts_json` fresh
from the DB (via `_rco_attached_gtts_already_set`) INSIDE the lock and
bails with `None` before ever calling `apply_template_to_order`, mirroring
the fill-triggered path's own in-lock idempotency check
(`orders_place.py:_opp_load_row_for_attach`).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_retry_attach_skips_when_concurrent_fill_won_the_lock_first():
    """Pre-fix: this test fails because apply_template_to_order is called
    even though attached_gtts_json was set by a 'concurrent' attach that
    landed while this call waited on the lock — the old code never
    re-checked inside the lock."""
    from backend.api.routes import orders as orders_mod

    row = MagicMock()
    row.id = 4242
    row.template_id = 11
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "SELL"
    row.product = "NRML"
    row.mode = "live"
    row.fill_price = 100.0
    row.initial_price = 100.0
    row.template_overrides_json = None

    apply_template_to_order_mock = AsyncMock(return_value=MagicMock())

    with patch.object(orders_mod, "_get_template_attach_lock",
                       new=AsyncMock(return_value=asyncio.Lock())), \
         patch.object(orders_mod, "_rco_attached_gtts_already_set",
                      new=AsyncMock(return_value=True)), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=apply_template_to_order_mock), \
         patch.object(orders_mod, "_retry_effective_parent_qty",
                      new=AsyncMock(return_value=50)):
        result = await orders_mod._rco_run_template_attach(row)

    assert result is None, (
        "retry-attach must bail with None when a concurrent fill-triggered "
        "attach already set attached_gtts_json while this call waited for "
        "the per-row lock"
    )
    apply_template_to_order_mock.assert_not_called()


@pytest.mark.asyncio
async def test_retry_attach_proceeds_when_not_concurrently_attached():
    """Sanity: the normal (non-race) path still calls through when the
    in-lock re-check finds attached_gtts_json still unset."""
    from backend.api.routes import orders as orders_mod

    row = MagicMock()
    row.id = 4243
    row.template_id = 11
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "SELL"
    row.product = "NRML"
    row.mode = "live"
    row.fill_price = 100.0
    row.initial_price = 100.0
    row.template_overrides_json = None

    sentinel_result = MagicMock()
    apply_template_to_order_mock = AsyncMock(return_value=sentinel_result)

    with patch.object(orders_mod, "_get_template_attach_lock",
                       new=AsyncMock(return_value=asyncio.Lock())), \
         patch.object(orders_mod, "_rco_attached_gtts_already_set",
                      new=AsyncMock(return_value=False)), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=apply_template_to_order_mock), \
         patch.object(orders_mod, "_retry_effective_parent_qty",
                      new=AsyncMock(return_value=50)):
        result = await orders_mod._rco_run_template_attach(row)

    assert result is sentinel_result
    apply_template_to_order_mock.assert_called_once()


@pytest.mark.asyncio
async def test_rco_attached_gtts_already_set_reads_fresh_from_db():
    """_rco_attached_gtts_already_set must query the DB directly (not the
    caller's possibly-stale in-memory row object) and return a plain bool."""
    from backend.api.routes import orders as orders_mod

    fake_session = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none.return_value = '[{"kind": "gtt", "id": "1"}]'
    fake_session.execute = AsyncMock(return_value=fake_result)

    fake_session_cm = MagicMock()
    fake_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.database.async_session",
               return_value=fake_session_cm):
        out = await orders_mod._rco_attached_gtts_already_set(4242)

    assert out is True

    fake_result.scalar_one_or_none.return_value = None
    with patch("backend.api.database.async_session",
               return_value=fake_session_cm):
        out2 = await orders_mod._rco_attached_gtts_already_set(4243)
    assert out2 is False
