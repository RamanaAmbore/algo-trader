"""P1 fix (2026-10): the chase aggressiveness level (L/M/H) an order was
originally placed with must survive a hold/release cycle, instead of every
release path resuming at a hardcoded MED tuple regardless of the original
tier.

Write side: `orders_helpers._live_chase_config` stamps the normalised
low/med/high tag onto `ChaseConfig.level`; `chase.py:_ch_hold_on_repeated_
rejection` persists it into `hold_json["price_policy"]` (e.g.
`"CHASE_HIGH"`) when a chase gets held after repeated rejection.

Read side: `order_release._chase_level_from_price_policy` maps that tag
back to a level string; `release_held_order` / `release_repeated_rejection_
hold` thread it through to `_chase_released` / `_resume_chase_after_hold`,
which now build the resumed `ChaseConfig` via the SAME
`orders_helpers._live_chase_config` every other chase-starting path uses
— collapsing a third independent hardcoded copy of the L/M/H tier table.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.order_release import _chase_level_from_price_policy


# ── Unit tests for the read-side mapping helper ─────────────────────────

@pytest.mark.parametrize("tag,expected", [
    ("CHASE_HIGH", "high"),
    ("CHASE_LOW", "low"),
    ("CHASE_MED", "med"),
    ("chase_high", "high"),  # case-insensitive
])
def test_chase_level_from_price_policy_recognised_tags(tag, expected):
    assert _chase_level_from_price_policy(tag) == expected


@pytest.mark.parametrize("tag", [None, "", "n/a", "CHASE_UNKNOWN", "garbage"])
def test_chase_level_from_price_policy_defaults_to_med(tag):
    """Unknown/missing/legacy tags fall back to MED — the exact tuple
    every release path hardcoded before this fix, so legacy rows and
    expiry-close holds (which never set a level) keep today's behaviour."""
    assert _chase_level_from_price_policy(tag) == "med"


# ── Write side: _live_chase_config stamps cfg.level ─────────────────────

@pytest.mark.parametrize("aggressiveness,expected_level,expected_tuple", [
    ("high", "high", (10, 0.25, 10)),
    ("med", "med", (20, 0.10, 20)),
    ("low", "low", (30, 0.05, 30)),
])
def test_live_chase_config_stamps_level(aggressiveness, expected_level, expected_tuple):
    from backend.api.routes.orders_helpers import _live_chase_config

    cfg = _live_chase_config(aggressiveness)
    assert cfg.level == expected_level
    assert (cfg.interval_seconds, cfg.aggression_step, cfg.max_attempts) == expected_tuple


# ── Write side: _ch_hold_on_repeated_rejection persists cfg.level ───────

def _chase_mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


@pytest.mark.asyncio
async def test_hold_on_repeated_rejection_persists_high_level_into_price_policy():
    from backend.api.algo import chase as m
    from backend.api.algo.order_hold import parse_hold_record
    from backend.api.routes.orders_helpers import _live_chase_config

    cfg = _live_chase_config("high")
    assert cfg.level == "high"

    mock_row = SimpleNamespace(id=55, status="OPEN", detail="", hold_json=None)
    mock_session = _chase_mock_session(mock_row)
    result = m.ChaseResult()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch.object(m, "_ch_write_order_event", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        await m._ch_hold_on_repeated_rejection(
            result, "ZG0790", "NIFTY25CE", "BUY", 50, "OID1", cfg, 55, MagicMock(),
        )

    rec = parse_hold_record(mock_row.hold_json)
    assert rec["price_policy"] == "CHASE_HIGH"


@pytest.mark.asyncio
async def test_hold_on_repeated_rejection_defaults_to_med_without_level():
    """A cfg built WITHOUT going through `_live_chase_config` (e.g. the
    engine's own fixed cfg, or a bare `ChaseConfig()`) has `level=None` —
    must persist the default MED tag, matching pre-fix behaviour."""
    from backend.api.algo import chase as m
    from backend.api.algo.order_hold import parse_hold_record

    cfg = m.ChaseConfig(interval_seconds=0, max_attempts=5, exchange="NFO")
    assert cfg.level is None

    mock_row = SimpleNamespace(id=56, status="OPEN", detail="", hold_json=None)
    mock_session = _chase_mock_session(mock_row)
    result = m.ChaseResult()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch.object(m, "_ch_write_order_event", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        await m._ch_hold_on_repeated_rejection(
            result, "ZG0790", "NIFTY25CE", "BUY", 50, "OID2", cfg, 56, MagicMock(),
        )

    rec = parse_hold_record(mock_row.hold_json)
    assert rec["price_policy"] == "CHASE_MED"


# ── End-to-end: place HIGH, hold, release → resumed chase IS HIGH ──────

def _release_mock_session(row):
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


@pytest.mark.asyncio
async def test_release_repeated_rejection_hold_resumes_at_original_high_tier():
    """The literal 'place HIGH, hold, release' scenario: a row held after
    repeated rejection, originally chased at HIGH, must resume at the HIGH
    tier (10s/0.25/10 attempts) on release — not the hardcoded MED tuple."""
    from backend.api.algo import chase as chase_mod
    from backend.api.algo import order_release as m
    from backend.api.routes.orders_helpers import _live_chase_config

    # Step 1: simulate the ORIGINAL placement building a HIGH-tier cfg,
    # and the chase getting held after repeated rejection — this is what
    # actually writes hold_json["price_policy"] = "CHASE_HIGH".
    original_cfg = _live_chase_config("high", intent=None, product="NRML")
    original_cfg.exchange = "NFO"
    hold_row = SimpleNamespace(id=77, status="OPEN", detail="", hold_json=None)
    hold_session = _chase_mock_session(hold_row)
    chase_result = chase_mod.ChaseResult()

    with patch.object(chase_mod, "_async_session", return_value=hold_session), \
         patch.object(chase_mod, "_ch_write_order_event", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        await chase_mod._ch_hold_on_repeated_rejection(
            chase_result, "ZG0790", "NIFTY24APR25000CE", "BUY", 50,
            "OID_ORIG", original_cfg, 77, MagicMock(),
        )

    assert hold_row.status == "HELD"
    assert "CHASE_HIGH" in hold_row.hold_json

    # Step 2: release the held row (same id, same persisted hold_json) and
    # assert the resumed chase is configured at the HIGH tier.
    held_row = SimpleNamespace(
        id=77, status="HELD", hold_json=hold_row.hold_json, detail="",
        account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="BUY", quantity=50, product="NRML",
        intent=None, filled_quantity=0,
    )
    release_session = _release_mock_session(held_row)
    mock_chase_order = AsyncMock()

    with patch("backend.api.database.async_session", return_value=release_session), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order), \
         patch("backend.api.algo.chase._ch_mark_chase_active"), \
         patch("backend.api.algo.chase._ch_mark_chase_inactive"):
        result = await m.release_repeated_rejection_hold(77, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is True
    mock_chase_order.assert_called_once()
    kw = mock_chase_order.call_args.kwargs
    cfg = kw["cfg"]
    assert (cfg.interval_seconds, cfg.aggression_step, cfg.max_attempts) == (10, 0.25, 10)
    assert cfg.exchange == "NFO"
    assert cfg.product == "NRML"


@pytest.mark.asyncio
async def test_release_held_order_resumes_expiry_close_at_med_default():
    """`release_held_order` (expiry-close path) never had an operator-chosen
    level to begin with (`price_policy` defaults to `"CHASE_MED"`) — must
    keep resuming at MED, proving the refactor onto `_live_chase_config`
    didn't change the default-case behaviour."""
    import pandas as pd
    from datetime import datetime, timezone
    from backend.api.algo import order_release as m
    from backend.api.algo.order_hold import HoldCategory, hold_record

    row = SimpleNamespace(
        id=501, status="HELD",
        hold_json=hold_record(HoldCategory.EXPIRY_CLOSE, "expiry close",
                              "CHASE_MED", None, datetime.now(timezone.utc)),
        detail="", account="ZG0790", symbol="CRUDEOIL26OCT8600CE", exchange="MCX",
        transaction_type="SELL", quantity=100, product="NRML",
    )
    mock_session = _release_mock_session(row)
    frame = pd.DataFrame([{
        "tradingsymbol": "CRUDEOIL26OCT8600CE", "exchange": "MCX",
        "quantity": 100, "multiplier": 100,
    }])
    mock_broker = MagicMock()
    mock_broker.quote.return_value = {
        "MCX:CRUDEOIL26OCT8600CE": {
            "depth": {"buy": [{"price": 100.0}], "sell": [{"price": 100.2}]},
            "last_price": 100.1,
            "lower_circuit_limit": 50.0,
            "upper_circuit_limit": 200.0,
        }
    }
    mock_chase_order = AsyncMock()

    with patch("backend.api.database.async_session", return_value=mock_session), \
         patch("backend.brokers.get_broker", return_value=mock_broker), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[frame]), \
         patch("backend.api.routes.orders_helpers._ensure_tick_index", new_callable=AsyncMock), \
         patch.dict("backend.api.routes.orders_helpers._TICK_INDEX",
                    {("MCX", "CRUDEOIL26OCT8600CE"): 0.1}, clear=True), \
         patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock), \
         patch("backend.api.algo.chase.chase_order", mock_chase_order):
        result = await m.release_held_order(501, actor="operator")
        await asyncio.sleep(0)

    assert result["ok"] is True
    mock_chase_order.assert_called_once()
    cfg = mock_chase_order.call_args.kwargs["cfg"]
    assert (cfg.interval_seconds, cfg.aggression_step, cfg.max_attempts) == (20, 0.10, 20)
    assert cfg.exchange == "MCX"
    assert cfg.intent == "close"
