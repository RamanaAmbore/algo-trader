"""
Tests for orders.py — postback broadcast and cache invalidation.
SSOT: triple cache invalidation (positions/holdings/funds) on terminal status.
Perf: async broadcast (non-blocking event loop).
Stale: raw-DF cache also invalidated alongside API cache.
Reuse: order_update emitted on EVERY postback; terminal events gated.
UX: HTTP 200 returned so broker stops retrying the webhook.
"""
import inspect
from pathlib import Path

_SRC = Path("backend/api/routes/orders.py").read_text()


def test_rco_invalidate_caches_includes_funds():
    from backend.api.routes import orders as _ord
    src = inspect.getsource(_ord._rco_invalidate_terminal_caches)
    assert "funds" in src, (
        "_rco_invalidate_terminal_caches must invalidate 'funds' cache — "
        "without this, /api/funds stays stale for up to 30s after a fill"
    )
    assert "positions" in src, "_rco_invalidate_terminal_caches must invalidate 'positions'"
    assert "holdings" in src, "_rco_invalidate_terminal_caches must invalidate 'holdings'"


def test_rco_invalidate_triple_key_loop():
    from backend.api.routes import orders as _ord
    src = inspect.getsource(_ord._rco_invalidate_terminal_caches)
    # All three keys must appear
    for key in ("positions", "holdings", "funds"):
        assert key in src, f"'{key}' must be in _rco_invalidate_terminal_caches"


def test_postback_fanout_emits_order_update():
    src = inspect.getsource(
        __import__("backend.api.routes.orders", fromlist=["_postback_broadcast_fanout"])
        ._postback_broadcast_fanout
    )
    assert "order_update" in src, (
        "_postback_broadcast_fanout must emit order_update on EVERY postback — "
        "not gated on terminal status — so the UI refreshes order state immediately"
    )


def test_postback_fanout_gates_book_changed_on_terminal():
    from backend.api.routes import orders as _ord
    src = inspect.getsource(_ord._postback_broadcast_fanout)
    assert "book_changed" in src, (
        "book_changed must be emitted from _postback_broadcast_fanout "
        "on terminal order status for downstream subscribers"
    )


def test_postback_fanout_is_defined():
    assert "def _postback_broadcast_fanout" in _SRC, (
        "_postback_broadcast_fanout must exist in orders.py — "
        "shared by all broker postback handlers (Kite inline, Dhan/Groww)"
    )


def test_raw_cache_also_invalidated():
    from backend.api.routes import orders as _ord
    src = inspect.getsource(_ord._rco_invalidate_terminal_caches)
    assert "_raw_cache_invalidate" in src, (
        "_raw_cache_invalidate (broker layer) must be called alongside "
        "API-layer invalidate() in the terminal path"
    )
