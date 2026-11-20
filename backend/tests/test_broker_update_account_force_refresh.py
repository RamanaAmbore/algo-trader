"""2026-11-20 fix (F12 — operator explicit new requirement, live ZG0790
CAPTCHA incident). Re-enabling a disabled broker account's connection must
force an immediate, unconditional (non-market-hours-gated) refresh of that
one account's positions/holdings/margins/funds — not leave it stale until
the next routine poll cycle.

This file proves:
  - the False→True transition specifically triggers the forced refresh
    (as a background task, fire-and-forget);
  - a True→False (disable), a True→True (already active, re-saved), and
    any other field-only change do NOT trigger it;
  - `_force_refresh_one_account_after_reenable()` busts both the
    route-level and broker-layer raw caches and re-fetches positions/
    holdings/margins scoped to the one account.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.models import BrokerAccount
from backend.api.routes.brokers import BrokerAccountUpdate, BrokersController


def _make_row(account: str = "ZG0790", is_active: bool = False) -> BrokerAccount:
    row = BrokerAccount()
    row.id = 1
    row.account = account
    row.broker_id = "zerodha_kite"
    row.api_key = "k"
    row.client_id = None
    row.source_ip = None
    row.is_active = is_active
    row.notes = None
    row.priority = 100
    row.extra_config = {}
    row.historical_data_enabled = True
    row.created_at = None
    row.updated_at = None
    row.poll_priority = "hot"
    row.auto_downgrade_enabled = False
    row.auto_downgraded_at = None
    row.auto_downgrade_reason = None
    row.circuit_breaker_enabled = False
    row.display_order = 500
    row.access_token_enc = None
    return row


def _mock_session_for_row(row: BrokerAccount):
    session_mock = AsyncMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    session_mock.execute = AsyncMock(return_value=result_mock)
    session_mock.commit = AsyncMock()
    session_mock.refresh = AsyncMock()
    factory = MagicMock(return_value=session_mock)
    return factory


async def _call_update_account(account: str, row: BrokerAccount, data: BrokerAccountUpdate):
    """Call the real `update_account` handler against a mocked DB session,
    with `_reload_connections` and `_loaded_accounts` neutralised, and
    capture whether/how `_force_refresh_one_account_after_reenable` was
    scheduled. Returns (response, refresh_calls, pending_tasks)."""
    import backend.api.routes.brokers as brokers_mod

    factory = _mock_session_for_row(row)
    refresh_calls: list[str] = []

    async def fake_force_refresh(acct: str) -> None:
        refresh_calls.append(acct)

    controller = BrokersController.__new__(BrokersController)
    update_account_fn = BrokersController.update_account.fn
    with patch.object(brokers_mod, "shared_async_session", factory), patch.object(
        brokers_mod, "_reload_connections", new=AsyncMock(),
    ), patch.object(
        brokers_mod, "_loaded_accounts", return_value=set(),
    ), patch.object(
        brokers_mod, "_force_refresh_one_account_after_reenable", new=fake_force_refresh,
    ):
        before_tasks = asyncio.all_tasks()
        resp = await update_account_fn(controller, account, data)
        new_tasks = asyncio.all_tasks() - before_tasks
        for t in new_tasks:
            await t
    return resp, refresh_calls


class TestForceRefreshOnlyOnFalseToTrueTransition:
    @pytest.mark.asyncio
    async def test_false_to_true_triggers_forced_refresh(self):
        row = _make_row(is_active=False)
        data = BrokerAccountUpdate(is_active=True)
        resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == ["ZG0790"], (
            f"expected the forced refresh to fire exactly once for ZG0790, got {refresh_calls!r}"
        )
        assert resp.is_active is True

    @pytest.mark.asyncio
    async def test_true_to_false_does_not_trigger_forced_refresh(self):
        """Disabling an account must never trigger this — only
        re-enabling (False→True) does."""
        row = _make_row(is_active=True)
        data = BrokerAccountUpdate(is_active=False)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_already_active_resaved_true_does_not_trigger_forced_refresh(self):
        """A no-op is_active=True PATCH on an ALREADY-active account
        (e.g. the operator re-saving the same form) must not re-trigger
        the forced refresh — only a genuine False→True transition."""
        row = _make_row(is_active=True)
        data = BrokerAccountUpdate(is_active=True)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_unrelated_field_change_does_not_trigger_forced_refresh(self):
        """A PATCH that changes some OTHER field (priority) on an
        already-disabled account, with `is_active` entirely omitted
        (None, not explicitly set), must not trigger the forced
        refresh — only an explicit is_active=True transition does."""
        row = _make_row(is_active=False)
        data = BrokerAccountUpdate(priority=50)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_already_inactive_explicit_false_does_not_trigger(self):
        """Explicitly re-confirming is_active=False on an already-
        inactive account — no transition at all — must not trigger."""
        row = _make_row(is_active=False)
        data = BrokerAccountUpdate(is_active=False)
        _resp, refresh_calls = await _call_update_account("ZG0790", row, data)
        assert refresh_calls == []


class TestForceRefreshHelperBustsCachesAndRefetchesScopedToOneAccount:
    @pytest.mark.asyncio
    async def test_invalidates_route_and_raw_caches_and_refetches_one_account(self):
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        invalidated_route: list[str] = []
        invalidated_raw: list[str] = []
        fetch_calls: dict[str, list] = {"positions": [], "holdings": [], "margins": []}

        def fake_invalidate(key):
            invalidated_route.append(key)

        def fake_raw_invalidate(key):
            invalidated_raw.append(key)

        def fake_fetch_positions(*, force_refresh=False):
            fetch_calls["positions"].append(force_refresh)
            return []

        def fake_fetch_holdings():
            fetch_calls["holdings"].append(True)
            return []

        def fake_fetch_margins(*, force_refresh=False):
            fetch_calls["margins"].append(force_refresh)
            return []

        with patch("backend.api.cache.invalidate", fake_invalidate), patch(
            "backend.brokers.broker_apis._raw_cache_invalidate", fake_raw_invalidate,
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", fake_fetch_positions,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", fake_fetch_holdings,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins", fake_fetch_margins,
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")

        assert set(invalidated_route) == {"positions", "holdings", "funds"}
        assert set(invalidated_raw) == {"positions", "holdings", "margins"}
        # Zero-arg forms — the SSOT-cache-populating path, NOT the
        # single-account branch (which bypasses that cache entirely and
        # would leave its result discarded). See the advisor-driven
        # correction in `_force_refresh_one_account_after_reenable`'s
        # own docstring.
        assert fetch_calls["positions"] == [True]
        assert fetch_calls["holdings"] == [True]
        assert fetch_calls["margins"] == [True]

    @pytest.mark.asyncio
    async def test_never_calls_with_account_kwarg_the_cache_bypassing_branch(self):
        """Regression guard for the exact bug the advisor review caught:
        the single-account `account=` branch of each `fetch_*` function
        bypasses the all-accounts SSOT cache entirely, so a fetch made
        that way is thrown away and never reaches the next route read.
        Asserts positional/keyword call shape directly rather than only
        checking call counts."""
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        calls = {"positions": None, "holdings": None, "margins": None}

        def fake_fetch_positions(*args, **kwargs):
            calls["positions"] = (args, kwargs)
            return []

        def fake_fetch_holdings(*args, **kwargs):
            calls["holdings"] = (args, kwargs)
            return []

        def fake_fetch_margins(*args, **kwargs):
            calls["margins"] = (args, kwargs)
            return []

        with patch("backend.api.cache.invalidate"), patch(
            "backend.brokers.broker_apis._raw_cache_invalidate",
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", fake_fetch_positions,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", fake_fetch_holdings,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins", fake_fetch_margins,
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")

        for name, (args, kwargs) in calls.items():
            assert "account" not in kwargs, (
                f"fetch_{name} was called with account= — this is the cache-"
                f"bypassing single-account branch, exactly the bug this test guards against"
            )
            assert args == (), f"fetch_{name} must be called with zero positional args too"

    @pytest.mark.asyncio
    async def test_a_broker_failure_during_refresh_never_raises(self):
        """Best-effort — a transient broker error during the forced
        refresh must never propagate (this runs as a detached background
        task; an unhandled exception there would be silently swallowed
        by asyncio anyway, but the function itself must degrade
        gracefully, matching every other best-effort helper in this
        file)."""
        from backend.api.routes.brokers import _force_refresh_one_account_after_reenable

        with patch(
            "backend.brokers.broker_apis.fetch_positions",
            side_effect=RuntimeError("broker down"),
        ):
            await _force_refresh_one_account_after_reenable("ZG0790")  # must not raise
