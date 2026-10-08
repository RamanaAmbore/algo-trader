"""
Fix 3 (orders-page P1 audit): `GET /api/orders/chases/active` is polled
every ~3s from multiple simultaneously-mounted UI components
(`/orders`, `SymbolPanel`, `ActivityLogModal`) and used to take a
`.with_for_update()` row lock across up to 500 OPEN/CANCEL_FAILED rows
on EVERY poll — even when nothing actually needed reconciling —
contending with the chase engine's own per-attempt row locking and
with postback writers.

Fix: an unlocked classification read (`_rco_reconcile_active_rows(...,
dry_run=True)`, no row mutated) determines which rows (if any) need a
write; only if that set is non-empty does a SECOND, narrowly-scoped
`.with_for_update()` re-read fetch and mutate just those rows.

Covers:
  - Zero locking statements run when nothing needs reconciling.
  - When exactly one row needs reconciling, exactly one locked
    statement runs, scoped to that row's id (not the full batch).

The real SQLAlchemy `Select` objects built by the route are inspected
directly (`stmt._for_update_arg`, and a literal-binds compile against
the postgresql dialect) via a fake async-session spy — this avoids the
vacuous-pass trap of asserting on sqlite behaviour (sqlite silently
drops FOR UPDATE, so a source-text/behavioural check against a real
sqlite run would prove nothing about the lock itself).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeResult:
    def __init__(self, rows: list):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _StatementSpy:
    """Fake async-session context manager. Records every statement
    object passed to `execute()` (in order) and returns the next
    pre-seeded row list for that call."""

    def __init__(self, row_sequences: list[list]):
        self._queue = list(row_sequences)
        self.statements: list = []
        self.committed = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self._queue.pop(0) if self._queue else []
        return _FakeResult(rows)

    async def commit(self):
        self.committed += 1


def _paper_row(**extra) -> SimpleNamespace:
    base = dict(
        id=4242, mode="paper", status="OPEN", detail="",
        account="ZG0790", symbol="NIFTY24APR25000CE", exchange="NFO",
        transaction_type="BUY", quantity=50, product="NRML",
        broker_order_id="", filled_quantity=0, fill_price=None,
        filled_at=None, created_at=None, template_id=None,
        attached_gtts_json=None,
    )
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_no_lock_taken_when_nothing_needs_reconciling():
    """The common case: the unlocked read comes back empty (or every
    row needs no change) — zero locking statements should run."""
    from backend.api.routes.orders import OrdersController

    spy = _StatementSpy(row_sequences=[[]])
    controller = OrdersController.__new__(OrdersController)

    with patch("backend.api.database.async_session", spy), \
         patch("backend.api.routes.orders._chase_snapshot_paper_open_ids",
               return_value=set()), \
         patch("backend.api.routes.orders._chase_snapshot_broker_status_by_id",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders._fetch_child_order_ids",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        result = await OrdersController.list_active_chases.fn(
            controller, request=MagicMock(),
        )

    assert result == []
    assert len(spy.statements) == 1, (
        "only the unlocked classification read should run when there's "
        "nothing to reconcile"
    )
    assert spy.statements[0]._for_update_arg is None, (
        "the unlocked classification read must NOT take a row lock"
    )
    assert spy.committed == 0


@pytest.mark.asyncio
async def test_lock_scoped_to_only_the_row_needing_reconcile():
    """Exactly one row (a paper-mode row the paper engine no longer
    tracks) needs reconciling. Exactly one locked statement must run,
    and it must be scoped to just that row's id — not the full
    500-row batch."""
    from backend.api.routes.orders import OrdersController

    stale_row = _paper_row()
    # Same row object returned for both the unlocked AND the locked
    # read — in production these would be two distinct instances
    # (`populate_existing=True` refreshes the locked one from the DB),
    # but for this test only the STATEMENTS matter.
    spy = _StatementSpy(row_sequences=[[stale_row], [stale_row]])
    controller = OrdersController.__new__(OrdersController)

    with patch("backend.api.database.async_session", spy), \
         patch("backend.api.routes.orders._chase_snapshot_paper_open_ids",
               return_value=set()), \
         patch("backend.api.routes.orders._chase_snapshot_broker_status_by_id",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders._fetch_child_order_ids",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        await OrdersController.list_active_chases.fn(
            controller, request=MagicMock(),
        )

    assert len(spy.statements) == 2, (
        "expected the unlocked classification read plus exactly one "
        "scoped locked re-read"
    )
    assert spy.statements[0]._for_update_arg is None, (
        "the first (classification) read must stay unlocked"
    )
    assert spy.statements[1]._for_update_arg is not None, (
        "the second read must take the row lock"
    )

    from sqlalchemy.dialects import postgresql
    compiled = str(spy.statements[1].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    )).upper()
    assert "4242" in compiled, "the locked read must target the stale row's id"
    assert "LIMIT 500" not in compiled, (
        "the locked re-read must be scoped to specific ids, not the "
        "full unlocked batch size"
    )
    assert spy.committed >= 1, "the reconciled row's mutation must be committed"
