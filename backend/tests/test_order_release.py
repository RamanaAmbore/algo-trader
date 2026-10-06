"""Release checks: position must match, and the held order must be HELD."""
from backend.api.algo.order_release import _find_net_qty, position_matches


def test_sell_close_needs_matching_long_position():
    assert position_matches("SELL", 200, 200) == (True, "ok")


def test_sell_close_refused_when_position_changed():
    ok, why = position_matches("SELL", 200, 100)
    assert ok is False and "position changed" in why


def test_buy_close_needs_matching_short_position():
    assert position_matches("BUY", 200, -200) == (True, "ok")
    assert position_matches("BUY", 200, 200)[0] is False


def test_find_net_qty_matches_symbol_and_exchange():
    positions = {"net": [
        {"tradingsymbol": "CRUDEOIL26OCT8600CE", "exchange": "MCX", "quantity": 200},
        {"tradingsymbol": "CRUDEOIL26OCT8600CE", "exchange": "NFO", "quantity": 5},
    ]}
    assert _find_net_qty(positions, "CRUDEOIL26OCT8600CE", "MCX") == 200
    assert _find_net_qty(positions, "CRUDEOIL26OCT8700CE", "MCX") == 0
    assert _find_net_qty({}, "X", "MCX") == 0
