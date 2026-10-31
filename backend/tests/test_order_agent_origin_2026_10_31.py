"""2026-10-31 — operator: "order should have manual as agent, if triggered
manually or agent name if triggered by agent" (the "Origin" chip fix
`AlgoOrderInfo.agent_id`'s own comment already flagged as upcoming, see
test_algo_order_info_broker_fields.py).

`AlgoOrderInfo.agent_origin` resolves via the exact same SSOT every alert's
own origin line already uses (`alert_utils.resolve_origin()`), so an
order's displayed agent can never disagree with that order's own alert
text. `_fetch_agent_name_map` is the batch {agent_id: name} lookup the two
live call sites (`list_algo_orders`, `list_active_chases`) use so
`resolve_origin` gets a real name instead of falling back to the generic
"Agent #{id}" form.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.routes.orders import _chase_row_to_info, _fetch_agent_name_map
from backend.tests.test_algo_order_info_broker_fields import _make_row, _identity_mask


def test_no_agent_id_resolves_to_manual():
    row = _make_row(agent_id=None)
    info = _chase_row_to_info(row, _identity_mask, {})
    assert info.agent_origin == "manual"


def test_default_field_value_is_manual():
    """The Struct's own default (no row/caller involved at all) must also
    be 'manual', not None or '' — every order renders an agent chip."""
    from backend.api.routes.orders_helpers import AlgoOrderInfo
    info = AlgoOrderInfo(
        id=1, account="ZG0790", symbol="NIFTY24DECFUT", exchange="NFO",
        transaction_type="BUY", quantity=50, initial_price=100.0,
        attempts=0, status="OPEN", engine="live", mode="live",
        detail=None, created_at="",
    )
    assert info.agent_origin == "manual"


def test_real_agent_id_with_name_map_resolves_to_real_name():
    row = _make_row(agent_id=7)
    info = _chase_row_to_info(row, _identity_mask, {}, agent_name_map={7: "loss-funds-negative"})
    assert info.agent_origin == "loss-funds-negative"


def test_real_agent_id_without_name_map_entry_falls_back_to_agent_hash_id():
    """A real, non-sentinel agent_id with no matching name (e.g. the
    firing agent was since deleted) must render an honest 'unresolved but
    real' label — never silently collapse to 'manual'."""
    row = _make_row(agent_id=42)
    info = _chase_row_to_info(row, _identity_mask, {}, agent_name_map={})
    assert info.agent_origin == "Agent #42"


def test_agent_name_matching_manual_sentinel_resolves_to_manual():
    """resolve_origin()'s own name-based manual-sentinel check — a row
    whose resolved name IS the literal 'Manual operator order' row name
    (case-insensitive) must render 'manual', not that literal string."""
    row = _make_row(agent_id=98)
    info = _chase_row_to_info(
        row, _identity_mask, {}, agent_name_map={98: "Manual operator order"},
    )
    assert info.agent_origin == "manual"


@pytest.mark.asyncio
async def test_fetch_agent_name_map_empty_ids_skips_query():
    session = MagicMock()
    session.execute = AsyncMock()
    result = await _fetch_agent_name_map(session, [None, None])
    assert result == {}
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_agent_name_map_dedupes_and_drops_none():
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.all.return_value = [(7, "agent-seven"), (9, "agent-nine")]
    session.execute = AsyncMock(return_value=exec_result)
    result = await _fetch_agent_name_map(session, [7, None, 9, 7])
    assert result == {7: "agent-seven", 9: "agent-nine"}
    session.execute.assert_awaited_once()
