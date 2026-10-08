"""
Regression test for the 2026-10 fix: the live `place_order` agent action
only ever read `params.template_id` — `template_slug` and the four
`*_override` params (`tp_pct_override` / `sl_pct_override` /
`wing_premium_pct_override` / `wing_strike_offset_override`), all
advertised as valid `place_order` params in
`backend/config/grammars/agent_grammar.yaml`, were silently ignored on
the LIVE path even though the paper/sim path (via
`_maybe_attach_template_from_action` → `apply_template_to_order`) honours
all of them. An agent configured with `template_slug` (the way the
Automation page's own Bracket picker writes it) got a naked live entry
with zero exits, with no error anywhere.

Fix: `backend/api/algo/actions_live.py`
  - `_al_place_resolve_params` now also reads `params.template_slug`.
  - `_place_order_resolve_template_slug` resolves a slug to its
    `OrderTemplate.id` via the SAME shared resolver
    (`template_attach.load_template_for_slug_or_id`) every other
    template-attach path already uses, with `template_id` winning when
    both are set (matches that resolver's own id-over-slug priority).
  - `_place_order_overrides_json` serialises the four override params
    into the SAME JSON shape (`tp_pct`/`sl_pct`/`wing_premium_pct`/
    `wing_strike_offset`) the fill-time reader
    (`orders_place._opp_load_row_for_attach`) already parses back out of
    `AlgoOrder.template_overrides_json` for OrderTicket/basket orders —
    so no change was needed on the fill-time consumer side.
  - `_place_order_set_product_template` now persists both the resolved
    `template_id` and the overrides JSON on the AlgoOrder row.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.actions_live import (
    _al_place_build_overrides,
    _al_place_resolve_params,
    _place_order_overrides_json,
    _place_order_resolve_template_slug,
    _place_order_set_product_template,
)


# ---------------------------------------------------------------------------
# _al_place_resolve_params — template_slug extraction
# ---------------------------------------------------------------------------

def test_resolve_params_reads_template_slug_when_template_id_absent():
    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
        "side": "SELL", "quantity": 50,
        "template_slug": "default-bull",
    }
    (*_rest, template_id, template_slug) = _al_place_resolve_params(agent, {}, params)
    assert template_id is None
    assert template_slug == "default-bull"


def test_resolve_params_template_id_present_slug_also_returned():
    """template_id wins downstream, but both values are returned here —
    the priority decision belongs to _place_order_set_product_template."""
    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
        "side": "SELL", "quantity": 50,
        "template_id": 9, "template_slug": "default-bull",
    }
    (*_rest, template_id, template_slug) = _al_place_resolve_params(agent, {}, params)
    assert template_id == 9
    assert template_slug == "default-bull"


# ---------------------------------------------------------------------------
# _al_place_build_overrides
# ---------------------------------------------------------------------------

def test_build_overrides_extracts_all_four_override_keys():
    params = {
        "tp_pct_override": 25.0,
        "sl_pct_override": -15.0,
        "wing_premium_pct_override": 10.0,
        "wing_strike_offset_override": 500,
    }
    overrides = _al_place_build_overrides(params)
    assert overrides == {
        "tp_pct": 25.0, "sl_pct": -15.0,
        "wing_premium_pct": 10.0, "wing_strike_offset": 500,
    }


def test_build_overrides_returns_none_when_nothing_supplied():
    assert _al_place_build_overrides({"account": "ZG0790"}) is None


def test_build_overrides_legacy_target_pct_mapping():
    """Backward-compat: target_pct (fractional) maps to tp_pct (% units)
    when tp_pct_override is absent — same as the sim-path helper."""
    overrides = _al_place_build_overrides({"target_pct": 0.30})
    assert overrides == {"tp_pct": 30.0}


# ---------------------------------------------------------------------------
# _place_order_overrides_json
# ---------------------------------------------------------------------------

def test_overrides_json_serializes_only_non_none_keys():
    out = _place_order_overrides_json({"tp_pct": 25.0, "sl_pct": None,
                                        "wing_premium_pct": None,
                                        "wing_strike_offset": None})
    assert json.loads(out) == {"tp_pct": 25.0}


def test_overrides_json_returns_none_for_empty_or_all_none():
    assert _place_order_overrides_json(None) is None
    assert _place_order_overrides_json({}) is None
    assert _place_order_overrides_json({"tp_pct": None}) is None


# ---------------------------------------------------------------------------
# _place_order_resolve_template_slug
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_template_slug_found_returns_id():
    mock_loader = AsyncMock(return_value={"id": 7, "slug": "default-bull"})
    with patch("backend.api.algo.template_attach.load_template_for_slug_or_id",
               new=mock_loader):
        result = await _place_order_resolve_template_slug("default-bull")
    assert result == 7
    mock_loader.assert_awaited_once_with(template_id=None, template_slug="default-bull")


@pytest.mark.asyncio
async def test_resolve_template_slug_not_found_returns_none():
    mock_loader = AsyncMock(return_value=None)
    with patch("backend.api.algo.template_attach.load_template_for_slug_or_id",
               new=mock_loader), \
         patch("backend.api.algo.actions_live.logger") as mock_logger:
        result = await _place_order_resolve_template_slug("no-such-slug")
    assert result is None
    mock_logger.warning.assert_called()


@pytest.mark.asyncio
async def test_resolve_template_slug_lookup_exception_returns_none():
    mock_loader = AsyncMock(side_effect=RuntimeError("db down"))
    with patch("backend.api.algo.template_attach.load_template_for_slug_or_id",
               new=mock_loader):
        result = await _place_order_resolve_template_slug("default-bull")
    assert result is None


# ---------------------------------------------------------------------------
# _place_order_set_product_template — persists resolved slug + overrides
# ---------------------------------------------------------------------------

def _mock_session_capturing_execute():
    """AsyncMock session whose .execute() captures the statement passed in."""
    captured: dict = {}

    async def _fake_execute(stmt):
        captured["stmt"] = stmt
        return MagicMock()

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(side_effect=_fake_execute)
    session.commit = AsyncMock()
    return session, captured


@pytest.mark.asyncio
async def test_set_product_template_resolves_slug_and_persists_overrides():
    """End-to-end for the DB-write half of the fix: template_id is None,
    template_slug resolves to id=7, and overrides get JSON-serialised
    onto the SAME row update."""
    session, captured = _mock_session_capturing_execute()
    mock_loader = AsyncMock(return_value={"id": 7, "slug": "default-bull"})

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.template_attach.load_template_for_slug_or_id",
               new=mock_loader):
        await _place_order_set_product_template(
            42, "NRML", None,
            template_slug="default-bull",
            overrides={"tp_pct": 25.0, "sl_pct": -15.0,
                       "wing_premium_pct": None, "wing_strike_offset": None},
        )

    session.commit.assert_awaited_once()
    params = captured["stmt"].compile().params
    assert params["template_id"] == 7
    assert params["product"] == "NRML"
    assert json.loads(params["template_overrides_json"]) == {
        "tp_pct": 25.0, "sl_pct": -15.0,
    }


@pytest.mark.asyncio
async def test_set_product_template_id_wins_over_slug():
    """template_id takes priority — the slug resolver must not even be
    consulted when template_id is already set."""
    session, captured = _mock_session_capturing_execute()
    mock_loader = AsyncMock(return_value={"id": 999, "slug": "default-bull"})

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.template_attach.load_template_for_slug_or_id",
               new=mock_loader):
        await _place_order_set_product_template(
            42, "NRML", 5, template_slug="default-bull", overrides=None,
        )

    mock_loader.assert_not_awaited()
    params = captured["stmt"].compile().params
    assert params["template_id"] == 5


@pytest.mark.asyncio
async def test_set_product_template_unresolvable_slug_leaves_template_id_null():
    """A slug that doesn't resolve to any OrderTemplate must not block
    the product-code write, and must not set template_id at all."""
    session, captured = _mock_session_capturing_execute()
    mock_loader = AsyncMock(return_value=None)

    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.template_attach.load_template_for_slug_or_id",
               new=mock_loader):
        await _place_order_set_product_template(
            42, "NRML", None, template_slug="no-such-slug", overrides=None,
        )

    session.commit.assert_awaited_once()
    params = captured["stmt"].compile().params
    assert "template_id" not in params
    assert params["product"] == "NRML"


# ---------------------------------------------------------------------------
# Integration smoke — _action_place_order with ONLY template_slug set
# (mirrors test_actions.py's test_action_place_order_ltp_fetched_via_helper,
# but leaves _place_order_set_product_template UNPATCHED so the real
# slug-resolution + DB-persist path runs end to end.)
# ---------------------------------------------------------------------------

def _make_conns_stub(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


def _make_broker_stub(*, ltp_value: float = 23500.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [{"initial": {"total": 5_000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500_000.0},
        "commodity": {"enabled": True, "net": 500_000.0},
    }
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": ltp_value}}
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


@pytest.mark.asyncio
async def test_action_place_order_with_template_slug_only_resolves_and_attaches():
    from backend.api.algo.actions import _action_place_order

    broker = _make_broker_stub()
    conns = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 7
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "NIFTY25JULFUT",
        "exchange": "NFO",
        "transaction_type": "SELL",
        "quantity": 50,
        "template_slug": "default-bull",
        "tp_pct_override": 25.0,
    }

    mock_chase = AsyncMock()
    session, captured = _mock_session_capturing_execute()
    mock_loader = AsyncMock(return_value={"id": 11, "slug": "default-bull"})

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.algo.template_attach.load_template_for_slug_or_id",
               new=mock_loader), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_place_order(agent, context, params)

    mock_chase.assert_called_once()
    mock_loader.assert_awaited_once_with(template_id=None, template_slug="default-bull")
    params_written = captured["stmt"].compile().params
    assert params_written["template_id"] == 11
    assert json.loads(params_written["template_overrides_json"]) == {"tp_pct": 25.0}
