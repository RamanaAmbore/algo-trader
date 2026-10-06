"""Golden output for the error and fill alerts as they are today. Any migration must keep these exact strings."""
import datetime as _dt
from types import SimpleNamespace

import pytest

from backend.api.algo import fill_notify
from backend.shared.helpers import alert_utils, error_alerts


class _FixedDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 6, 10, 15, 30, tzinfo=tz)


@pytest.fixture
def sent(monkeypatch):
    import backend.shared.helpers.utils as u
    calls = {"ntfy": [], "tg": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(alert_utils, "send_ntfy_alert", lambda t, b, **k: calls["ntfy"].append((t, b)))
    monkeypatch.setattr(alert_utils, "_send_telegram", lambda m, **k: calls["tg"].append(m))
    return calls


def _error_rec(msg, name="backend.x", alert_now=True):
    from datetime import timezone as _tz
    return {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": "ERROR",
            "logger": name, "message": msg, "tags": ["error"],
            "extra": {"alert_now": alert_now} if alert_now else {}}


@pytest.fixture
def error_agent(monkeypatch):
    from backend.api.algo import event_agents
    monkeypatch.setattr(event_agents, "_gate", error_alerts.RepeatGate())
    return SimpleNamespace(slug="error-alert", **{k: event_agents.ERROR_AGENT[k]
                                                  for k in ("conditions", "events", "actions")})


@pytest.mark.asyncio
async def test_error_alert_golden_text(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("boom: bad value")], [error_agent])
    assert sent["ntfy"] == [("RamboQuant error", "backend.x\nboom: bad value")]
    assert sent["tg"] == ["<b>RamboQuant error</b>\n<code>backend.x</code>\nboom: bad value"]


@pytest.mark.asyncio
async def test_error_alert_strips_html_like_fragments_as_before(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("boom <bad>")], [error_agent])
    assert sent["ntfy"] == [("RamboQuant error", "backend.x\nboom")]


@pytest.mark.asyncio
async def test_error_alert_repeat_suffix_after_cooldown(monkeypatch, sent, error_agent):
    from backend.api.algo import event_agents
    clock = {"t": 0.0}
    monkeypatch.setattr(event_agents, "_gate", error_alerts.RepeatGate(clock=lambda: clock["t"]))
    await event_agents.dispatch([_error_rec("down", alert_now=True)], [error_agent])
    clock["t"] = 10.0
    await event_agents.dispatch([_error_rec("down", alert_now=True)], [error_agent])
    clock["t"] = error_alerts.COOLDOWN_S + 1
    await event_agents.dispatch([_error_rec("down", alert_now=True)], [error_agent])
    assert [b for _, b in sent["ntfy"]] == ["backend.x\ndown", "backend.x\ndown (+1 repeats)"]
    assert sent["tg"][1] == "<b>RamboQuant error</b>\n<code>backend.x</code>\ndown (+1 repeats)"


@pytest.mark.asyncio
async def test_error_alert_masks_account_numbers(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("order failed for ZG0790.orders")], [error_agent])
    assert "ZG0790" not in sent["ntfy"][0][1]


def test_fill_format_golden():
    from datetime import timezone
    row = SimpleNamespace(id=101, account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="NFO",
                          transaction_type="BUY", quantity=75, fill_price=112.5, product="NRML", mode="live")
    title, body = fill_notify.format_fill_message(
        row, when=_dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc))
    assert title == "Order filled: BUY 75 NIFTY26OCT25000CE"
    assert body == "\n".join([
        "Account: ZG0790",
        "BUY 75 NIFTY26OCT25000CE (NFO) @ 112.50",
        "Product: NRML",
        "Order id: 101",
        "Time: 10:15:30 IST",
    ])


@pytest.mark.asyncio
async def test_fill_delivery_golden_via_event_agent(monkeypatch):
    from datetime import timezone
    from backend.api.algo import event_agents
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.utils as u
    sent = {"ntfy": [], "tg": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "send_ntfy_alert", lambda t, b, **k: sent["ntfy"].append((t, b)))
    monkeypatch.setattr(au, "_send_telegram", lambda m, **k: sent["tg"].append(m))
    records = []
    monkeypatch.setattr(fill_notify.logger, "info", lambda msg, **kw: records.append(kw["extra"]))
    row = SimpleNamespace(id=7, account="ZG0790", symbol="CRUDEOIL26OCTFUT", exchange="MCX",
                          transaction_type="SELL", quantity=1, fill_price=8750.0, product="NRML", mode="live")
    await fill_notify.notify_fills([row])
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc), "level": "INFO",
           "tags": ["orders"], "extra": records[0]}
    agent = SimpleNamespace(slug=event_agents.FILL_AGENT["slug"], **{k: event_agents.FILL_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent["ntfy"] == [("Order filled: SELL 1 CRUDEOIL26OCTFUT",
                             "Account: ZG0790\nSELL 1 CRUDEOIL26OCTFUT (MCX) @ 8750.00\nProduct: NRML\nOrder id: 7\nTime: 10:15:30 IST")]
    assert sent["tg"] == ["<b>Order filled: SELL 1 CRUDEOIL26OCTFUT</b>\n"
                          "Account: ZG0790\nSELL 1 CRUDEOIL26OCTFUT (MCX) @ 8750.00\nProduct: NRML\nOrder id: 7\nTime: 10:15:30 IST"]
