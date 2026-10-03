"""Sprint 2a fix — AlgoOrderInfo now surfaces broker_order_id/source/
agent_id (docs/proposals/SPRINT2_LAYER_INTEGRATION.md §1 finding 2, §2).

Pre-fix, `AlgoOrderInfo` exposed neither `order_id` nor `broker_order_id`,
so the frontend's broker-row/algo-row dedup (OrderBook.svelte, ChaseCard)
could never match a live, algo-tracked order against its bare broker
counterpart — the same row showed up twice. `source`/`agent_id` were also
real AlgoOrder columns (Sprint 1a/1b-i) not yet surfaced on this response
shape.

These tests cover the single construction site, `_chase_row_to_info` in
backend/api/routes/orders.py, which both `list_active_chases` and
`list_orders` delegate to.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.api.routes.orders import _chase_row_to_info
from backend.api.routes.orders_helpers import AlgoOrderInfo


def _make_row(**overrides):
    base = dict(
        id=1, account="ZG0790", symbol="NIFTY24DECFUT", exchange="NFO",
        transaction_type="BUY", quantity=50, initial_price=100.0,
        current_limit=None, fill_price=None, attempts=0,
        status="OPEN", engine="live", mode="live", detail=None,
        created_at=None, target_pct=None, target_abs=None,
        parent_order_id=None, basket_tag=None, template_id=None,
        attached_gtts_json=None, filled_quantity=None,
        interval_seconds=None, last_attempt_at=None, next_attempt_at=None,
        broker_order_id=None, source=None, agent_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _identity_mask(acct: str) -> str:
    return acct


def test_algo_order_info_struct_has_new_fields_with_safe_defaults():
    """The msgspec Struct itself must declare the fields with None
    defaults so legacy construction sites (if any) don't break."""
    info = AlgoOrderInfo(
        id=1, account="ZG0790", symbol="NIFTY24DECFUT", exchange="NFO",
        transaction_type="BUY", quantity=50, initial_price=100.0,
        attempts=0, status="OPEN", engine="live", mode="live",
        detail=None, created_at="",
    )
    assert info.broker_order_id is None
    assert info.source is None
    assert info.agent_id is None


def test_chase_row_to_info_populates_broker_order_id():
    row = _make_row(broker_order_id="251003000012345")
    info = _chase_row_to_info(row, _identity_mask, {})
    assert info.broker_order_id == "251003000012345"


def test_chase_row_to_info_populates_source_and_agent_id():
    row = _make_row(source="chase", agent_id=7)
    info = _chase_row_to_info(row, _identity_mask, {})
    assert info.source == "chase"
    assert info.agent_id == 7


def test_chase_row_to_info_defaults_to_none_when_columns_unset():
    """Legacy rows predating the Sprint 1a migration have NULL
    source/agent_id/broker_order_id — must surface as None, not raise
    or coerce to some other falsy sentinel."""
    row = _make_row()
    info = _chase_row_to_info(row, _identity_mask, {})
    assert info.broker_order_id is None
    assert info.source is None
    assert info.agent_id is None


def test_chase_row_to_info_tolerates_missing_attrs_via_getattr():
    """source/agent_id use getattr() with a None default so a mock/row
    object missing the attribute entirely (shouldn't happen post-
    migration, but defensive) doesn't raise AttributeError."""
    row = _make_row()
    del row.source
    del row.agent_id
    info = _chase_row_to_info(row, _identity_mask, {})
    assert info.source is None
    assert info.agent_id is None
