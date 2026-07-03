"""
Unified price resolver — single decision point for every symbol's
current_price + price_source + is_animating triad.

Jul 2026 architectural refactor. Every operator-visible surface that
shows a per-symbol price (positions, holdings, movers, watchlist quotes,
NAV strip) routes its per-row LTP through `resolve_current_price` so
the same rules apply to all three surfaces:

  Exchange OPEN
      → (live_ltp, "live", is_animating=True)

  Exchange CLOSED and settled=True and snapshot_close available
      → (snapshot_close, "snapshot_settled", is_animating=False)

  Exchange CLOSED and (settled=False OR snapshot_close missing)
      → (snapshot_last_ltp or None, "snapshot_unsettled",
         is_animating=False)

  Exchange CLOSED and NEITHER snapshot value present
      → (None, "snapshot_unsettled", is_animating=False)

Design notes:

1. **No I/O.** The resolver is pure — it receives all inputs (exchange
   state, snapshot values) and returns the tuple. Callers fetch state
   once per response and reuse across N rows to avoid N×holidays lookups.

2. **Additive to the existing overlay.** positions.py + holdings.py
   still build their snapshot map via `latest_snapshot_ltp_map(kind)`
   and iterate rows; they now call this resolver per-row instead of
   inlining the branch logic. The overlay dispatchers stay the same
   shape.

3. **Settled discriminator.** Callers derive `settled` from the source
   material — for daily_book snapshot readers, `settled` is
   `close_price IS NOT NULL` (Kite's post-45m weighted-avg-last-30-min
   value). For the row-overlay path in positions.py / holdings.py, the
   resolver caller passes `settled=True` when a snapshot LTP is present
   in `latest_snapshot_ltp_map` — the map itself is built from rows
   with non-null `ltp` in the latest batch. Pre-settled rows carry
   `ltp=None` in daily_book and simply don't appear in the map, which
   maps to the `snapshot_unsettled` branch here.

4. **is_animating gate.** True only when the exchange is open. The
   frontend's cell renderer reads this flag from the row payload and
   suppresses tick-flash / freshness shimmer when False. This is the
   canonical gate — no per-cell client-side market-hours logic.

5. **No cross-symbol dispatch.** One symbol, one resolver call. The
   caller owns the loop and the map lookups. Keeps the resolver
   O(1) per invocation + trivial to unit test.
"""

from __future__ import annotations

from typing import Optional


def resolve_current_price(
    *,
    exchange_open: bool,
    live_ltp: Optional[float],
    snapshot_close: Optional[float] = None,
    snapshot_last_ltp: Optional[float] = None,
    settled: bool = False,
) -> tuple[Optional[float], str, bool]:
    """Return the (current_price, price_source, is_animating) triad for
    one symbol given its inputs.

    Args:
        exchange_open      True when the symbol's exchange is currently
                           open (market hours + not holiday).
        live_ltp           Live LTP from broker / ticker. Required when
                           exchange_open=True. Ignored when closed.
        snapshot_close     Broker-published close_price (post-settle
                           window). Used when exchange closed + settled.
        snapshot_last_ltp  Last live LTP captured before close (pre-
                           settle window). Fallback when settled=False
                           OR snapshot_close missing.
        settled            True when the settle-window has elapsed AND
                           broker has published close_price. The
                           daily_book close_settled path sets this.
                           False during the initial `<exch>:close` cut
                           and the 45-minute pre-settle window.

    Returns:
        Three-tuple `(current_price, price_source, is_animating)`.

        current_price   float or None — None only when exchange closed
                        AND no snapshot value of any kind is available.
        price_source    one of "live" / "snapshot_settled" /
                        "snapshot_unsettled".
        is_animating    True iff exchange_open is True.

    Backward-compat: legacy `"snapshot"` value is not emitted here.
    Consumers that still tolerate the old value should map it to
    "snapshot_settled" at the routing layer (positions.py /
    holdings.py did this for one commit only; Commit 3 flips them
    to consume the resolver directly).
    """
    if exchange_open:
        # Live path — return whatever LTP the caller supplied, even if
        # None (during a broker outage). The caller is responsible for
        # LKG substitution before invoking the resolver.
        return live_ltp, "live", True

    # Exchange closed. Prefer settled close_price when available.
    if settled and snapshot_close is not None:
        return float(snapshot_close), "snapshot_settled", False

    # Pre-settle or no close_price yet. Fall back to the last observed
    # live LTP if we captured one before close.
    if snapshot_last_ltp is not None:
        return float(snapshot_last_ltp), "snapshot_unsettled", False

    # Nothing to serve — the frontend renders the row with current_price=
    # None and a "SNAP" chip indicating no snapshot yet.
    return None, "snapshot_unsettled", False
