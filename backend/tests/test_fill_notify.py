"""Fill records: each FILLED live or paper fill is logged once, tagged orders, with its mode."""
from types import SimpleNamespace

import pytest

from backend.api.algo import fill_notify


def _row(**over):
    base = dict(
        id=101, account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="NFO",
        transaction_type="BUY", quantity=75, fill_price=112.5,
        product="NRML", mode="live",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def logged(monkeypatch):
    calls = []
    monkeypatch.setattr(fill_notify.logger, "info", lambda msg, **kw: calls.append((msg, kw)))
    return calls


@pytest.mark.asyncio
async def test_live_fill_is_logged_once_with_tags_and_fields(logged):
    await fill_notify.notify_fills([_row()])
    assert len(logged) == 1
    msg, kw = logged[0]
    assert msg == "order filled"
    extra = kw["extra"]
    assert extra["tags"] == ["orders"]
    assert extra["event"] == "filled"
    assert extra["mode"] == "live"
    assert extra["order_id"] == 101
    assert extra["account"] == "ZG0790"
    assert extra["symbol"] == "NIFTY26OCT25000CE"
    assert extra["fill_price"] == 112.5


@pytest.mark.asyncio
async def test_paper_fill_is_logged_with_its_mode_so_agents_can_skip_it(logged):
    await fill_notify.notify_fills([_row(mode="paper")])
    assert logged[0][1]["extra"]["mode"] == "paper"


@pytest.mark.asyncio
async def test_each_row_gets_its_own_record(logged):
    await fill_notify.notify_fills([_row(id=1), _row(id=2)])
    assert [kw["extra"]["order_id"] for _, kw in logged] == [1, 2]


@pytest.mark.asyncio
async def test_a_bad_row_does_not_stop_the_others(monkeypatch, logged):
    await fill_notify.notify_fills([SimpleNamespace(id=5), _row(id=6)])
    assert [kw["extra"]["order_id"] for _, kw in logged] == [6]


def test_format_uses_the_given_time_in_ist():
    from datetime import datetime, timezone
    title, body = fill_notify.format_fill_message(
        _row(), when=datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc))
    assert title == "Order filled: BUY 75 NIFTY26OCT25000CE"
    assert "Time: 10:15:30 IST" in body


def test_fill_message_shows_full_account():
    title, body = fill_notify.format_fill_message(_row(account="ZG0790"))
    assert "Account: ZG0790" in body
