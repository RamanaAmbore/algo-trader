"""
Regression test for the 2026-09-28 audit fix — a failed AlgoOrder
pre-persist (`_ticket_persist_live_algo_order` returning None) used to
be treated as best-effort: the live order was placed at the broker
anyway with algo_order_id=None, running the ENTIRE chase loop untracked
(no attempts/last_attempt_at/next_attempt_at/current_limit ever
written, since _sync_algo_order_id no-ops when algo_order_id is None)
and skipping TP-arm / template-attach / FIFO ledger write entirely on
fill (_chase_terminal_update_db's fallback lookup finds no row either).

Root-caused live via AlgoOrder #1088 on prod (2026-09-28): a real
chase-mode order sat ~12 minutes with attempts=0, every timing field
NULL, and request_id=None — the row that eventually appeared was a
postback-created orphan (backend/api/routes/orders_postback.py's
M2(c)), not the original tracking row.

Fix: `_ticket_place_live` (backend/api/routes/orders_place.py) now
fails closed — raises HTTPException(503) immediately when
_ticket_persist_live_algo_order returns None, BEFORE ever calling
_ticket_place_or_chase_live (i.e. before any broker call happens).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException


def _fake_data(**over):
    base = dict(
        mode="live", side="BUY", tradingsymbol="NIFTY25JUL24000CE",
        quantity=1, exchange="NFO", account="ZG0790",
        order_type="LIMIT", price=100.0, chase=True,
        chase_aggressiveness="low", product="NRML", variety="regular",
        target_pct=None, template_id=None, strategy_id=None,
    )
    base.update(over)
    return MagicMock(**base)


@pytest.mark.asyncio
async def test_ticket_place_live_aborts_when_pre_persist_returns_none():
    from backend.api.routes import orders_place as m

    data = _fake_data()
    request = MagicMock()

    with patch.object(m, "_opp_live_check_mode_gates", return_value="bk-key"), \
         patch.object(m, "_ticket_run_preflight", new=AsyncMock(return_value={"ok": True})), \
         patch.object(m, "_ticket_check_mcx_size_cap"), \
         patch.object(m, "_ticket_persist_live_algo_order",
                       new=AsyncMock(return_value=None)) as mock_persist, \
         patch.object(m, "_ticket_place_or_chase_live",
                       new=AsyncMock()) as mock_place_or_chase:
        with pytest.raises(HTTPException) as exc_info:
            await m._ticket_place_live(
                data, request, "ZG0790", "NIFTY25JUL24000CE", "BUY", 50, 50,
            )

    mock_persist.assert_awaited_once()
    # The whole point: no broker call must ever happen when the
    # tracking row couldn't be created.
    mock_place_or_chase.assert_not_called()
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_ticket_place_live_proceeds_when_pre_persist_succeeds():
    from backend.api.routes import orders_place as m

    data = _fake_data()
    request = MagicMock()

    with patch.object(m, "_opp_live_check_mode_gates", return_value="bk-key"), \
         patch.object(m, "_ticket_run_preflight", new=AsyncMock(return_value={"ok": True})), \
         patch.object(m, "_ticket_check_mcx_size_cap"), \
         patch.object(m, "_ticket_persist_live_algo_order",
                       new=AsyncMock(return_value=42)) as mock_persist, \
         patch.object(m, "_ticket_place_or_chase_live",
                       new=AsyncMock(return_value=("bo-1", True))) as mock_place_or_chase, \
         patch.object(m, "_ticket_seed_broker_order_id", new=AsyncMock()), \
         patch.object(m, "_opp_live_handle_success",
                       new=AsyncMock(return_value="ok")) as mock_success:
        result = await m._ticket_place_live(
            data, request, "ZG0790", "NIFTY25JUL24000CE", "BUY", 50, 50,
        )

    mock_persist.assert_awaited_once()
    mock_place_or_chase.assert_awaited_once()
    mock_success.assert_awaited_once()
    assert result == "ok"
