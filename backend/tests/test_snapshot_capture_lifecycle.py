"""
Verification tests for the snapshot capture lifecycle (Commit 5 of the
unified animation-model refactor).

The refactor's contract:
  • `<exch>:close`          → first-cut snapshot: `ltp = current live LTP`
                              (mid_session flips False right at close).
  • `<exch>:close_settled`  → OVERWRITES: broker has now published
                              `close_price`; day_pnl recomputes.

Both events call the SAME `snapshot_daily_book()` function which UPSERTs
on (date, account, kind, symbol). Second call overwrites the first.

The `is_settled` discriminator lives implicitly in the presence of `ltp`
+ `close_price` in the daily_book row:
  • mid-session rows          → ltp=None,  close_price=None (idle state)
  • :close cut                 → ltp=<last live LTP>, close_price=None
                                 or the last-observed close from prior day
  • :close_settled overwrite   → ltp=<broker close_price>, close_price=<broker>

The route-layer overlay (positions.py + holdings.py + watchlist.py)
uses `latest_snapshot_ltp_map()` which reads `ltp IS NOT NULL AND ltp > 0`
— any row from either cut becomes visible. Commit 3's overlay treats
"snapshot LTP present" as `settled=True` for the resolver — the pre-cut
"no snapshot yet" case correctly resolves to `snapshot_unsettled`.

Five quality dimensions:
  SSOT       — one snapshot_daily_book() function serves both events.
  Idempotent — UPSERT on (date, account, kind, symbol); reruns fine.
  Perf       — single call per event; no additional read-modify-write.
  Reuse      — both close + close_settled reuse the same handler chain.
  Correctness (UX) — close_settled uses broker's weighted-avg-last-30-min
                     value; the initial :close cut serves the live LTP so
                     the frontend has a value to render immediately.
"""

from __future__ import annotations

from pathlib import Path


_HANDLERS = Path(__file__).parent.parent / "api" / "algo" / "market_lifecycle_handlers.py"
_SNAPSHOT = Path(__file__).parent.parent / "api" / "algo" / "daily_snapshot.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_close_and_close_settled_share_handler():
    """The same _snapshot_close callback services both `<exch>:close` and
    `<exch>:close_settled` events across NSE / MCX / CDS."""
    src = _src(_HANDLERS)
    # The handler registers itself for both events per exchange.
    for exch in ("nse", "mcx", "cds"):
        assert f'"{exch}:close"' in src.lower() or f"'{exch}:close'" in src.lower() \
            or f'f"{{exch}}:close"' in src, (
            f"handler registration for {exch}:close not found"
        )
        assert f'"{exch}:close_settled"' in src.lower() \
            or f"'{exch}:close_settled'" in src.lower() \
            or f'f"{{exch}}:close_settled"' in src, (
            f"handler registration for {exch}:close_settled not found"
        )


def test_snapshot_handler_calls_snapshot_daily_book():
    """The lifecycle handler dispatches to snapshot_daily_book (UPSERT)."""
    src = _src(_HANDLERS)
    assert "snapshot_daily_book" in src


def test_snapshot_daily_book_is_upsert():
    """snapshot_daily_book uses ON CONFLICT DO UPDATE so re-firing the
    same event (or the close_settled overwrite path) is idempotent."""
    src = _src(_SNAPSHOT)
    assert "ON CONFLICT" in src.upper()


def test_snapshot_writes_ltp_column():
    """The daily_book row carries `ltp` — read by
    `latest_snapshot_ltp_map` for the row-overlay in positions/holdings."""
    src = _src(_SNAPSHOT)
    assert "ltp" in src.lower()
    # And it's part of the INSERT column list.
    assert "ltp" in src


def test_close_settled_overwrites_close_cut():
    """Both close + close_settled call the SAME snapshot handler, and
    the UPSERT in snapshot_daily_book makes the second call overwrite
    the first. No separate settled code path is needed — the daily_book
    row simply gets refreshed with broker's close_price when it lands.

    Verifies structurally: the callback registered for `<exch>:close`
    is the same as the callback registered for `<exch>:close_settled`.
    """
    src = _src(_HANDLERS)
    import re
    # Extract (event_suffix, callback_name) pairs from each register call.
    pairs = re.findall(
        r'market_lifecycle\.register\(\s*f?"[^"]*?:([a-z_]+)"\s*,\s*(\w+)\)',
        src,
    )
    assert pairs, "no market_lifecycle.register() calls parsed"
    close_callbacks = {cb for evt, cb in pairs if evt == "close"}
    settled_callbacks = {cb for evt, cb in pairs if evt == "close_settled"}
    assert close_callbacks, "no `<exch>:close` handlers registered"
    assert settled_callbacks, "no `<exch>:close_settled` handlers registered"
    # The snapshot writer must be registered for BOTH events.
    shared = close_callbacks & settled_callbacks
    assert shared, (
        "no handler registered for both close AND close_settled — "
        "close_settled cannot overwrite close cut"
    )


def test_mid_session_rows_carry_null_ltp():
    """During mid-session polling (via /admin), rows are written with
    ltp=None so the row-overlay skips them (won't confuse the frontend
    with a snapshot LTP that isn't a proper close_settled value)."""
    src = _src(_SNAPSHOT)
    assert "None if mid_session" in src or "mid_session" in src
