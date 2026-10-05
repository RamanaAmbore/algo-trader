"""Preflight margin: MCX falls back to the equity wallet when the commodity wallet is disabled."""
import asyncio
from pathlib import Path

import pytest

from backend.api.algo import actions_preflight as pf

_ROOT = Path(__file__).resolve().parents[2]


class _Broker:
    def __init__(self, payload):
        self._payload = payload

    def margins(self, segment=None):
        return self._payload


@pytest.mark.asyncio
async def test_commodity_disabled_falls_back_to_equity_wallet():
    payload = {
        "equity": {"enabled": True, "net": 4_396_449.9},
        "commodity": {"enabled": False, "net": 0},
    }
    m, err = await pf._preflight_fetch_account_margins(_Broker(payload), asyncio.get_running_loop(), "commodity")
    assert err is None
    assert m["_wallet"] == "equity"
    enabled, available = pf._preflight_resolve_available_margin((m, None), "commodity", "ZG0790")
    assert enabled is True
    assert available == 4_396_449.9


@pytest.mark.asyncio
async def test_enabled_commodity_wallet_is_used_as_is():
    payload = {
        "equity": {"enabled": True, "net": 4_000_000},
        "commodity": {"enabled": True, "net": 250_000},
    }
    m, _ = await pf._preflight_fetch_account_margins(_Broker(payload), asyncio.get_running_loop(), "commodity")
    assert m["_wallet"] == "commodity"
    assert pf._preflight_resolve_available_margin((m, None), "commodity", "ZG0790") == (True, 250_000.0)


@pytest.mark.asyncio
async def test_both_wallets_disabled_stays_disabled():
    payload = {
        "equity": {"enabled": False, "net": 0},
        "commodity": {"enabled": False, "net": 0},
    }
    m, _ = await pf._preflight_fetch_account_margins(_Broker(payload), asyncio.get_running_loop(), "commodity")
    enabled, _ = pf._preflight_resolve_available_margin((m, None), "commodity", "ZG0790")
    assert enabled is False


def test_take_profit_price_is_tick_aligned_in_source():
    src = (_ROOT / "backend/api/routes/orders_place.py").read_text(encoding="utf-8")
    assert "_align_price_to_tick(parent.exchange, parent.symbol, tp_price)" in src
    assert "round(tp_price, 2)" not in src


def test_broker_issue_upsert_uses_cast_not_double_colon():
    src = (_ROOT / "backend/api/background.py").read_text(encoding="utf-8")
    assert "CAST(:breakdown AS jsonb)" in src
    assert ":breakdown::jsonb" not in src


def test_ntfy_startup_log_does_not_include_topic():
    src = (_ROOT / "backend/api/algo/agent_engine.py").read_text(encoding="utf-8")
    assert "ntfy configured (topic=" not in src
