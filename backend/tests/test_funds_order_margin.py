"""Order-margin dry run: validation, success path, broker failure mapping."""
import pytest
from litestar.exceptions import HTTPException

import backend.api.algo.actions_preflight as pf
import backend.brokers as brokers
from backend.api.routes import funds


class _Broker:
    pass


@pytest.fixture
def stubs(monkeypatch):
    seen = {}

    async def build(broker, exch, symbol, side, qty, order_type, product, variety, price, paired):
        seen["basket"] = (exch, symbol, side, qty, order_type, product, variety, price)
        return [{"tradingsymbol": symbol}]

    async def fetch(broker, loop, orders):
        seen["fetched"] = orders
        return {"final": {"total": 1234.5}}

    monkeypatch.setattr(pf, "_preflight_build_basket_orders", build)
    monkeypatch.setattr(pf, "_preflight_fetch_basket_margin", fetch)
    monkeypatch.setattr(pf, "_preflight_parse_basket_margin", lambda bm: 1234.5)
    monkeypatch.setattr(brokers, "get_broker", lambda account: _Broker())
    return seen


def _call(**over):
    kwargs = dict(
        account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="nfo", side="buy",
        qty=75, product="NRML", order_type="LIMIT", price=112.5,
    )
    kwargs.update(over)
    return funds.order_margin_for(**kwargs)


@pytest.mark.asyncio
async def test_returns_required_margin_and_echoes_order(stubs):
    out = await _call()
    assert out["required_margin"] == 1234.5
    assert out["side"] == "BUY"
    assert out["exchange"] == "NFO"
    assert out["source"] == "kite_basket_margin"
    assert stubs["basket"] == ("NFO", "NIFTY26OCT25000CE", "BUY", 75, "LIMIT", "NRML", "regular", 112.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"side": "HOLD"},
    {"qty": 0},
    {"symbol": ""},
])
async def test_rejects_bad_input(stubs, over):
    with pytest.raises(HTTPException) as exc:
        await _call(**over)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_broker_failure_maps_to_502(monkeypatch, stubs):
    async def fail(broker, loop, orders):
        return RuntimeError("kite down")

    monkeypatch.setattr(pf, "_preflight_fetch_basket_margin", fail)
    with pytest.raises(HTTPException) as exc:
        await _call()
    assert exc.value.status_code == 502
