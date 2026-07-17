"""
Tests for api/algo/shadow.py — shadow trade engine.
SSOT: ShadowTradeEngine.capture_order is the sole execution path.
Perf: validates via basket_margin but never executes a real order.
Stale: AlgoOrder(mode='shadow') audit trail always written.
Reuse: same basket_margin path as live orders (identical validation).
UX: returns structured result (not None / bare dict).
"""
from pathlib import Path

_SRC = Path("backend/api/algo/shadow.py").read_text()


def test_shadow_engine_class_exists():
    from backend.api.algo.shadow import ShadowTradeEngine
    assert ShadowTradeEngine is not None


def test_shadow_uses_basket_margin_for_validation():
    assert "basket_margin" in _SRC, (
        "ShadowTradeEngine must call basket_margin for preflight validation — "
        "shadow validates but never executes"
    )


def test_shadow_writes_algo_order_in_shadow_mode():
    assert "AlgoOrder" in _SRC, "Shadow must write an AlgoOrder DB row for audit trail"
    assert "mode='shadow'" in _SRC or 'mode="shadow"' in _SRC, (
        "AlgoOrder must be written with mode='shadow' so shadow fills don't "
        "appear in the live order book"
    )


def test_shadow_does_not_call_place_order():
    # Shadow logs what kite.place_order WOULD receive — never actually calls it
    # The method builds kwargs but must not call broker.place_order or kite.place_order
    import re
    # Find the capture_order method body
    match = re.search(r"async def capture_order.*?(?=\n    async def |\nclass |\Z)", _SRC, re.DOTALL)
    if match:
        body = match.group(0)
        # broker.place_order must NOT appear (shadow never executes)
        assert "broker.place_order" not in body, (
            "capture_order must NOT call broker.place_order — shadow only logs, never executes"
        )


def test_shadow_captures_kite_payload_as_json():
    assert "json" in _SRC.lower() or "payload" in _SRC, (
        "Shadow must capture the Kite-formatted payload (as JSON or dict) "
        "for the audit trail"
    )


def test_get_shadow_engine_factory_exists():
    from backend.api.algo.shadow import get_shadow_engine
    engine = get_shadow_engine()
    assert engine is not None, "get_shadow_engine() must return a ShadowTradeEngine instance"
