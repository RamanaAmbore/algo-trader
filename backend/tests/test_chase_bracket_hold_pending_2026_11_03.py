"""`GET /api/orders/chases/active` also surfaces bracket (template-exit)
holds, so the operator can release them from the same card they already
watch for live chases (2026-11-03).

Operator: "the kill button in chase can be used to make it pending and
release for bracket on hold... this is for bracket orders only where
bracket orders can be held with admin settings."

Before this change, `list_active_chases` only ever queried
`status IN ('OPEN', 'CANCEL_FAILED')` — a FILLED parent whose exit GTTs/
wing are withheld by `order_hold_gate.hold_template_exit()` (gated by the
`hold.template_exit_released` admin setting) never reaches either status,
so it was invisible here; the operator had to go to a separate Held
Orders card to release it. This adds a second, unlocked query for FILLED
rows carrying a `hold_json` whose `category == 'template_exit'`, merged
into the same response — the frontend (`ChaseCard.svelte`) renders a
"Pending" action in place of "Kill" for these, via the SAME
`isHeldPendingReleaseState()` predicate OrderCard's Bracket pill already
uses, calling the SAME release endpoint.

Covers:
  1. A FILLED, held, not-yet-attached bracket row is included.
  2. A FILLED row with a hold_json of a DIFFERENT category is excluded —
     "bracket orders only" is the category filter, not a bare
     hold_json-is-not-null check.
  3. A draft-mode row with hold_json is excluded (mode != draft, same
     exclusion every other branch of this query already applies).
  4. An already-released row (hold_json cleared to None) is excluded.
  5. Held rows and genuinely-open chase rows are merged into one
     id-descending list, not two separately-ordered blocks.
  6. The held-rows query takes no row lock (matches the classification
     read's own unlocked contract — see test_chases_active_lock_scoping.py).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _held_row(**extra) -> SimpleNamespace:
    base = dict(
        id=900, mode="live", status="FILLED", account="ZG0790",
        symbol="NIFTY24APR25000CE", exchange="NFO", transaction_type="BUY",
        quantity=50, initial_price=None, current_limit=None, fill_price=120.0,
        attempts=1, engine="live", detail="", created_at=None,
        target_pct=None, target_abs=None, parent_order_id=None,
        basket_tag=None, template_id=5, attached_gtts_json=None,
        hold_json='{"category": "template_exit", "reason": "x", "price_policy": "n/a", "override": null, "held_at": "2026-11-03T00:00:00+00:00"}',
        filled_quantity=50, interval_seconds=None, last_attempt_at=None,
        next_attempt_at=None, broker_order_id="BO900", source=None,
        agent_id=None,
    )
    base.update(extra)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _session_with(open_rows, held_rows):
    """Classification read returns `open_rows`; the bracket-hold query
    returns `held_rows`. No reconcile helper call is triggered because
    every test here keeps `open_rows` empty."""
    results = [_Result(open_rows), _Result(held_rows)]

    async def _execute(_stmt):
        return results.pop(0)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(side_effect=_execute)
    return session


async def _run(session):
    from backend.api.routes.orders import OrdersController
    controller = OrdersController.__new__(OrdersController)
    with patch("backend.api.database.async_session", return_value=session), \
         patch("backend.api.routes.orders._chase_snapshot_paper_open_ids",
               return_value=set()), \
         patch("backend.api.routes.orders._chase_snapshot_broker_status_by_id",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders._fetch_child_order_ids",
               new=AsyncMock(return_value={})), \
         patch("backend.api.routes.orders.is_admin_request", return_value=True):
        return await OrdersController.list_active_chases.fn(
            controller, request=MagicMock(),
        )


async def test_held_bracket_row_is_included():
    row = _held_row()
    session = _session_with([], [row])
    out = await _run(session)
    assert [r.id for r in out] == [900]
    assert out[0].hold_json == row.hold_json
    assert out[0].status == "FILLED"


async def test_held_row_with_different_category_is_excluded():
    """Only TEMPLATE_EXIT holds show here — EXPIRY_CLOSE/AGENT_ORDER
    holds already set status='HELD' (a separate surface, HeldOrdersCard),
    and the query above targets FILLED specifically for brackets."""
    row = _held_row(
        hold_json='{"category": "expiry_close", "reason": "x", "price_policy": "n/a", "override": null, "held_at": "2026-11-03T00:00:00+00:00"}',
    )
    session = _session_with([], [row])
    out = await _run(session)
    assert out == []


async def test_held_rows_query_excludes_drafts_and_targets_filled_held():
    """The mode/status/hold_json filters are real SQL WHERE clauses —
    a fake in-memory session can't express filtering, so this inspects
    the actual compiled statement (same technique as
    test_chases_active_lock_scoping.py's lock assertions)."""
    spy_statements = []

    async def _execute(stmt):
        spy_statements.append(stmt)
        return _Result([])

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(side_effect=_execute)

    await _run(session)

    from sqlalchemy.dialects import postgresql
    compiled = str(spy_statements[1].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    )).upper()
    assert "MODE != 'DRAFT'" in compiled or "MODE <> 'DRAFT'" in compiled
    assert "HOLD_JSON IS NOT NULL" in compiled
    assert "STATUS = 'FILLED'" in compiled


async def test_released_row_is_excluded():
    """hold_json cleared to None on release — must never resurface as
    pending after a successful release."""
    row = _held_row(hold_json=None)
    session = _session_with([], [row])
    out = await _run(session)
    assert out == []


async def test_held_and_open_rows_merge_into_one_id_descending_list():
    open_row = SimpleNamespace(
        id=950, mode="live", status="OPEN", account="ZG0790",
        symbol="BANKNIFTY24APR50000PE", exchange="NFO", transaction_type="SELL",
        quantity=25, initial_price=200.0, current_limit=200.0, fill_price=None,
        attempts=1, engine="live", detail="", created_at=None,
        target_pct=None, target_abs=None, parent_order_id=None,
        basket_tag=None, template_id=None, attached_gtts_json=None,
        hold_json=None, filled_quantity=0, interval_seconds=None,
        last_attempt_at=None, next_attempt_at=None, broker_order_id="BO950",
        source=None, agent_id=None,
    )
    held = _held_row(id=900)
    session = _session_with([open_row], [held])
    out = await _run(session)
    assert [r.id for r in out] == [950, 900]


async def test_held_rows_query_takes_no_row_lock():
    """Same unlocked contract the classification read already has —
    see test_chases_active_lock_scoping.py."""
    spy_statements = []

    async def _execute(stmt):
        spy_statements.append(stmt)
        if len(spy_statements) == 1:
            return _Result([])
        return _Result([_held_row()])

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(side_effect=_execute)

    await _run(session)

    assert len(spy_statements) == 2
    assert spy_statements[1]._for_update_arg is None, (
        "the bracket-hold query must not take a row lock"
    )
