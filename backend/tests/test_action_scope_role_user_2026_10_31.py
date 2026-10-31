"""2026-10-31 — operator: "I should be able to select an account in
actions, so, you may need to add this parameters to these functions
registers as default parameters. if the parameters are not mentioned, it
should be for all" / "it should be for accounts, roles, users, etc."

`cancel_all_orders` and `chase_close_positions` (the two genuine
fan-out, multi-account actions — `place_order`/`modify_order`/
`cancel_order`/`close_position` are single-target and out of scope,
confirmed in this session's own audit) gain `scope='role'`/`scope='user'`
alongside the pre-existing `total`/`account`. Deliberately restricted to
`role='trader'`: that is the only role with a real, well-defined
per-user `assigned_accounts` list — every other role is firm-wide
(`_FIRM_WIDE_ROLES` in `rbac.py`) and sees every account regardless of
list contents, so "accounts for role=admin" has no well-defined answer.

Omitting role/user entirely must be a complete no-op — scope='total'
(the default) behaves exactly as before this change, for every
already-saved agent.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.actions_live import (
    _al_accounts_for_trader_role,
    _al_accounts_for_trader_user,
    _al_positions_filter,
    _al_resolve_scope_accounts,
    cancel_all_orders_scoped,
)

def _session_cm(execute_result):
    """A fake `async_session()` context manager returning a canned
    `.execute()` result on `__aenter__`."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=execute_result)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
class TestAccountsForTraderRole:
    async def test_unions_and_dedupes_multiple_traders(self):
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [
            ["ZG0790", "ZJ6294"], ["ZJ6294", "ZK1111"], [],
        ]
        with patch("backend.api.database.async_session",
                    return_value=_session_cm(exec_result)):
            result = await _al_accounts_for_trader_role()
        assert result == ["ZG0790", "ZJ6294", "ZK1111"]

    async def test_no_traders_returns_empty_list(self):
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = []
        with patch("backend.api.database.async_session",
                    return_value=_session_cm(exec_result)):
            result = await _al_accounts_for_trader_role()
        assert result == []


@pytest.mark.asyncio
class TestAccountsForTraderUser:
    async def test_trader_user_returns_own_accounts(self):
        exec_result = MagicMock()
        exec_result.first.return_value = ("trader", ["ZG0790", "ZG0790", None])
        with patch("backend.api.database.async_session",
                    return_value=_session_cm(exec_result)):
            result = await _al_accounts_for_trader_user("alice")
        assert result == ["ZG0790"]

    async def test_non_trader_user_raises(self):
        exec_result = MagicMock()
        exec_result.first.return_value = ("admin", ["ZG0790"])
        with patch("backend.api.database.async_session",
                    return_value=_session_cm(exec_result)):
            with pytest.raises(ValueError, match="firm-wide"):
                await _al_accounts_for_trader_user("bob_admin")

    async def test_unknown_user_raises(self):
        exec_result = MagicMock()
        exec_result.first.return_value = None
        with patch("backend.api.database.async_session",
                    return_value=_session_cm(exec_result)):
            with pytest.raises(ValueError, match="no such user"):
                await _al_accounts_for_trader_user("nobody")


@pytest.mark.asyncio
class TestResolveScopeAccounts:
    async def test_scope_total_returns_none(self):
        assert await _al_resolve_scope_accounts("total", None, None, None) is None

    async def test_scope_unset_defaults_to_total_returns_none(self):
        assert await _al_resolve_scope_accounts(None, None, None, None) is None

    async def test_scope_account_returns_single_item_list(self):
        result = await _al_resolve_scope_accounts("account", "ZG0790", None, None)
        assert result == ["ZG0790"]

    async def test_scope_account_without_account_raises(self):
        with pytest.raises(ValueError, match="scope=account"):
            await _al_resolve_scope_accounts("account", None, None, None)

    async def test_scope_role_requires_role_param(self):
        with pytest.raises(ValueError, match="scope=role"):
            await _al_resolve_scope_accounts("role", None, None, None)

    async def test_scope_role_rejects_non_trader_role(self):
        with pytest.raises(ValueError, match="firm-wide"):
            await _al_resolve_scope_accounts("role", None, "admin", None)

    async def test_scope_role_trader_resolves_via_helper(self):
        with patch(
            "backend.api.algo.actions_live._al_accounts_for_trader_role",
            new=AsyncMock(return_value=["ZG0790", "ZJ6294"]),
        ):
            result = await _al_resolve_scope_accounts("role", None, "trader", None)
        assert result == ["ZG0790", "ZJ6294"]

    async def test_scope_user_requires_user_param(self):
        with pytest.raises(ValueError, match="scope=user"):
            await _al_resolve_scope_accounts("user", None, None, None)

    async def test_scope_user_resolves_via_helper(self):
        with patch(
            "backend.api.algo.actions_live._al_accounts_for_trader_user",
            new=AsyncMock(return_value=["ZG0790"]),
        ) as mock_helper:
            result = await _al_resolve_scope_accounts("user", None, None, "alice")
        mock_helper.assert_awaited_once_with("alice")
        assert result == ["ZG0790"]


class TestPositionsFilterAcceptsListOrString:
    def _rows(self):
        return [
            {"account": "ZG0790", "quantity": 10},
            {"account": "ZJ6294", "quantity": 5},
            {"account": "ZK1111", "quantity": 0},  # dropped — zero qty
        ]

    def test_none_scope_keeps_all_nonzero_rows(self):
        result = _al_positions_filter(self._rows(), None)
        assert {r["account"] for r in result} == {"ZG0790", "ZJ6294"}

    def test_single_string_scope_narrows_to_one_account(self):
        result = _al_positions_filter(self._rows(), "ZG0790")
        assert [r["account"] for r in result] == ["ZG0790"]

    def test_list_scope_narrows_to_multiple_accounts(self):
        result = _al_positions_filter(self._rows(), ["ZG0790", "ZJ6294"])
        assert {r["account"] for r in result} == {"ZG0790", "ZJ6294"}

    def test_empty_list_scope_narrows_to_nothing(self):
        result = _al_positions_filter(self._rows(), [])
        assert result == []


@pytest.mark.asyncio
class TestCancelAllOrdersScopedRoleUser:
    """`cancel_all_orders_scoped`'s broker loop — role/user resolution
    narrows WHICH brokers are even considered, orthogonal to (and
    composable with) the pre-existing `account` narrowing."""

    def _broker(self, account):
        b = MagicMock()
        b.account = account
        return b

    async def test_scope_role_only_touches_resolved_accounts(self):
        brokers = [self._broker("ZG0790"), self._broker("ZJ6294"), self._broker("ZK1111")]
        with patch("backend.brokers.registry.all_brokers", return_value=brokers), \
             patch(
                 "backend.api.algo.actions_live._al_accounts_for_trader_role",
                 new=AsyncMock(return_value=["ZG0790", "ZJ6294"]),
             ), \
             patch(
                 "backend.api.algo.actions_live._al_cancel_broker_orders",
                 new=AsyncMock(return_value=(1, 0)),
             ) as mock_cancel:
            cancelled, errors = await cancel_all_orders_scoped(
                scope="role", role="trader",
            )
        touched = {c.args[0].account for c in mock_cancel.await_args_list}
        assert touched == {"ZG0790", "ZJ6294"}
        assert cancelled == 2 and errors == 0

    async def test_scope_role_non_trader_raises_before_touching_any_broker(self):
        brokers = [self._broker("ZG0790")]
        with patch("backend.brokers.registry.all_brokers", return_value=brokers), \
             patch(
                 "backend.api.algo.actions_live._al_cancel_broker_orders",
                 new=AsyncMock(),
             ) as mock_cancel:
            with pytest.raises(ValueError, match="firm-wide"):
                await cancel_all_orders_scoped(scope="role", role="partner")
        mock_cancel.assert_not_awaited()

    async def test_scope_user_empty_resolution_cancels_nothing(self):
        """A trader-role user with no assigned accounts (or a lookup
        that matches nobody) narrows to ZERO accounts — same fail-safe
        convention as trader's own empty `assigned_accounts` elsewhere,
        not an error."""
        brokers = [self._broker("ZG0790"), self._broker("ZJ6294")]
        with patch("backend.brokers.registry.all_brokers", return_value=brokers), \
             patch(
                 "backend.api.algo.actions_live._al_accounts_for_trader_user",
                 new=AsyncMock(return_value=[]),
             ), \
             patch(
                 "backend.api.algo.actions_live._al_cancel_broker_orders",
                 new=AsyncMock(),
             ) as mock_cancel:
            cancelled, errors = await cancel_all_orders_scoped(
                scope="user", user="alice",
            )
        mock_cancel.assert_not_awaited()
        assert cancelled == 0 and errors == 0

    async def test_scope_total_default_untouched_by_role_user_change(self):
        """Backward-compat guard: omitting role/user entirely (every
        already-saved agent) must behave exactly as before — every
        broker touched, no role/user resolution attempted at all."""
        brokers = [self._broker("ZG0790"), self._broker("ZJ6294")]
        with patch("backend.brokers.registry.all_brokers", return_value=brokers), \
             patch(
                 "backend.api.algo.actions_live._al_accounts_for_trader_role",
                 new=AsyncMock(),
             ) as mock_role_helper, \
             patch(
                 "backend.api.algo.actions_live._al_cancel_broker_orders",
                 new=AsyncMock(return_value=(1, 0)),
             ) as mock_cancel:
            cancelled, errors = await cancel_all_orders_scoped()
        mock_role_helper.assert_not_awaited()
        assert mock_cancel.await_count == 2
        assert cancelled == 2 and errors == 0
