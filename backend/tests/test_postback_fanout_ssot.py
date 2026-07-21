"""Tier 2 / A4 — Postback broadcast fan-out delegation tests.

Pre-fix: Kite postback handler inlined the cache-invalidate +
WS-broadcast trio (`invalidate("orders")`, `position_filled`,
`book_changed`) — a near-duplicate of the shared
`_process_broker_postback` used by Dhan/Groww.

Post-fix: A new `_postback_broadcast_fanout` helper owns the trio.
Both Kite (inline) and `_process_broker_postback` delegate to it.

Asserts:
  1. SSOT — only `_postback_broadcast_fanout` writes the broadcast
     payloads (regex grep).
  2. Helper behaviour on terminal vs non-terminal vs COMPLETE.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from backend.api.routes.orders import _postback_broadcast_fanout


ROUTES_FILE = Path(__file__).resolve().parent.parent / "api" / "routes" / "orders.py"


# ---------------------------------------------------------------------------
# Helper behaviour
# ---------------------------------------------------------------------------

def _capture_broadcasts():
    """Return a list that captures every broadcast payload as dict."""
    captured = []
    def _fake_broadcast(payload):
        captured.append(json.loads(payload))
    return captured, _fake_broadcast


class TestPostbackBroadcastFanout:
    def test_terminal_status_invalidates_all_caches(self):
        captured, fake = _capture_broadcasts()
        with patch("backend.api.routes.orders.broadcast", side_effect=fake), \
             patch("backend.api.routes.orders.invalidate") as inv:
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="123", account="ZG0790",
                masked="ZG####", symbol="NIFTY26JULFUT", txn="BUY",
                qty=50, price=22000.0, exchange="NFO",
            )
        # invalidate called for orders + positions + holdings (3x)
        calls = [c.args[0] for c in inv.call_args_list]
        assert "orders" in calls
        assert "positions" in calls
        assert "holdings" in calls

    def test_non_terminal_only_invalidates_orders(self):
        captured, fake = _capture_broadcasts()
        with patch("backend.api.routes.orders.broadcast", side_effect=fake), \
             patch("backend.api.routes.orders.invalidate") as inv:
            _postback_broadcast_fanout(
                status="OPEN", order_id="123", account="ZG0790",
                masked="ZG####", symbol="NIFTY26JULFUT", txn="BUY",
                qty=50, price=22000.0,
            )
        calls = [c.args[0] for c in inv.call_args_list]
        assert calls == ["orders"]

    def test_complete_emits_position_filled(self):
        captured, fake = _capture_broadcasts()
        with patch("backend.api.routes.orders.broadcast", side_effect=fake), \
             patch("backend.api.routes.orders.invalidate"):
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="123", account="ZG0790",
                masked="ZG####", symbol="NIFTY26JULFUT", txn="BUY",
                qty=50, price=22000.0, exchange="NFO",
            )
        events = [b["event"] for b in captured]
        assert "order_update" in events
        assert "position_filled" in events
        assert "book_changed" in events
        # Signed qty: BUY → positive
        pf = next(b for b in captured if b["event"] == "position_filled")
        assert pf["qty"] == 50

    def test_complete_sell_is_negative_delta(self):
        captured, fake = _capture_broadcasts()
        with patch("backend.api.routes.orders.broadcast", side_effect=fake), \
             patch("backend.api.routes.orders.invalidate"):
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="123", account="ZG0790",
                masked="ZG####", symbol="NIFTY26JULFUT", txn="SELL",
                qty=50, price=22000.0,
            )
        pf = next(b for b in captured if b["event"] == "position_filled")
        assert pf["qty"] == -50

    def test_cancelled_emits_book_changed_no_position_filled(self):
        captured, fake = _capture_broadcasts()
        with patch("backend.api.routes.orders.broadcast", side_effect=fake), \
             patch("backend.api.routes.orders.invalidate"):
            _postback_broadcast_fanout(
                status="CANCELLED", order_id="123", account="ZG0790",
                masked="ZG####", symbol="NIFTY26JULFUT", txn="BUY",
                qty=50, price=22000.0,
            )
        events = [b["event"] for b in captured]
        assert "order_update" in events
        assert "book_changed" in events
        assert "position_filled" not in events


# ---------------------------------------------------------------------------
# SSOT grep — the broadcast trio lives in exactly one helper
# ---------------------------------------------------------------------------

class TestPostbackBroadcastSSOT:
    def test_position_filled_broadcast_only_in_fanout(self):
        """The `event: position_filled` payload should be written from a
        single helper. Pre-fix it lived in TWO places (Kite postback +
        _process_broker_postback)."""
        src = ROUTES_FILE.read_text(encoding="utf-8")
        # Count occurrences of the literal "position_filled" payload key.
        # The helper itself contains 1; _process_broker_postback should
        # delegate to it (no inline duplicate). Kite postback should
        # also delegate. So the json.dumps call site count is exactly 1.
        json_dumps_with_position_filled = re.findall(
            r'json\.dumps\(\{\s*"event":\s*"position_filled"', src,
        )
        assert len(json_dumps_with_position_filled) == 1, (
            f"Expected exactly 1 inline `position_filled` broadcast "
            f"(inside _postback_broadcast_fanout); found "
            f"{len(json_dumps_with_position_filled)}. Each surface "
            f"should delegate to _postback_broadcast_fanout."
        )

    def test_book_changed_broadcast_only_in_fanout(self):
        src = ROUTES_FILE.read_text(encoding="utf-8")
        json_dumps_with_book_changed = re.findall(
            r'json\.dumps\(\{\s*"event":\s*"book_changed"', src,
        )
        assert len(json_dumps_with_book_changed) == 1, (
            f"Expected exactly 1 inline `book_changed` broadcast; "
            f"found {len(json_dumps_with_book_changed)}."
        )

    def test_raw_cache_invalidate_only_in_fanout(self):
        """`_raw_cache_invalidate("positions")` has exactly two call sites
        in orders.py:
          1. `_rco_invalidate_terminal_caches` — immediate bust on any terminal
             status (COMPLETE/CANCELLED/REJECTED/EXPIRED).
          2. `_positions_refresh_after_fill` — delayed re-bust once the broker's
             positions endpoint reflects the fill (handles propagation lag).
        Guard: if this count changes, the SSOT invariant needs re-review."""
        src = ROUTES_FILE.read_text(encoding="utf-8")
        hits = re.findall(r'_raw_cache_invalidate\("positions"\)', src)
        assert len(hits) == 2, (
            f"_raw_cache_invalidate('positions') called from "
            f"{len(hits)} sites; expected exactly 2 (terminal + fill-poll)."
        )
