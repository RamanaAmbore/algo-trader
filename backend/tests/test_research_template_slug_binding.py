"""Confirm-token binding of place_order `template_slug`.

A token minted for a PLACE order must redeem only with the exact template
slug the operator approved. Covers:
  (a) token minted WITH a slug validates only a place_order carrying the
      SAME slug; mismatched or omitted slug is rejected with 403.
  (b) token minted WITHOUT a slug (None / "") validates exactly as before,
      and the purpose hash for the no-template case is bit-for-bit the
      pre-change value (pinned literals captured from the pre-change code).
  (c) the mint endpoint accepts and threads template_slug into the hash.

Uses the real in-process token store and real hash function; only the
template loader, audit writer, Telegram ping and ticket pipeline are patched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.routes import research as R


# Pre-change hashes, captured from HEAD before template_slug was bound.
# The no-template fingerprint must never drift — a drift would invalidate
# every token minted today for a template-less order.
_BASELINE_NO_TPL_A = "ac94242b9d0f6fffa8a8588521427c14b2b8d1998d93ae182501471b13f15e0d"
_BASELINE_NO_TPL_B = "21e687c0236e60279454100ebf386c3883e155a06d9e10b0a15d2e8562e430b5"


@pytest.fixture(autouse=True)
def _clear_token_store():
    R._confirm_tokens.clear()
    yield
    R._confirm_tokens.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Hash function — backward compatibility
# ═══════════════════════════════════════════════════════════════════════════

def test_no_template_hash_is_bit_for_bit_unchanged():
    assert R._purpose_hash_place(
        "ZG0790", "NIFTY25OCTFUT", "BUY", 1, "LIMIT", "paper", None, None,
    ) == _BASELINE_NO_TPL_A
    assert R._purpose_hash_place(
        "zg0790", " nifty25octfut ", "sell", 50, "SL", "live", 590.8, 589.5,
    ) == _BASELINE_NO_TPL_B


def test_none_empty_and_omitted_slug_all_hash_identically():
    base = R._purpose_hash_place("ZG0790", "X", "BUY", 1, "LIMIT", "paper", None, None)
    assert R._purpose_hash_place("ZG0790", "X", "BUY", 1, "LIMIT", "paper", None, None, None) == base
    assert R._purpose_hash_place("ZG0790", "X", "BUY", 1, "LIMIT", "paper", None, None, "") == base


def test_template_slug_changes_hash():
    base = R._purpose_hash_place("ZG0790", "X", "BUY", 1, "LIMIT", "paper", None, None)
    with_tpl = R._purpose_hash_place("ZG0790", "X", "BUY", 1, "LIMIT", "paper", None, None, "default-bull")
    other_tpl = R._purpose_hash_place("ZG0790", "X", "BUY", 1, "LIMIT", "paper", None, None, "default-bear")
    assert with_tpl != base
    assert other_tpl != with_tpl
    assert with_tpl != other_tpl


# ═══════════════════════════════════════════════════════════════════════════
# (c) Mint endpoint accepts and threads template_slug
# ═══════════════════════════════════════════════════════════════════════════

def _mint_request(template_slug):
    return R.MintTokenRequest(
        kind="place", account="ZG0790", tradingsymbol="NIFTY25OCTFUT",
        side="BUY", quantity=1, mode="paper", order_type="LIMIT",
        template_slug=template_slug,
    )


@pytest.mark.asyncio
async def test_mint_endpoint_threads_template_slug_into_hash():
    ctrl = R.ResearchController(owner=None)
    with patch("backend.api.routes.research._user_id", return_value=1):
        resp = await R.ResearchController.mint_confirm_token.fn(
            ctrl, data=_mint_request("default-bull"), request=MagicMock(),
        )
    expected = R._purpose_hash_place(
        "ZG0790", "NIFTY25OCTFUT", "BUY", 1, "LIMIT", "paper", None, None, "default-bull",
    )
    assert resp.purpose_hash == expected
    assert "template=default-bull" in resp.purpose
    assert resp.token in R._confirm_tokens


@pytest.mark.asyncio
async def test_mint_endpoint_without_template_keeps_baseline_hash():
    ctrl = R.ResearchController(owner=None)
    with patch("backend.api.routes.research._user_id", return_value=1):
        resp_none = await R.ResearchController.mint_confirm_token.fn(
            ctrl, data=_mint_request(None), request=MagicMock(),
        )
    assert resp_none.purpose_hash == _BASELINE_NO_TPL_A
    assert "template" not in resp_none.purpose


# ═══════════════════════════════════════════════════════════════════════════
# (a) + (b) Token redemption through place_order
# ═══════════════════════════════════════════════════════════════════════════

def _place_request(template_slug, token):
    from backend.api.routes.research import PlaceOrderRequest
    return PlaceOrderRequest(
        confirm_token=token,
        account="ZG0790",
        tradingsymbol="NIFTY25OCTFUT",
        side="BUY",
        quantity=1,
        mode="paper",
        order_type="LIMIT",
        template_slug=template_slug,
    )


class _FakeTicketResponse:
    order_id = "555"
    mode = "paper"
    status = "OPEN"
    detail = "placed"


async def _fake_ticket_order_handler(data, request):
    return _FakeTicketResponse()


def _mint_token_for(template_slug):
    ph = R._purpose_hash_place(
        "ZG0790", "NIFTY25OCTFUT", "BUY", 1, "LIMIT", "paper", None, None, template_slug,
    )
    tok, _ = R._mint_token(1, ph)
    return tok


@pytest.mark.asyncio
async def test_token_minted_with_slug_redeems_with_same_slug():
    tok = _mint_token_for("default-bull")
    ctrl = R.ResearchController(owner=None)
    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value={"id": 7, "slug": "default-bull"}),
    ), patch("backend.api.routes.research._user_id", return_value=1), \
       patch("backend.api.routes.research._res_mcp_audit", new=AsyncMock()), \
       patch("backend.api.routes.research._res_place_telegram_ping"), \
       patch("backend.api.routes.orders_place.ticket_order_handler",
             new=_fake_ticket_order_handler):
        resp = await R.ResearchController.place_order.fn(
            ctrl, data=_place_request("default-bull", tok), request=MagicMock(),
        )
    assert resp.order_id == "555"


@pytest.mark.asyncio
async def test_token_minted_with_slug_rejected_for_different_slug():
    tok = _mint_token_for("default-bull")
    ctrl = R.ResearchController(owner=None)
    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value={"id": 9, "slug": "default-bear"}),
    ), patch("backend.api.routes.research._user_id", return_value=1), \
       patch("backend.api.routes.research._res_mcp_audit", new=AsyncMock()), \
       patch("backend.api.routes.orders_place.ticket_order_handler",
             new=_fake_ticket_order_handler):
        with pytest.raises(HTTPException) as ei:
            await R.ResearchController.place_order.fn(
                ctrl, data=_place_request("default-bear", tok), request=MagicMock(),
            )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_token_minted_with_slug_rejected_when_slug_omitted():
    tok = _mint_token_for("default-bull")
    ctrl = R.ResearchController(owner=None)
    with patch("backend.api.routes.research._user_id", return_value=1), \
       patch("backend.api.routes.research._res_mcp_audit", new=AsyncMock()), \
       patch("backend.api.routes.orders_place.ticket_order_handler",
             new=_fake_ticket_order_handler):
        with pytest.raises(HTTPException) as ei:
            await R.ResearchController.place_order.fn(
                ctrl, data=_place_request(None, tok), request=MagicMock(),
            )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_token_minted_without_slug_redeems_without_slug():
    tok = _mint_token_for(None)
    ctrl = R.ResearchController(owner=None)
    with patch("backend.api.routes.research._user_id", return_value=1), \
       patch("backend.api.routes.research._res_mcp_audit", new=AsyncMock()), \
       patch("backend.api.routes.research._res_place_telegram_ping"), \
       patch("backend.api.routes.orders_place.ticket_order_handler",
             new=_fake_ticket_order_handler):
        resp = await R.ResearchController.place_order.fn(
            ctrl, data=_place_request(None, tok), request=MagicMock(),
        )
    assert resp.order_id == "555"


@pytest.mark.asyncio
async def test_token_minted_without_slug_rejected_when_slug_added():
    tok = _mint_token_for(None)
    ctrl = R.ResearchController(owner=None)
    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value={"id": 7, "slug": "default-bull"}),
    ), patch("backend.api.routes.research._user_id", return_value=1), \
       patch("backend.api.routes.research._res_mcp_audit", new=AsyncMock()), \
       patch("backend.api.routes.orders_place.ticket_order_handler",
             new=_fake_ticket_order_handler):
        with pytest.raises(HTTPException) as ei:
            await R.ResearchController.place_order.fn(
                ctrl, data=_place_request("default-bull", tok), request=MagicMock(),
            )
    assert ei.value.status_code == 403
