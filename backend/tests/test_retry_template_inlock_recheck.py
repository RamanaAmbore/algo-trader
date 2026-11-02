"""test_retry_template_inlock_recheck.py

Covers audit finding T13 (2026-10-23 template/bracket audit, cluster 2A):
`_rco_run_template_attach` (backend/api/routes/orders.py) never re-checked
`attached_gtts_json` INSIDE its own per-row lock. The retry route's own
precheck (`_retry_precheck_row`) reads `attached_gtts_json` BEFORE the lock
is acquired — if a concurrent fill-triggered attach
(`_fire_template_attach_on_fill`, orders_place.py) wins the lock first and
persists `attached_gtts_json` in the window between that precheck and the
retry path actually acquiring the lock, the retry path used to proceed to
call `apply_template_to_order` a SECOND time, placing a duplicate live
GTT/wing set and orphaning the first one.

Fix: `_rco_run_template_attach` now re-reads `attached_gtts_json` fresh
from the DB (via `_rco_attached_gtts_already_set`) INSIDE the lock and
bails with `None` before ever calling `apply_template_to_order`, mirroring
the fill-triggered path's own in-lock idempotency check
(`orders_place.py:_opp_load_row_for_attach`).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_retry_attach_skips_when_concurrent_fill_won_the_lock_first():
    """Pre-fix: this test fails because apply_template_to_order is called
    even though attached_gtts_json was set by a 'concurrent' attach that
    landed while this call waited on the lock — the old code never
    re-checked inside the lock."""
    from backend.api.routes import orders as orders_mod

    row = MagicMock()
    row.id = 4242
    row.template_id = 11
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "SELL"
    row.product = "NRML"
    row.mode = "live"
    row.fill_price = 100.0
    row.initial_price = 100.0
    row.template_overrides_json = None

    apply_template_to_order_mock = AsyncMock(return_value=MagicMock())

    with patch.object(orders_mod, "_get_template_attach_lock",
                       new=AsyncMock(return_value=asyncio.Lock())), \
         patch.object(orders_mod, "_rco_attached_gtts_already_set",
                      new=AsyncMock(return_value=True)), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=apply_template_to_order_mock), \
         patch.object(orders_mod, "_retry_effective_parent_qty",
                      new=AsyncMock(return_value=50)):
        result = await orders_mod._rco_run_template_attach(row)

    assert result is None, (
        "retry-attach must bail with None when a concurrent fill-triggered "
        "attach already set attached_gtts_json while this call waited for "
        "the per-row lock"
    )
    apply_template_to_order_mock.assert_not_called()


@pytest.mark.asyncio
async def test_retry_attach_proceeds_when_not_concurrently_attached():
    """Sanity: the normal (non-race) path still calls through when the
    in-lock re-check finds attached_gtts_json still unset.

    2026-11 — `_rco_run_template_attach` now also builds + persists the
    attach payload under the SAME lock acquisition (see that function's
    own docstring), so it returns a `(result, persist_state)` tuple, not
    a bare `result`. `sentinel_result` is configured as a real "nothing
    to attach" outcome (no plan, no wing) so the payload-build step is
    deterministic and the persist machinery (which is exercised by its
    own dedicated tests) never runs here."""
    from backend.api.routes import orders as orders_mod

    row = MagicMock()
    row.id = 4243
    row.template_id = 11
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "SELL"
    row.product = "NRML"
    row.mode = "live"
    row.fill_price = 100.0
    row.initial_price = 100.0
    row.template_overrides_json = None

    sentinel_result = MagicMock()
    sentinel_result.plan = None
    sentinel_result.wing_order_id = None
    sentinel_result.gtt_ids = []
    sentinel_result.errors = []
    apply_template_to_order_mock = AsyncMock(return_value=sentinel_result)

    with patch.object(orders_mod, "_get_template_attach_lock",
                       new=AsyncMock(return_value=asyncio.Lock())), \
         patch.object(orders_mod, "_rco_attached_gtts_already_set",
                      new=AsyncMock(return_value=False)), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=apply_template_to_order_mock), \
         patch.object(orders_mod, "_retry_effective_parent_qty",
                      new=AsyncMock(return_value=50)):
        outcome = await orders_mod._rco_run_template_attach(row)

    assert outcome == (sentinel_result, "empty")
    apply_template_to_order_mock.assert_called_once()


@pytest.mark.asyncio
async def test_retry_attach_persists_payload_under_the_same_lock():
    """The non-empty-payload, successful-persist path: the DB write now
    happens INSIDE `_rco_run_template_attach`'s own lock acquisition
    (the actual bug fix), returning `(result, "persisted")`."""
    from backend.api.routes import orders as orders_mod

    row = MagicMock()
    row.id = 4244
    row.template_id = 11
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "SELL"
    row.product = "NRML"
    row.mode = "live"
    row.fill_price = 100.0
    row.initial_price = 100.0
    row.template_overrides_json = None

    sentinel_result = MagicMock()
    sentinel_result.plan = None
    sentinel_result.wing_order_id = "W1"
    sentinel_result.wing_chased = False
    sentinel_result.gtt_ids = []
    sentinel_result.errors = []
    apply_template_to_order_mock = AsyncMock(return_value=sentinel_result)
    persist_mock = AsyncMock(return_value=True)

    with patch.object(orders_mod, "_get_template_attach_lock",
                       new=AsyncMock(return_value=asyncio.Lock())), \
         patch.object(orders_mod, "_rco_attached_gtts_already_set",
                      new=AsyncMock(return_value=False)), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=apply_template_to_order_mock), \
         patch.object(orders_mod, "_retry_effective_parent_qty",
                      new=AsyncMock(return_value=50)), \
         patch("backend.api.routes.orders_place._opp_persist_with_retry",
               new=persist_mock):
        outcome = await orders_mod._rco_run_template_attach(row)

    assert outcome == (sentinel_result, "persisted")
    persist_mock.assert_awaited_once()
    _call_args = persist_mock.await_args.args
    assert _call_args[0] == row.id
    assert _call_args[1] == [
        {"kind": "wing", "label": "Wing", "id": "W1", "chased": False},
    ]


@pytest.mark.asyncio
async def test_rco_attached_gtts_already_set_reads_fresh_from_db():
    """_rco_attached_gtts_already_set must query the DB directly (not the
    caller's possibly-stale in-memory row object) and return a plain bool."""
    from backend.api.routes import orders as orders_mod

    fake_session = MagicMock()
    fake_result = MagicMock()
    fake_result.scalar_one_or_none.return_value = '[{"kind": "gtt", "id": "1"}]'
    fake_session.execute = AsyncMock(return_value=fake_result)

    fake_session_cm = MagicMock()
    fake_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.database.async_session",
               return_value=fake_session_cm):
        out = await orders_mod._rco_attached_gtts_already_set(4242)

    assert out is True

    fake_result.scalar_one_or_none.return_value = None
    with patch("backend.api.database.async_session",
               return_value=fake_session_cm):
        out2 = await orders_mod._rco_attached_gtts_already_set(4243)
    assert out2 is False


# ── 2026-11 fix — real-money race closure ───────────────────────────────
#
# Covers the audit finding this file was dispatched to fix:
# `_rco_run_template_attach`'s per-row lock used to release the instant
# `apply_template_to_order` returned, with the caller (`retry_template`)
# building + persisting the attach payload with NO lock held at all. In
# that window a concurrent fill-triggered attach (`_fire_template_
# attach_on_fill`, orders_place.py — a late postback or chase-terminal
# fill for the SAME row) could win the now-free lock, see
# `attached_gtts_json` still NULL (this call's own write hadn't
# committed yet), and place a SECOND live GTT/wing set.
#
# Fix: broker placement AND the DB persist now run inside ONE continuous
# `async with _row_lock:` block in `_rco_run_template_attach`.

@pytest.mark.asyncio
async def test_retry_and_fill_triggered_attach_never_place_broker_orders_twice():
    """End-to-end proof, using the REAL per-row lock registry (shared by
    both call sites via `_get_template_attach_lock`), that the retry
    path and a concurrent fill-triggered attach for the SAME row can
    never both place broker orders.

    The retry path is paused mid-persist (after broker placement has
    already happened, before the DB write completes) — exactly the
    window the pre-fix code left unlocked. A concurrent fill-triggered
    attach is then started and proven to be genuinely BLOCKED on the
    lock (not merely non-interleaved by scheduling luck) before the
    retry path's persist is allowed to complete. Only once the retry
    path has released the lock does the fill-triggered attach proceed —
    by which point `attached_gtts_json` is already set, so it must bail
    without ever calling `apply_template_to_order` again.
    """
    from backend.api.routes import orders as orders_mod
    from backend.api.routes import orders_place as opl_mod

    # Unique row id — avoids any collision with the shared in-process
    # per-row lock registry other tests in this process may have touched.
    _ROW_ID = 918273645

    row = MagicMock()
    row.id = _ROW_ID
    row.template_id = 11
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "SELL"
    row.product = "NRML"
    row.mode = "live"
    row.fill_price = 100.0
    row.initial_price = 100.0
    row.template_overrides_json = None
    row.agent_id = None

    fake_result = MagicMock()
    fake_result.plan = None
    fake_result.wing_order_id = "W1"
    fake_result.wing_chased = False
    fake_result.gtt_ids = []
    fake_result.errors = []

    apply_template_to_order_mock = AsyncMock(return_value=fake_result)

    # Simulated DB state for attached_gtts_json — shared between the
    # retry path's in-lock recheck and the fill path's own idempotency
    # read, exactly as the real column would be shared by both.
    attached_state = {"json": None}
    in_persist = asyncio.Event()
    release_persist = asyncio.Event()

    async def _fake_attached_gtts_already_set(row_id):
        return bool(attached_state["json"])

    async def _fake_persist_with_retry(parent_row_id, attached, fill_price):
        # Pauses HERE, mid-critical-section — broker placement has
        # already happened (apply_template_to_order already returned)
        # but the DB write has not. This is exactly the window the
        # pre-fix code left unlocked.
        in_persist.set()
        await release_persist.wait()
        attached_state["json"] = attached
        return True

    async def _fake_load_row_for_attach(parent_row_id):
        if attached_state["json"]:
            return None
        return {"overrides": {}, "agent_id": None}

    with patch("backend.api.algo.template_attach.apply_template_to_order",
               new=apply_template_to_order_mock), \
         patch.object(orders_mod, "_rco_attached_gtts_already_set",
                      new=_fake_attached_gtts_already_set), \
         patch.object(orders_mod, "_retry_effective_parent_qty",
                      new=AsyncMock(return_value=50)), \
         patch.object(opl_mod, "_opp_persist_with_retry",
                      new=_fake_persist_with_retry), \
         patch.object(opl_mod, "_opp_load_row_for_attach",
                      new=_fake_load_row_for_attach), \
         patch("backend.api.algo.order_hold_gate.template_exit_held",
               return_value=False), \
         patch("backend.api.algo.order_hold_gate.template_exit_override",
               new=AsyncMock(return_value=None)):

        retry_task = asyncio.create_task(
            orders_mod._rco_run_template_attach(row)
        )
        await in_persist.wait()

        fill_task = asyncio.create_task(
            opl_mod._fire_template_attach_on_fill(
                parent_row_id=_ROW_ID, parent_account="ZG0790",
                parent_symbol="NIFTY25JULFUT", parent_exchange="NFO",
                parent_side="SELL", parent_qty=50, fill_price=100.0,
                template_id=11, mode="live",
            )
        )
        # Give the fill-triggered task every opportunity to run ahead if
        # it were NOT genuinely blocked by the lock.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not fill_task.done(), (
            "the fill-triggered attach must be blocked on the per-row "
            "lock while the retry path is still mid-persist — if this "
            "assertion fails, the two paths are not actually contending "
            "on the same lock and the rest of this test proves nothing"
        )

        release_persist.set()
        retry_outcome = await retry_task
        fill_result = await fill_task

    assert retry_outcome == (fake_result, "persisted")
    assert fill_result is None, (
        "the fill-triggered attach must see attached_gtts_json already "
        "set (by the retry path's own persist) once it finally acquires "
        "the lock, and must bail WITHOUT calling apply_template_to_order "
        "again"
    )
    assert apply_template_to_order_mock.await_count == 1, (
        "apply_template_to_order must be called exactly once across "
        "both the retry-triggered and fill-triggered attach attempts "
        "for the SAME row — a second call means a duplicate live "
        "GTT/wing set was placed at the broker"
    )


# ── _rco_annotate_retry_detail ───────────────────────────────────────────
# 2026-11 fix — this is the detail-note best-effort helper
# `_rco_run_template_attach` calls on its own, outside the caller's
# session (see that function's docstring for why it needs its own
# session now). Both branches below were previously unreached by any
# test.

@pytest.mark.asyncio
async def test_annotate_retry_detail_writes_note_and_commits():
    from backend.api.routes import orders as orders_mod

    fake_row = MagicMock()
    fake_row.detail = None
    fake_result_obj = MagicMock()
    fake_result_obj.scalar_one_or_none.return_value = fake_row
    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=fake_result_obj)
    fake_session.commit = AsyncMock()
    fake_session_cm = MagicMock()
    fake_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.api.database.async_session",
               return_value=fake_session_cm):
        await orders_mod._rco_annotate_retry_detail(
            4321, ["chain scan empty", "ignored third error"],
        )

    assert "· retry attach: " in fake_row.detail
    assert "chain scan empty" in fake_row.detail
    fake_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_annotate_retry_detail_swallows_session_failure():
    """A DB failure while writing the cosmetic detail note must never
    propagate — the exits are already placed and persisted by the time
    this helper runs; a note-write failure must not look like an
    attach failure to the caller."""
    from backend.api.routes import orders as orders_mod

    with patch("backend.api.database.async_session",
               side_effect=RuntimeError("DB down")):
        # Must not raise.
        await orders_mod._rco_annotate_retry_detail(4321, ["boom"])


@pytest.mark.asyncio
async def test_rco_run_template_attach_annotates_detail_only_when_errors_present():
    """`_rco_run_template_attach` must call `_rco_annotate_retry_detail`
    on the successful-persist path ONLY when `result.errors` is
    non-empty, and must not call it at all when there are no errors."""
    from backend.api.routes import orders as orders_mod

    def _row(row_id):
        row = MagicMock()
        row.id = row_id
        row.template_id = 11
        row.account = "ZG0790"
        row.symbol = "NIFTY25JULFUT"
        row.exchange = "NFO"
        row.transaction_type = "SELL"
        row.product = "NRML"
        row.mode = "live"
        row.fill_price = 100.0
        row.initial_price = 100.0
        row.template_overrides_json = None
        return row

    async def _run_with_errors(errors):
        fake_result = MagicMock()
        fake_result.plan = None
        fake_result.wing_order_id = "W1"
        fake_result.wing_chased = False
        fake_result.gtt_ids = []
        fake_result.errors = errors
        annotate_mock = AsyncMock()
        with patch.object(orders_mod, "_get_template_attach_lock",
                           new=AsyncMock(return_value=asyncio.Lock())), \
             patch.object(orders_mod, "_rco_attached_gtts_already_set",
                          new=AsyncMock(return_value=False)), \
             patch("backend.api.algo.template_attach.apply_template_to_order",
                   new=AsyncMock(return_value=fake_result)), \
             patch.object(orders_mod, "_retry_effective_parent_qty",
                          new=AsyncMock(return_value=50)), \
             patch("backend.api.routes.orders_place._opp_persist_with_retry",
                   new=AsyncMock(return_value=True)), \
             patch.object(orders_mod, "_rco_annotate_retry_detail",
                          new=annotate_mock):
            outcome = await orders_mod._rco_run_template_attach(_row(1))
        assert outcome == (fake_result, "persisted")
        return annotate_mock

    annotate_with_errors = await _run_with_errors(["chain scan empty"])
    annotate_with_errors.assert_awaited_once()

    annotate_no_errors = await _run_with_errors([])
    annotate_no_errors.assert_not_called()
