"""P1 fix (2026-10 audit) — `_resolve_resumed_chase_agent_id` collapses the
"manual" sentinel agent id back to `None` so a resumed chase for an
ORIGINALLY-MANUAL order produces a `[Manual]`-labeled failure alert, not
`[Agent]`.

`backend.shared.helpers.alert_utils._classify_order_origin_label(source,
agent_id)` treats `agent_id is not None` as "this order was agent-fired" —
a manually-placed order's `AlgoOrder.agent_id` is the real id of the
synthetic "manual" Agent row, NOT None, so the raw row value must never
be threaded straight through to a resumed `chase_order()` call (which
ultimately feeds any eventual `send_order_failure_alert(agent_id=...)`
call).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.shared.helpers.alert_utils import _classify_order_origin_label


@pytest.mark.asyncio
async def test_resolve_resumed_chase_agent_id_collapses_manual_sentinel():
    from backend.api.algo.order_release import _resolve_resumed_chase_agent_id

    with patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
               new=AsyncMock(return_value=99)):
        resolved = await _resolve_resumed_chase_agent_id(99)

    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_resumed_chase_agent_id_preserves_real_agent_id():
    from backend.api.algo.order_release import _resolve_resumed_chase_agent_id

    with patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
               new=AsyncMock(return_value=99)):
        resolved = await _resolve_resumed_chase_agent_id(7)

    assert resolved == 7


@pytest.mark.asyncio
async def test_resolve_resumed_chase_agent_id_none_input_short_circuits():
    """A row with no agent_id at all (already None) must never trigger
    the manual-agent-id DB lookup at all."""
    from backend.api.algo.order_release import _resolve_resumed_chase_agent_id

    mock_lookup = AsyncMock(return_value=99)
    with patch("backend.api.algo.agent_engine.get_agent_id_by_slug", mock_lookup):
        resolved = await _resolve_resumed_chase_agent_id(None)

    assert resolved is None
    mock_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_resolved_manual_agent_id_feeds_manual_label_not_agent():
    """End-to-end: the resolved value from `_resolve_resumed_chase_agent_id`
    for an originally-manual row, fed into `_classify_order_origin_label`,
    must produce "Manual" (not "Agent") — the exact label shown as
    `[{origin_label}]` in a failure alert's title/subject."""
    from backend.api.algo.order_release import _resolve_resumed_chase_agent_id

    with patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
               new=AsyncMock(return_value=99)):
        resolved_manual = await _resolve_resumed_chase_agent_id(99)
        resolved_agent = await _resolve_resumed_chase_agent_id(7)

    assert _classify_order_origin_label("chase", resolved_manual) == "Manual"
    assert _classify_order_origin_label("chase", resolved_agent) == "Agent"
