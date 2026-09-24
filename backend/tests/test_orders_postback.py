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


# ---------------------------------------------------------------------------
# `broker=` threading (2026-09 Day P&L audit item #6 — Groww MCX postback
# quantity double-conversion fix). `_postback_broadcast_fanout` requires a
# `broker` kwarg (no default) so `_mcx_postback_qty_to_contracts` can skip
# the lots->contracts multiply for Groww, which ships CONTRACTS for every
# exchange including MCX (unlike Kite/Dhan, which ship lots for MCX).
# ---------------------------------------------------------------------------

def test_postback_fanout_requires_broker_kwarg():
    """_postback_broadcast_fanout's `broker` parameter has no default —
    every caller must explicitly state which broker's postback this is."""
    import inspect
    from backend.api.routes import orders as _ord

    sig = inspect.signature(_ord._postback_broadcast_fanout)
    assert "broker" in sig.parameters, (
        "_postback_broadcast_fanout must accept a `broker` kwarg"
    )
    assert sig.parameters["broker"].default is inspect.Parameter.empty, (
        "`broker` must have NO default — a future caller must not silently "
        "inherit Kite's lots-conversion behaviour for a broker that ships "
        "contracts (e.g. Groww)"
    )


def test_process_broker_postback_forwards_broker_id_to_fanout():
    """_process_broker_postback (Dhan/Groww shared path) must forward its
    own `broker_id` param through to `_postback_broadcast_fanout(broker=...)`
    — not hardcode 'kite' or omit it."""
    import inspect
    from backend.api.routes import orders_postback as _pb

    src = inspect.getsource(_pb._process_broker_postback)
    assert "broker=broker_id" in src, (
        "_process_broker_postback must call _postback_broadcast_fanout("
        "..., broker=broker_id, ...) so a Dhan/Groww postback carries its "
        "real broker identity through to the MCX qty conversion gate"
    )


def test_kite_postback_handler_passes_broker_kite():
    """kite_postback_handler's inline call must pass broker='kite'."""
    import inspect
    from backend.api.routes import orders_postback as _pb

    src = inspect.getsource(_pb.kite_postback_handler)
    assert 'broker="kite"' in src, (
        "kite_postback_handler must call _postback_broadcast_fanout("
        "..., broker=\"kite\", ...)"
    )
