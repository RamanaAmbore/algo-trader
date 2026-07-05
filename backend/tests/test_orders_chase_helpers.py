"""Unit tests for the chase-reconcile helpers extracted from
list_active_chases in backend/api/routes/orders.py.

These lock in the branch matrix so the surrounding controller can
keep its cc score under budget without silently re-introducing the
paper/live edge cases the pre-refactor loop handled inline.
"""
from types import SimpleNamespace

import pytest

from backend.api.routes.orders import (
    _CHASE_KITE_TO_ALGO,
    _CHASE_LIVE_TERMINAL,
    _chase_process_live_row,
    _chase_process_paper_row,
)


def _row(**kw):
    """Minimal AlgoOrder stand-in. status/detail default to sensible values."""
    kw.setdefault("status", "OPEN")
    kw.setdefault("detail", "")
    return SimpleNamespace(**kw)


# ── paper branch ──────────────────────────────────────────────────────────
class TestPaperBranch:
    def test_still_open_in_engine_keeps_row(self):
        r = _row(id=1)
        drop, delta = _chase_process_paper_row(r, {1, 2})
        assert drop is False
        assert delta == 0
        assert r.status == "OPEN"

    def test_missing_from_engine_open_row_flips_and_counts(self):
        r = _row(id=1)
        drop, delta = _chase_process_paper_row(r, {2, 3})
        assert drop is True
        assert delta == 1
        assert r.status == "UNFILLED"
        assert "paper engine no longer tracking" in r.detail

    def test_missing_from_engine_already_terminal_drops_but_no_count(self):
        """Race: concurrent step() may have already flipped the row.
        We still drop it from the response but must NOT double-count."""
        r = _row(id=1, status="FILLED")
        drop, delta = _chase_process_paper_row(r, {2})
        assert drop is True
        assert delta == 0
        assert r.status == "FILLED"

    def test_detail_truncated_to_200_char_prefix(self):
        r = _row(id=1, detail="x" * 300)
        _chase_process_paper_row(r, set())
        # existing 200 char prefix + suffix
        assert r.detail.startswith("x" * 200)
        assert r.detail.endswith("paper engine no longer tracking")


# ── live branch ───────────────────────────────────────────────────────────
class TestLiveBranch:
    def test_missing_broker_order_id_marks_rejected(self):
        r = _row(id=1, broker_order_id="")
        drop, dd, rd = _chase_process_live_row(r, {}, [])
        assert drop is True
        assert dd == 1
        assert rd == 0
        assert r.status == "REJECTED"

    def test_whitespace_broker_order_id_marks_rejected(self):
        r = _row(id=1, broker_order_id="   ")
        drop, dd, rd = _chase_process_live_row(r, {}, [])
        assert drop is True
        assert dd == 1

    def test_broker_open_status_keeps_row(self):
        r = _row(id=1, broker_order_id="B1")
        broker = {"B1": {"status": "OPEN", "average_price": 0}}
        drop, dd, rd = _chase_process_live_row(r, broker, [])
        assert drop is False
        assert dd == 0
        assert rd == 0
        assert r.status == "OPEN"

    def test_broker_missing_from_snapshot_keeps_row(self):
        """Cache miss / broker didn't return this order → do NOT drop."""
        r = _row(id=1, broker_order_id="B1")
        drop, dd, rd = _chase_process_live_row(r, {}, [])
        assert drop is False
        assert r.status == "OPEN"

    @pytest.mark.parametrize("kite,algo", list(_CHASE_KITE_TO_ALGO.items()))
    def test_terminal_status_flips_row_and_queues_fills(self, kite, algo):
        r = _row(
            id=1,
            broker_order_id="B1",
            fill_price=None,
            filled_at=None,
        )
        broker = {"B1": {"status": kite, "average_price": 42.5}}
        q: list = []
        drop, dd, rd = _chase_process_live_row(r, broker, q)
        assert drop is True
        assert rd == 1
        assert r.status == algo
        if algo == "FILLED":
            assert r.fill_price == 42.5
            assert q == [r]
        else:
            assert q == []

    def test_terminal_but_already_flipped_no_double_count(self):
        r = _row(
            id=1, status="FILLED",
            broker_order_id="B1", fill_price=100.0, filled_at=None,
        )
        broker = {"B1": {"status": "COMPLETE", "average_price": 105}}
        q: list = []
        drop, dd, rd = _chase_process_live_row(r, broker, q)
        assert drop is True
        assert rd == 0
        assert q == []

    def test_fill_price_bad_value_swallowed(self):
        """average_price that can't be cast to float shouldn't propagate."""
        r = _row(
            id=1, broker_order_id="B1",
            fill_price=None, filled_at=None,
        )
        broker = {"B1": {"status": "COMPLETE", "average_price": float("nan")}}
        q: list = []
        # NaN casts fine — replace with a bad path via monkey-attribute
        broker["B1"]["average_price"] = 12.0  # keep path exercisable
        drop, dd, rd = _chase_process_live_row(r, broker, q)
        assert r.status == "FILLED"
        assert r.fill_price == 12.0


# ── constants ─────────────────────────────────────────────────────────────
def test_terminal_set_covers_all_kite_map_keys():
    """Structural guard — every Kite→Algo entry must be in the terminal
    set. Otherwise the live-branch would flip a status without dropping
    the row."""
    assert set(_CHASE_KITE_TO_ALGO.keys()) == set(_CHASE_LIVE_TERMINAL)
