"""Fill alerts: live fills notify ntfy and Telegram once; paper/sim are skipped."""
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
def sent(monkeypatch):
    calls = {"ntfy": [], "tg": []}
    monkeypatch.setattr(fill_notify, "send_ntfy_alert",
                        lambda title, body: calls["ntfy"].append((title, body)))
    monkeypatch.setattr(fill_notify, "_send_telegram",
                        lambda msg: calls["tg"].append(msg))
    return calls


def _enable(monkeypatch, **caps):
    monkeypatch.setattr(fill_notify, "is_enabled",
                        lambda cap: caps.get(cap, False))


def test_format_has_order_details():
    title, body = fill_notify.format_fill_message(_row())
    assert title == "Order filled: BUY 75 NIFTY26OCT25000CE"
    assert "@ 112.50" in body
    assert "Product: NRML" in body
    assert "Order id: 101" in body
    assert "IST" in body


@pytest.mark.asyncio
async def test_live_fill_sends_to_both_channels(monkeypatch, sent):
    _enable(monkeypatch, ntfy=True, telegram=True)
    await fill_notify.notify_fills([_row()])
    assert len(sent["ntfy"]) == 1
    assert len(sent["tg"]) == 1
    assert "Order filled" in sent["tg"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paper", "sim", "replay", "shadow"])
async def test_non_live_fill_is_skipped(monkeypatch, sent, mode):
    _enable(monkeypatch, ntfy=True, telegram=True)
    await fill_notify.notify_fills([_row(mode=mode)])
    assert sent["ntfy"] == [] and sent["tg"] == []


@pytest.mark.asyncio
async def test_disabled_capabilities_send_nothing(monkeypatch, sent):
    _enable(monkeypatch)
    await fill_notify.notify_fills([_row()])
    assert sent["ntfy"] == [] and sent["tg"] == []


@pytest.mark.asyncio
async def test_ntfy_failure_does_not_block_telegram(monkeypatch, sent):
    _enable(monkeypatch, ntfy=True, telegram=True)

    def boom(title, body):
        raise RuntimeError("ntfy down")

    monkeypatch.setattr(fill_notify, "send_ntfy_alert", boom)
    await fill_notify.notify_fills([_row()])
    assert len(sent["tg"]) == 1
