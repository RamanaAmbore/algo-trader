"""P1 fix (2026-10 audit, Group B item 6) — `_res_place_telegram_ping`
(backend/api/routes/lab.py) built its MCP "place_order" Telegram ping
with the raw, unmasked account code (`acct={acct}`). Mask it with the
same `mask_account()` helper every other correctly-masked alert path
already uses.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_place_telegram_ping_masks_account():
    from backend.api.routes import lab

    captured: list[str] = []
    res = MagicMock(mode="live", order_id="O123", status="PLACED")

    with patch.object(lab, "_mcp_ping", side_effect=lambda tg: captured.append(tg)), \
         patch.object(lab, "_audit_link_html", return_value="req-1"):
        lab._res_place_telegram_ping(
            "ZG0790", "BUY", 50, "NIFTY26OCTFUT",
            MagicMock(price=None, trigger_price=None), res, "req-1", 7,
        )

    assert len(captured) == 1
    assert "ZG0790" not in captured[0]
    assert "acct=ZG####" in captured[0]
