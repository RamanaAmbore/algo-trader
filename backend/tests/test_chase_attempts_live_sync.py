"""
Tests for the 2026-10 "live attempts count" fix.

Bug: `AlgoOrder.attempts` was only ever written once, at the very end of a
chase, by `_chase_apply_terminal_mutation`. The per-attempt sync path,
`_ch_apply_broker_sync_fields` (called from `_sync_algo_order_id`, which the
main `chase_order()` loop calls on EVERY cancel-and-replace attempt), already
wrote `broker_order_id` / `current_limit` / `last_attempt_at` /
`next_attempt_at` / `interval_seconds` on every attempt — but never
`attempts`. Result: the operator-facing "Attempts" column in
`ChaseCard.svelte` / `OrderCard.svelte` showed a stale/wrong count while a
chase was actively running (it only became correct once the chase finished).

Fix: the current attempt number is threaded through
`_sync_algo_order_id()` → `_ch_apply_broker_sync_fields()`, which now writes
`row.attempts = int(attempt)` on every cancel-and-replace sync, mirroring the
existing timing-field writes.

Covers:
  - `_ch_apply_broker_sync_fields` writes `attempts` when given a value,
    marks the row dirty, and no-ops cleanly when `attempt` is None or
    unchanged.
  - `_sync_algo_order_id` forwards `attempt=` through to the DB write.
  - End-to-end `chase_order()` loop (broker fully mocked, DB session mocked):
    the AlgoOrder row's `attempts` field reflects the CURRENT attempt number
    at each intermediate cancel-and-replace — not just once at the end.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_session(row):
    """Mirrors the helper in test_chase_sync_final_status_race.py."""
    _result = MagicMock()
    _result.scalar_one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()
    return mock_session


class _Row:
    """Plain object (not MagicMock) so `hasattr()` checks in
    `_ch_apply_broker_sync_fields` behave deterministically — a MagicMock
    auto-creates every attribute, which would make the timing-field
    `hasattr` guards always True and mask whether `attempts` specifically
    changed."""

    def __init__(self, broker_order_id=None, current_limit=None, attempts=0,
                 status="OPEN"):
        self.broker_order_id = broker_order_id
        self.current_limit = current_limit
        self.attempts = attempts
        self.status = status


# ─────────────────────────────────────────────────────────────────────────
# _ch_apply_broker_sync_fields — unit coverage of the new `attempts` write
# ─────────────────────────────────────────────────────────────────────────

def test_apply_broker_sync_fields_writes_attempts_and_marks_dirty():
    from backend.api.algo.chase import _ch_apply_broker_sync_fields

    row = _Row(broker_order_id="bo-1", current_limit=100.0, attempts=1)
    dirty = _ch_apply_broker_sync_fields(
        row, "bo-1", None, None, attempt=2,
    )

    assert row.attempts == 2
    assert dirty is True, "writing a changed attempts value must mark the row dirty"


def test_apply_broker_sync_fields_attempt_none_leaves_attempts_untouched():
    from backend.api.algo.chase import _ch_apply_broker_sync_fields

    row = _Row(broker_order_id="bo-1", current_limit=100.0, attempts=3)
    dirty = _ch_apply_broker_sync_fields(
        row, "bo-1", None, None, attempt=None,
    )

    assert row.attempts == 3, "attempt=None must not touch the existing value"
    assert dirty is False, "no field changed → not dirty"


def test_apply_broker_sync_fields_unchanged_attempt_is_not_dirty():
    from backend.api.algo.chase import _ch_apply_broker_sync_fields

    row = _Row(broker_order_id="bo-1", current_limit=100.0, attempts=2)
    dirty = _ch_apply_broker_sync_fields(
        row, "bo-1", None, None, attempt=2,
    )

    assert row.attempts == 2
    assert dirty is False, "re-syncing the SAME attempt number must not force a write"


def test_apply_broker_sync_fields_default_attempt_param_is_none():
    """Backward-compat: callers that don't pass `attempt` (none exist in
    this codebase after the fix, but defensive) must not crash or mutate
    attempts."""
    from backend.api.algo.chase import _ch_apply_broker_sync_fields

    row = _Row(broker_order_id="bo-1", current_limit=100.0, attempts=5)
    dirty = _ch_apply_broker_sync_fields(row, "bo-2", 101.0, 20)

    assert row.attempts == 5
    assert row.broker_order_id == "bo-2"
    assert dirty is True  # broker_order_id + current_limit changed


# ─────────────────────────────────────────────────────────────────────────
# _sync_algo_order_id — forwards attempt= through to the DB write
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_algo_order_id_writes_attempts_on_non_final_row():
    from backend.api.algo import chase as m

    mock_row = _Row(broker_order_id="bo-old", current_limit=100.0, attempts=1,
                     status="OPEN")
    mock_session = _mock_session(mock_row)

    with patch.object(m, "_async_session", return_value=mock_session):
        synced = await m._sync_algo_order_id(
            202, "bo-new", current_limit=105.0, interval_seconds=20, attempt=2,
        )

    assert synced is True
    assert mock_row.attempts == 2
    assert mock_row.broker_order_id == "bo-new"
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_sync_algo_order_id_never_writes_attempts_on_final_row():
    """Mirrors the existing final-status guard test — attempts must NOT be
    overwritten on an already-final row either, same as broker_order_id/
    current_limit."""
    from backend.api.algo import chase as m

    mock_row = _Row(broker_order_id="bo-old", current_limit=100.0, attempts=3,
                     status="REJECTED")
    mock_row.account = "ZG0790"
    mock_row.symbol = "NIFTY24APR25000CE"
    mock_row.exchange = "NFO"
    mock_row.transaction_type = "BUY"
    mock_row.quantity = 50
    mock_row.id = 101

    mock_session = _mock_session(mock_row)
    mock_alert = MagicMock()

    with patch.object(m, "_async_session", return_value=mock_session), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        synced = await m._sync_algo_order_id(
            101, "bo-new", current_limit=105.0, interval_seconds=20, attempt=9,
        )

    assert synced is False
    assert mock_row.attempts == 3, "a final row's attempts must never be overwritten"
    mock_session.commit.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# chase_order() end-to-end — attempts tracks the CURRENT attempt mid-chase
# ─────────────────────────────────────────────────────────────────────────

def _make_depth(bid: float = 100.00, ask: float = 100.05) -> dict:
    return {
        "buy":  [{"price": bid, "quantity": 50}],
        "sell": [{"price": ask, "quantity": 50}],
    }


@pytest.mark.asyncio
async def test_attempts_updates_live_during_chase_not_just_at_terminal():
    """Drives a real (unmocked) `_sync_algo_order_id` through 3
    cancel-and-replace attempts against a mocked DB row/session, and
    snapshots `row.attempts` at every commit. Before the fix, nothing in
    this per-attempt sync path ever touched `attempts` — the row only
    reflected attempt 1 (or whatever `attempts` was seeded at) for the
    entire chase until `_chase_apply_terminal_mutation` finally ran.

    After the fix, each commit's snapshot must equal that commit's own
    attempt number — proving the column is live-updated DURING the chase,
    not merely correct once the chase is already over.
    """
    from backend.api.algo.chase import chase_order, ChaseConfig, ChaseStatus

    algo_order_id = 555
    mock_row = _Row(broker_order_id=None, current_limit=None, attempts=0,
                     status="OPEN")

    attempts_snapshots: list[int] = []

    async def _commit_side_effect():
        attempts_snapshots.append(mock_row.attempts)

    mock_session = _mock_session(mock_row)
    mock_session.commit = AsyncMock(side_effect=_commit_side_effect)

    placed_prices: list[float] = []

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import _get_depth, _place_order, _order_status
        if fn is _get_depth:
            return _make_depth()
        if fn is _place_order:
            return _fake_place(*args)
        if fn is _order_status:
            return {"status": "CANCELLED", "filled_quantity": 0, "average_price": 0}
        return None

    async def _fake_poll(*args, **kwargs):
        # Continue chasing for the first two placements, fill on the third.
        if len(placed_prices) < 3:
            return "continue", 50, 0, 0
        return "filled", 0, 0, 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._async_session", return_value=mock_session),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._emit_chase_terminal", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_seed_pre_fill_net_qty", new_callable=AsyncMock),
        # 2026-10-23 chase robustness (fix #6) — _ch_append_superseded_id
        # now runs a real DB commit on every confirmed cancel-and-replace
        # (to flag the just-superseded broker_order_id), which would
        # otherwise add extra entries into `attempts_snapshots` below
        # (this test's `mock_session.commit` side effect fires on ANY
        # commit, not just _sync_algo_order_id's). Out of scope for this
        # test's own assertion — stubbed out, covered by its own test.
        patch("backend.api.algo.chase._ch_append_superseded_id", new_callable=AsyncMock),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = ChaseConfig(interval_seconds=0, max_attempts=5, exchange="NFO")
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
            algo_order_id=algo_order_id,
        )

    assert len(placed_prices) == 3, (
        f"expected 3 cancel-and-replace attempts, got {len(placed_prices)}"
    )
    # The DB row's attempts must track the CURRENT attempt at the time of
    # each sync — not frozen at 1, and not only correct at the very end.
    assert attempts_snapshots == [1, 2, 3], (
        f"attempts must update live on every attempt, got {attempts_snapshots}"
    )
    # Confirm the mid-chase assertion explicitly: after attempt 2 synced
    # (before the chase has terminated), the row already reads 2 — this is
    # the exact bug being fixed (previously it would still have read
    # whatever it was seeded at, e.g. 0 or 1, until the terminal mutation).
    assert attempts_snapshots[1] == 2
    # And the row is left correctly reflecting the final attempt once the
    # chase loop itself is done (independent of any later terminal-mutation
    # task).
    assert mock_row.attempts == 3
    assert result.status == ChaseStatus.CHASING, (
        "the main loop returns immediately on a 'filled' poll signal without "
        "flipping result.status itself — the caller applies the FILLED "
        "terminal status; this assertion just pins that known shape so a "
        "future refactor of _ch_handle_poll_signal's return contract is "
        "caught here too"
    )
