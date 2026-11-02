"""
Item 6 (backend half) of the bracket-UX plan — confirm `GET /api/orders/held`
(`HeldOrdersController.list_held`, orders_held.py) surfaces a template-exit
hold even though its row sits at `status='FILLED'` (never a separate
`status='HELD'` row — see `order_hold_gate.py:hold_template_exit()`, which
only sets `hold_json` on the already-filled parent).

Investigation finding (2026-11): the list query's `WHERE` clause already
reads `or_(AlgoOrder.status == "HELD", AlgoOrder.hold_json.isnot(None))`
(present since the original H3-H6 feature commit, 9910f6c4, long before this
session) — NOT the AND-combined filter a prior investigation assumed. This
test is therefore a regression guard, locking in behaviour that already
works, rather than a fix. See the real fix sibling for the genuine gap this
session found: `test_order_hold_gate.py`'s "Pending" rename tests.

Uses a real in-process SQLite `algo_orders` table (same pattern as
test_draft_sweep.py) rather than a mocked `s.execute()`, specifically
because a mock would echo back whatever rows the test hands it regardless
of the real WHERE clause — it would prove nothing about the OR-vs-AND
distinction this test exists to pin.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sqlite_session_factory():
    """Async session factory backed by an in-process SQLite DB, seeded
    with the real `algo_orders` table (AlgoOrder has no JSONB columns so
    it compiles cleanly on SQLite — same convention as test_draft_sweep.py)."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from backend.api.models import AlgoOrder

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(AlgoOrder.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_order(factory, **overrides) -> int:
    from backend.api.models import AlgoOrder

    defaults = dict(
        account="ZG0790", symbol="GOLD26OCT142000PE", exchange="MCX",
        transaction_type="BUY", quantity=10, product="NRML",
        status="OPEN", engine="live", mode="live",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    async with factory() as s:
        row = AlgoOrder(**defaults)
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


class TestHeldOrdersListSurfacesTemplateExitHolds:

    async def test_filled_template_exit_hold_row_is_listed(self, sqlite_session_factory):
        """The exact order #1377 shape — parent already FILLED, hold_json
        carries the template_exit category, no separate HELD row exists."""
        from backend.api.routes.orders_held import HeldOrdersController

        hold_json = (
            '{"category": "template_exit", "reason": '
            '"template exits pending release for GOLD26OCT142000PE"}'
        )
        order_id = await _insert_order(
            sqlite_session_factory, status="FILLED", hold_json=hold_json,
        )

        ctrl = HeldOrdersController(owner=None)
        mock_request = MagicMock()
        with patch("backend.api.database.async_session", sqlite_session_factory), \
             patch("backend.api.routes.orders_held.is_admin_request", return_value=True):
            resp = await HeldOrdersController.list_held.fn(ctrl, request=mock_request)

        ids = [r["id"] for r in resp["held"]]
        assert order_id in ids, (
            "a FILLED row with hold_json set (template_exit category) must "
            "appear in /api/orders/held — this is what made order #1377's "
            "held TP invisible without a direct server-side script"
        )

    async def test_genuine_held_expiry_close_row_is_still_listed(self, sqlite_session_factory):
        """Regression guard — adding template-exit visibility must not
        narrow the query and drop the pre-existing status='HELD' case
        (expiry_close / agent_order), which release_held_order's own
        status gate (`if row.status != "HELD"`) still depends on."""
        from backend.api.routes.orders_held import HeldOrdersController

        hold_json = '{"category": "expiry_close", "reason": "expiry close"}'
        order_id = await _insert_order(
            sqlite_session_factory, status="HELD", hold_json=hold_json,
        )

        ctrl = HeldOrdersController(owner=None)
        mock_request = MagicMock()
        with patch("backend.api.database.async_session", sqlite_session_factory), \
             patch("backend.api.routes.orders_held.is_admin_request", return_value=True):
            resp = await HeldOrdersController.list_held.fn(ctrl, request=mock_request)

        ids = [r["id"] for r in resp["held"]]
        assert order_id in ids

    async def test_both_kinds_listed_together_none_masked_out(self, sqlite_session_factory):
        """Both rows from the two tests above, inserted together — proves
        the OR condition, not just two isolated single-row cases."""
        from backend.api.routes.orders_held import HeldOrdersController

        filled_id = await _insert_order(
            sqlite_session_factory, status="FILLED",
            hold_json='{"category": "template_exit", "reason": "pending release"}',
        )
        held_id = await _insert_order(
            sqlite_session_factory, status="HELD",
            hold_json='{"category": "expiry_close", "reason": "expiry close"}',
        )
        # A normal, non-held order must NOT appear.
        normal_id = await _insert_order(sqlite_session_factory, status="OPEN", hold_json=None)

        ctrl = HeldOrdersController(owner=None)
        mock_request = MagicMock()
        with patch("backend.api.database.async_session", sqlite_session_factory), \
             patch("backend.api.routes.orders_held.is_admin_request", return_value=True):
            resp = await HeldOrdersController.list_held.fn(ctrl, request=mock_request)

        ids = {r["id"] for r in resp["held"]}
        assert filled_id in ids
        assert held_id in ids
        assert normal_id not in ids
