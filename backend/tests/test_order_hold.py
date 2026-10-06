"""Hold policy: override beats global switch, global beats default; cut-off and release price."""
from datetime import datetime

import pytest

from backend.api.algo.order_hold import (
    HoldCategory, cutoff_time, effective_hold, release_price,
)


def test_default_is_held_with_no_override_or_switch():
    assert effective_hold(HoldCategory.EXPIRY_CLOSE, None, None) is True


def test_global_switch_released_releases_category():
    sw = {"expiry_close": True}
    assert effective_hold(HoldCategory.EXPIRY_CLOSE, None, sw) is False


def test_global_switch_held_holds_category():
    sw = {"expiry_close": False}
    assert effective_hold(HoldCategory.EXPIRY_CLOSE, None, sw) is True


def test_override_beats_global_switch_both_ways():
    assert effective_hold(HoldCategory.EXPIRY_CLOSE, True, {"expiry_close": True}) is True
    assert effective_hold(HoldCategory.EXPIRY_CLOSE, False, {"expiry_close": False}) is False


def test_switch_for_one_category_does_not_affect_another():
    sw = {"expiry_close": True}
    assert effective_hold(HoldCategory.TEMPLATE_EXIT, None, sw) is True


def test_cutoff_is_close_minus_lead():
    close = datetime(2026, 10, 15, 15, 30)
    assert cutoff_time(close, 15) == datetime(2026, 10, 15, 15, 15)


def test_cutoff_rejects_negative_lead():
    with pytest.raises(ValueError):
        cutoff_time(datetime(2026, 10, 15, 15, 30), -1)


def test_release_price_is_mid_rounded_to_tick():
    ok, price, reason = release_price(bid=100.0, ask=100.3, last=None,
                                      tick=0.05, band_low=50, band_high=200)
    assert ok is True and reason == "ok"
    assert price == 100.15


def test_release_price_falls_back_to_last():
    ok, price, _ = release_price(bid=None, ask=None, last=101.0,
                                 tick=0.05, band_low=50, band_high=200)
    assert ok is True and price == 101.0


def test_release_price_refused_without_live_price():
    ok, price, reason = release_price(bid=None, ask=None, last=None,
                                      tick=0.05, band_low=50, band_high=200)
    assert ok is False and price is None and "no live price" in reason


def test_release_price_refused_outside_band():
    ok, price, reason = release_price(bid=300, ask=301, last=None,
                                      tick=0.05, band_low=50, band_high=200)
    assert ok is False and "outside band" in reason


def test_release_price_refused_on_bad_tick():
    ok, _, reason = release_price(bid=100, ask=101, last=None,
                                  tick=0, band_low=50, band_high=200)
    assert ok is False and "tick" in reason


def test_hold_record_round_trips():
    from backend.api.algo.order_hold import hold_record, parse_hold_record
    raw = hold_record(HoldCategory.EXPIRY_CLOSE, "expiry close", "CHASE_MED", None,
                      datetime(2026, 10, 15, 15, 15))
    rec = parse_hold_record(raw)
    assert rec["category"] == "expiry_close"
    assert rec["price_policy"] == "CHASE_MED"
    assert rec["override"] is None
    assert rec["held_at"] == "2026-10-15T15:15:00"


def test_parse_hold_record_handles_empty_and_bad_input():
    from backend.api.algo.order_hold import parse_hold_record
    assert parse_hold_record(None) is None
    assert parse_hold_record("") is None
    assert parse_hold_record("{bad") is None
    assert parse_hold_record("[1]") is None
