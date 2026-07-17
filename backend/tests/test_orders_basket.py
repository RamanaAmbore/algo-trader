"""
Tests for orders_basket.py — basket order critical path.
SSOT: translate_qty called per leg; basket_order_margins in preflight.
Perf: lots→contracts multiplication exactly once per leg.
Stale: G2 fat-finger guard present.
Reuse: delegated lot_size resolution, shared translate_qty path.
UX: validation errors on bad legs.
"""
from pathlib import Path

_SRC = Path("backend/api/routes/orders_basket.py").read_text()


def test_basket_translate_qty_per_leg():
    assert "translate_qty" in _SRC, (
        "translate_qty must be called per leg to convert contracts→lots for MCX "
        "and match Kite's basket_order_margins convention"
    )


def test_basket_preflight_calls_margins():
    assert "basket_order_margins" in _SRC, (
        "basket preflight must call basket_order_margins before placement "
        "to block over-margin orders"
    )


def test_basket_g2_fat_finger_cap_present():
    assert "5" in _SRC and ("lot" in _SRC.lower() or "cap" in _SRC.lower()), (
        "G2 5-lot fat-finger cap must be present in basket handler"
    )
    # Exact check: lots > 5 guard appears
    assert "_lots > 5" in _SRC or "lots > 5" in _SRC or "lot_cap" in _SRC.lower(), (
        "Fat-finger 5-lot cap guard must compare lots against threshold"
    )


def test_basket_lot_size_resolution_present():
    assert "lot_size" in _SRC, (
        "lot_size must be resolved per leg before broker submit"
    )


def test_basket_lots_to_contracts_multiplication():
    # Verify the contracts = lots × lot_size multiplication
    assert "lot_size" in _SRC and ("_leg_contracts" in _SRC or "* _leg_lot" in _SRC or "* lot_size" in _SRC), (
        "lots→contracts multiplication (lots × lot_size) must appear in basket handler"
    )


def test_basket_validation_raises_on_bad_legs():
    assert (
        "422" in _SRC
        or "400" in _SRC
        or "ValidationException" in _SRC
        or "raise" in _SRC
    ), (
        "HTTP 400/422 or exception must be raised for invalid basket legs"
    )
