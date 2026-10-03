"""Sprint 2a fix — the real MCP cancel_order route
(`backend/api/routes/research.py:_res_cancel_paper`) must explicitly pass
`source="mcp"` to `cancel_paper_order`, executing the actual fixed line
(not just a source-text inspection)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes.research import _res_cancel_paper


@pytest.mark.asyncio
async def test_res_cancel_paper_calls_engine_with_source_mcp():
    mock_engine = MagicMock()
    mock_engine.cancel_paper_order = MagicMock(return_value=True)
    audit_fn = AsyncMock()

    with patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine), \
         patch("backend.shared.helpers.alert_utils._send_telegram"):
        resp = await _res_cancel_paper(
            oid="42", acct="ZG0790", request_id="req-1", user_id=1,
            audit_fn=audit_fn,
        )

    mock_engine.cancel_paper_order.assert_called_once_with(42, source="mcp")
    assert resp.order_id == "42"
    assert resp.detail == "cancelled (paper)"


@pytest.mark.asyncio
async def test_res_cancel_paper_404_when_not_found():
    from litestar.exceptions import HTTPException

    mock_engine = MagicMock()
    mock_engine.cancel_paper_order = MagicMock(return_value=False)
    audit_fn = AsyncMock()

    with patch("backend.api.algo.paper.get_prod_paper_engine", return_value=mock_engine):
        with pytest.raises(HTTPException) as ei:
            await _res_cancel_paper(
                oid="99", acct="ZG0790", request_id="req-2", user_id=1,
                audit_fn=audit_fn,
            )

    assert ei.value.status_code == 404
    mock_engine.cancel_paper_order.assert_called_once_with(99, source="mcp")
