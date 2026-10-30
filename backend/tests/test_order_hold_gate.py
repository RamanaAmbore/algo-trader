"""Expiry-close gate: held by default, released only by the global setting."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.algo import order_hold_gate as gate
from backend.api.algo.order_hold import HoldCategory, parse_hold_record


def test_expiry_close_held_by_default(monkeypatch):
    monkeypatch.setattr(gate, "get_bool", lambda key, default=False: default)
    assert gate.expiry_close_held() is True


def test_expiry_close_released_when_setting_on(monkeypatch):
    monkeypatch.setattr(gate, "get_bool",
                        lambda key, default=False: True if key == gate.EXPIRY_CLOSE_RELEASED_KEY else default)
    assert gate.expiry_close_held() is False


def test_record_held_order_returns_none_on_db_failure(monkeypatch):
    import backend.api.database as db

    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(db, "async_session", _Boom())
    out = asyncio.run(gate.record_held_order(
        HoldCategory.EXPIRY_CLOSE,
        account="ZG0790", symbol="CRUDEOIL26OCT8600CE", exchange="MCX",
        side="SELL", qty=200, product="NRML", reason="test"))
    assert out is None


def test_cutoff_is_close_minus_default_lead_nfo(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(gate, "get_int", lambda key, default=0: default)
    now = datetime(2026, 10, 15, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert gate.cutoff_for("NFO", now).strftime("%H:%M") == "15:15"


def test_cutoff_uses_mcx_lead_default(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(gate, "get_int", lambda key, default=0: default)
    now = datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert gate.cutoff_for("MCX", now).strftime("%H:%M") == "23:00"


def test_before_cutoff_true_until_cutoff(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(gate, "get_int", lambda key, default=0: default)
    early = datetime(2026, 10, 15, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    late = datetime(2026, 10, 15, 15, 20, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert gate.before_cutoff("NFO", early) is True
    assert gate.before_cutoff("NFO", late) is False


def test_template_exit_held_by_default(monkeypatch):
    monkeypatch.setattr(gate, "get_bool", lambda key, default=False: default)
    assert gate.template_exit_held() is True


# ── held_for: generic hold-check, exact parity with the two specific wrappers ──

def test_held_for_expiry_close_matches_specific_wrapper(monkeypatch):
    monkeypatch.setattr(gate, "get_bool", lambda key, default=False: default)
    assert gate.held_for(HoldCategory.EXPIRY_CLOSE) is True
    monkeypatch.setattr(gate, "get_bool",
                        lambda key, default=False: True if key == gate.EXPIRY_CLOSE_RELEASED_KEY else default)
    assert gate.held_for(HoldCategory.EXPIRY_CLOSE) is False


def test_held_for_template_exit_matches_specific_wrapper(monkeypatch):
    monkeypatch.setattr(gate, "get_bool", lambda key, default=False: False)
    assert gate.held_for(HoldCategory.TEMPLATE_EXIT) is True
    assert gate.held_for(HoldCategory.TEMPLATE_EXIT, False) is False
    monkeypatch.setattr(gate, "get_bool", lambda key, default=False: True)
    assert gate.held_for(HoldCategory.TEMPLATE_EXIT) is False
    assert gate.held_for(HoldCategory.TEMPLATE_EXIT, True) is True
    assert gate.held_for(HoldCategory.TEMPLATE_EXIT, None) is False


def test_held_for_queries_the_categorys_own_released_key(monkeypatch):
    """held_for must build 'hold.<category>_released', not a stringified
    enum repr — regression guard against f-string-ing the Enum member
    itself instead of `.value`."""
    seen_keys = []

    def _get_bool(key, default=False):
        seen_keys.append(key)
        return default

    monkeypatch.setattr(gate, "get_bool", _get_bool)
    gate.held_for(HoldCategory.EXPIRY_CLOSE)
    assert seen_keys[-1] == gate.EXPIRY_CLOSE_RELEASED_KEY
    gate.held_for(HoldCategory.TEMPLATE_EXIT)
    assert seen_keys[-1] == gate.TEMPLATE_EXIT_RELEASED_KEY


# ── record_held_order: generic hold-creation, parameterised by category ──────

def _mock_session_for_insert():
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.add = MagicMock()

    # `record_held_order`'s dedup guard (2026-10-29) does
    # `(await s.execute(select(...).with_for_update())).scalars().first()`
    # BEFORE the insert. SQLAlchemy's real `AsyncSession.execute()` is
    # awaited but returns a plain sync `Result` — `.scalars()`/`.first()`
    # are sync calls on it. A bare `AsyncMock()`'s child attributes are
    # themselves `AsyncMock` recursively, so `result.scalars()` would
    # return a coroutine (not a value) and `.first()` would fail on it.
    # Build the execute-result chain as plain `MagicMock` so it mirrors
    # the real sync `Result` shape; "no existing HELD row" (`None`) is
    # the correct simulation for every test here — none of them are
    # exercising the dedup-hit path.
    _scalars = MagicMock()
    _scalars.first.return_value = None
    _result = MagicMock()
    _result.scalars.return_value = _scalars
    mock_session.execute = AsyncMock(return_value=_result)

    async def _commit():
        # Simulate the DB assigning a primary key on commit/flush, the
        # way the real AsyncSession would for an autoincrement PK.
        for call in mock_session.add.call_args_list:
            call.args[0].id = 123

    mock_session.commit = _commit
    return mock_session


def test_record_held_order_creates_row_with_requested_category_expiry_close(monkeypatch):
    mock_session = _mock_session_for_insert()
    monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)
    with patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock):
        row_id = asyncio.run(gate.record_held_order(
            HoldCategory.EXPIRY_CLOSE, account="ZG0790", symbol="CRUDEOIL26OCT8600CE",
            exchange="MCX", side="SELL", qty=200, product="NRML", reason="expiry close"))

    assert row_id == 123
    created = mock_session.add.call_args.args[0]
    rec = parse_hold_record(created.hold_json)
    assert rec["category"] == "expiry_close"
    assert rec["price_policy"] == "CHASE_MED"
    assert created.status == "HELD"


def test_record_held_order_creates_row_with_requested_category_agent_order(monkeypatch):
    mock_session = _mock_session_for_insert()
    monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)
    with patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock):
        row_id = asyncio.run(gate.record_held_order(
            HoldCategory.AGENT_ORDER, account="ZJ6294", symbol="NIFTY24APR25000CE",
            exchange="NFO", side="BUY", qty=50, product="NRML", reason="repeated rejection",
            price_policy="n/a"))

    assert row_id == 123
    created = mock_session.add.call_args.args[0]
    rec = parse_hold_record(created.hold_json)
    assert rec["category"] == "agent_order"
    assert rec["price_policy"] == "n/a"


def test_record_held_order_dedup_returns_existing_row_id_without_inserting(monkeypatch):
    """2026-10-29 audit fix — up to 3 independent sources (legacy bg-expiry
    task, its own 30-min re-scan, and the new expiry-day-itm-auto-close
    agent) can legitimately try to hold the SAME leg. The second caller
    must get back the FIRST caller's existing HELD row id, not insert a
    duplicate — see the function's own docstring for why a duplicate is
    a real double-close hazard, not just noise."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.add = MagicMock()

    _existing_row = MagicMock(id=456, detail="HELD: already staged")
    _scalars = MagicMock()
    _scalars.first.return_value = _existing_row
    _result = MagicMock()
    _result.scalars.return_value = _scalars
    mock_session.execute = AsyncMock(return_value=_result)
    mock_session.commit = AsyncMock()

    monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)
    with patch("backend.api.algo.order_events.write_event", new_callable=AsyncMock) as mock_write:
        row_id = asyncio.run(gate.record_held_order(
            HoldCategory.EXPIRY_CLOSE, account="ZG0790", symbol="CRUDEOIL26OCT8600CE",
            exchange="MCX", side="SELL", qty=200, product="NRML", reason="expiry close"))

    assert row_id == 456
    mock_session.add.assert_not_called()
    mock_write.assert_not_called()


