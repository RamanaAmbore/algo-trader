"""
SimGttBook contract tests — covers the price-crossing logic and OCO
sibling-cancel semantics in isolation from SimDriver.

The book is a pure data structure plus an on_trigger callback hook; we
can exercise the full lifecycle with stub callbacks and synthetic
ltp_by_symbol maps. Faster + more reliable than spinning up a full
SimDriver run.
"""
from __future__ import annotations

import pytest

from backend.api.algo.sim.gtt_book import (
    GTT_STATUS_ACTIVE,
    GTT_STATUS_CANCELLED,
    GTT_STATUS_EXPIRED,
    GTT_STATUS_TRIGGERED,
    SimGttBook,
    _crossed,
)


@pytest.fixture
def book():
    """Empty book with a list-recording trigger handler."""
    fired = []
    rec = []
    b = SimGttBook(
        on_trigger=lambda gtt, idx: fired.append((gtt.gtt_id, idx)),
        on_record=lambda kind, payload: rec.append((kind, payload)),
    )
    b._fired = fired
    b._rec = rec
    return b


# ── _crossed primitive ──────────────────────────────────────────────

def test_crossed_up_direction():
    """LTP moves upward past trigger."""
    assert _crossed(100.0, 105.0, 103.0) is True
    assert _crossed(100.0, 103.0, 103.0) is True  # equality counts
    assert _crossed(100.0, 102.99, 103.0) is False


def test_crossed_down_direction():
    """LTP moves downward past trigger."""
    assert _crossed(100.0, 95.0, 97.0) is True
    assert _crossed(100.0, 97.0, 97.0) is True
    assert _crossed(100.0, 97.01, 97.0) is False


def test_crossed_flat():
    """LTP doesn't move — no crossing unless we start exactly on it."""
    assert _crossed(100.0, 100.0, 100.0) is True   # placed AT the trigger
    assert _crossed(100.0, 100.0, 99.99) is False


# ── place / cancel basics ───────────────────────────────────────────

def test_place_single_gtt_returns_active(book):
    g = book.place(
        account="ZG0001", tradingsymbol="NIFTY26JUNFUT", exchange="NFO",
        trigger_type="single", trigger_values=[22000.0],
        orders=[{"transaction_type": "SELL", "quantity": 50, "price": 22000.0}],
        last_price=21800.0,
    )
    assert g.is_active()
    assert g.status == GTT_STATUS_ACTIVE
    assert g.last_seen_ltp == 21800.0
    assert g.gtt_id.startswith("sim-gtt-")


def test_place_validates_single_vs_two_leg_lengths(book):
    # single must have exactly 1 trigger + 1 order
    with pytest.raises(ValueError, match="single-trigger"):
        book.place(
            account="A", tradingsymbol="X", exchange="NSE",
            trigger_type="single", trigger_values=[100.0, 90.0],
            orders=[{"transaction_type": "BUY", "quantity": 1, "price": 100.0},
                    {"transaction_type": "BUY", "quantity": 1, "price": 90.0}],
            last_price=95.0,
        )
    # two-leg must have exactly 2 of each
    with pytest.raises(ValueError, match="two-leg"):
        book.place(
            account="A", tradingsymbol="X", exchange="NSE",
            trigger_type="two-leg", trigger_values=[100.0],
            orders=[{"transaction_type": "BUY", "quantity": 1, "price": 100.0}],
            last_price=95.0,
        )


def test_place_rejects_unknown_trigger_type(book):
    with pytest.raises(ValueError):
        book.place(
            account="A", tradingsymbol="X", exchange="NSE",
            trigger_type="five-leg", trigger_values=[1.0],
            orders=[{}], last_price=0.5,
        )


def test_cancel_flips_to_cancelled(book):
    g = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[100.0],
        orders=[{"transaction_type": "BUY", "quantity": 1, "price": 100.0}],
        last_price=95.0,
    )
    cancelled = book.cancel(g.gtt_id)
    assert cancelled.status == GTT_STATUS_CANCELLED
    assert cancelled.cancelled_at is not None


def test_cancel_terminal_gtt_returns_none(book):
    g = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[100.0],
        orders=[{"transaction_type": "BUY", "quantity": 1, "price": 100.0}],
        last_price=95.0,
    )
    book.cancel(g.gtt_id)
    # Second cancel — already terminal.
    assert book.cancel(g.gtt_id) is None


# ── check_triggers behavior ─────────────────────────────────────────

def test_single_gtt_fires_on_upward_cross(book):
    g = book.place(
        account="ZG0001", tradingsymbol="NIFTY",
        exchange="NSE_INDICES",
        trigger_type="single", trigger_values=[22000.0],
        orders=[{"transaction_type": "SELL", "quantity": 50, "price": 22000.0}],
        last_price=21800.0,
    )
    # Tick 1: price moves to 22050 — crossed.
    fired = book.check_triggers({("ZG0001", "NIFTY"): 22050.0})
    assert len(fired) == 1
    assert fired[0].gtt_id == g.gtt_id
    assert g.status == GTT_STATUS_TRIGGERED
    assert g.triggered_at is not None
    assert g.triggered_leg_index == 0
    assert book._fired == [(g.gtt_id, 0)]


def test_gtt_does_not_fire_until_crossing(book):
    book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[100.0],
        orders=[{"transaction_type": "BUY", "quantity": 1, "price": 100.0}],
        last_price=95.0,
    )
    # Several ticks below — no fire.
    book.check_triggers({("A", "X"): 96.0})
    book.check_triggers({("A", "X"): 98.0})
    book.check_triggers({("A", "X"): 99.99})
    assert len(book._fired) == 0


def test_two_leg_oco_fires_winning_leg_only(book):
    """A two-leg GTT with TP=110, SL=90: price rises past TP, only the
    TP leg fires. SL is implicitly skipped (broker side handles this
    on real Kite two-leg)."""
    g = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="two-leg",
        trigger_values=[110.0, 90.0],
        orders=[
            {"transaction_type": "SELL", "quantity": 1, "price": 110.0},  # TP leg
            {"transaction_type": "SELL", "quantity": 1, "price": 90.0},   # SL leg
        ],
        last_price=100.0,
    )
    fired = book.check_triggers({("A", "X"): 112.0})
    assert len(fired) == 1
    assert g.triggered_leg_index == 0  # TP leg only


def test_oco_emulation_cancels_sibling(book):
    """Groww-style: TWO single GTTs paired via pair_with. When one
    triggers, the book auto-cancels the sibling so only one of TP/SL
    executes."""
    tp = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[110.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 110.0}],
        last_price=100.0,
    )
    sl = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[90.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 90.0}],
        last_price=100.0,
        pair_with=tp.gtt_id,
    )
    # Wire the other direction too — pair_with is symmetric in production.
    tp.pair_with = sl.gtt_id

    # Price spikes up — TP fires; SL must auto-cancel.
    book.check_triggers({("A", "X"): 112.0})
    assert tp.status == GTT_STATUS_TRIGGERED
    assert sl.status == GTT_STATUS_CANCELLED


def test_symbol_disappears_marks_expired(book):
    """When the position's symbol vanishes from ltp_by_symbol (filled
    elsewhere, force-closed at session end, etc.), the GTT auto-expires
    so the book doesn't keep checking a dead row."""
    g = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[110.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 110.0}],
        last_price=100.0,
    )
    # First tick — symbol still around.
    book.check_triggers({("A", "X"): 105.0, ("A", "Y"): 50.0})
    assert g.status == GTT_STATUS_ACTIVE
    # Next tick — X gone; only Y left.
    book.check_triggers({("A", "Y"): 51.0})
    assert g.status == GTT_STATUS_EXPIRED


def test_check_triggers_skips_terminal_gtts(book):
    g = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[110.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 110.0}],
        last_price=100.0,
    )
    book.cancel(g.gtt_id)
    # Even with a crossing LTP, the cancelled GTT doesn't fire.
    book.check_triggers({("A", "X"): 200.0})
    assert book._fired == []


def test_reset_clears_book(book):
    book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[110.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 110.0}],
        last_price=100.0,
    )
    book.place(
        account="A", tradingsymbol="Y", exchange="NSE",
        trigger_type="single", trigger_values=[60.0],
        orders=[{"transaction_type": "BUY", "quantity": 1, "price": 60.0}],
        last_price=50.0,
    )
    assert len(book.all_active()) == 2
    book.reset()
    assert len(book.all_()) == 0
    # ID counter resets so a fresh run starts at sim-gtt-000001.
    fresh = book.place(
        account="A", tradingsymbol="Z", exchange="NSE",
        trigger_type="single", trigger_values=[100.0],
        orders=[{"transaction_type": "BUY", "quantity": 1, "price": 100.0}],
        last_price=95.0,
    )
    assert fresh.gtt_id == "sim-gtt-000001"


def test_record_hook_called_for_lifecycle(book):
    g = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[110.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 110.0}],
        last_price=100.0,
    )
    book.check_triggers({("A", "X"): 115.0})
    kinds = [k for k, _ in book._rec]
    assert "gtt_placed" in kinds
    assert "gtt_triggered" in kinds


def test_snapshot_counts_each_state(book):
    a = book.place(
        account="A", tradingsymbol="X", exchange="NSE",
        trigger_type="single", trigger_values=[110.0],
        orders=[{"transaction_type": "SELL", "quantity": 1, "price": 110.0}],
        last_price=100.0,
    )
    b = book.place(
        account="A", tradingsymbol="Y", exchange="NSE",
        trigger_type="single", trigger_values=[60.0],
        orders=[{"transaction_type": "BUY", "quantity": 1, "price": 60.0}],
        last_price=70.0,
    )
    book.cancel(a.gtt_id)
    book.check_triggers({("A", "Y"): 55.0})  # b fires (downward cross)

    snap = book.snapshot()
    assert snap["cancelled_count"] == 1
    assert snap["triggered_count"] == 1
    assert snap["active_count"] == 0
    assert len(snap["gtts"]) == 2
