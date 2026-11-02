"""
Tests for the login-triggered "reset execution mode to LIVE" side effect.

Scope: `AuthController.login` (backend/api/routes/auth.py) calls the shared
`reset_to_live_mode()` helper (backend/api/routes/execution.py) after a
successful login, so a prior session's forgotten paper/shadow toggle never
silently carries over — but ONLY for admin/designated logins on the prod
branch, and never in a way that can block login itself.

These tests run the REAL `reset_to_live_mode()` body (not a mock of the
function itself), so they prove the actual settings state changes, not just
that some function got called. `settings.set_bool` is mocked with a side
effect that writes into a patched `settings._CACHE`, which `get_bool` reads
from — avoids needing a real DB connection while still exercising the real
read/write contract.

Covers:
  1. Admin login on prod flips both settings flags from True to False.
  2. Designated login on prod does the same.
  3. Trader/risk/partner logins on prod leave both flags untouched (True).
  4. Any role's login on a non-prod branch leaves both flags untouched.
  5. A `set_bool` failure inside `reset_to_live_mode` never blocks login.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes.auth import AuthController, LoginRequest


def _fake_user(role: str = "admin") -> SimpleNamespace:
    """A minimal stand-in for the `User` ORM row — only the attributes
    `login()` actually reads."""
    return SimpleNamespace(
        id=1,
        username="opuser",
        password_hash="irrelevant-mocked-out",
        role=role,
        display_name="Op User",
        terminated_at=None,
        suspended_at=None,
        is_active=True,
        email_verified=True,
        is_approved=True,
        must_change_password=False,
        token_version=1,
    )


def _mock_login_session(user):
    """Mimic `async with async_session() as session: await session.execute(...)`
    resolving to a single `User` row — same mocking shape
    test_order_placement_and_nav_rbac_2026_10_31.py already uses for
    `async_session`."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=user)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _mock_settings_cache(initial: dict[str, str]) -> tuple[dict[str, str], AsyncMock]:
    """A fake `_CACHE` dict plus a `set_bool` AsyncMock whose side effect
    writes into it — real `get_bool`/`_lookup_raw` read straight out of
    `_CACHE`, so this lets the real `reset_to_live_mode()` body run end to
    end without a DB."""
    cache = dict(initial)

    async def _fake_set_bool(key, value, *, category=None, description=None):
        old = cache.get(key)
        cache[key] = "true" if value else "false"
        return None if old is None else old.strip().lower() in ("1", "true", "yes", "on")

    return cache, AsyncMock(side_effect=_fake_set_bool)


async def _do_login(role: str, is_prod: bool, cache: dict[str, str],
                     set_bool_mock: AsyncMock):
    """Runs the real `login()` coroutine — including the real
    `reset_to_live_mode()` body when it fires — with the DB, password
    check, and token signing mocked out."""
    user = _fake_user(role)
    session_factory = _mock_login_session(user)

    with patch("backend.api.routes.auth.async_session", session_factory), \
         patch("backend.api.routes.auth._check_password", return_value=True), \
         patch("backend.api.routes.auth._make_token", return_value="tok-123"), \
         patch("backend.api.routes.auth.is_prod_branch", return_value=is_prod), \
         patch("backend.shared.helpers.settings._CACHE", cache), \
         patch("backend.shared.helpers.settings.set_bool", set_bool_mock), \
         patch("backend.shared.helpers.settings.reload_cache", AsyncMock()), \
         patch("backend.api.routes.execution._stop_drivers", MagicMock()):

        login_fn = AuthController.login.fn
        resp = await login_fn(AuthController.__new__(AuthController),
                               LoginRequest(username="opuser", password="pw"))

    return resp


class TestLoginResetsToLiveMode:

    @pytest.mark.asyncio
    async def test_admin_login_on_prod_flips_both_flags_to_false(self):
        from backend.shared.helpers.settings import get_bool

        cache, set_bool_mock = _mock_settings_cache({
            "execution.paper_trading_mode": "true",
            "execution.shadow_mode": "true",
        })

        with patch("backend.shared.helpers.settings._CACHE", cache):
            resp = await _do_login(role="admin", is_prod=True,
                                    cache=cache, set_bool_mock=set_bool_mock)

            assert resp.access_token == "tok-123"
            assert get_bool("execution.paper_trading_mode") is False
            assert get_bool("execution.shadow_mode") is False

        set_bool_mock.assert_any_await(
            "execution.paper_trading_mode", False, category="execution",
            description="Reset to live: login by opuser",
        )
        set_bool_mock.assert_any_await(
            "execution.shadow_mode", False, category="execution",
            description="Reset to live: login by opuser",
        )

    @pytest.mark.asyncio
    async def test_designated_login_on_prod_flips_both_flags_to_false(self):
        from backend.shared.helpers.settings import get_bool

        cache, set_bool_mock = _mock_settings_cache({
            "execution.paper_trading_mode": "true",
            "execution.shadow_mode": "true",
        })

        with patch("backend.shared.helpers.settings._CACHE", cache):
            resp = await _do_login(role="designated", is_prod=True,
                                    cache=cache, set_bool_mock=set_bool_mock)

            assert resp.access_token == "tok-123"
            assert get_bool("execution.paper_trading_mode") is False
            assert get_bool("execution.shadow_mode") is False

    @pytest.mark.parametrize("role", ["trader", "risk", "partner"])
    @pytest.mark.asyncio
    async def test_non_privileged_role_login_on_prod_leaves_flags_untouched(self, role):
        from backend.shared.helpers.settings import get_bool

        cache, set_bool_mock = _mock_settings_cache({
            "execution.paper_trading_mode": "true",
            "execution.shadow_mode": "true",
        })

        with patch("backend.shared.helpers.settings._CACHE", cache):
            resp = await _do_login(role=role, is_prod=True,
                                    cache=cache, set_bool_mock=set_bool_mock)

            assert resp.access_token == "tok-123"
            assert get_bool("execution.paper_trading_mode") is True
            assert get_bool("execution.shadow_mode") is True

        set_bool_mock.assert_not_awaited()

    @pytest.mark.parametrize("role", ["admin", "designated", "trader"])
    @pytest.mark.asyncio
    async def test_login_on_dev_branch_leaves_flags_untouched(self, role):
        from backend.shared.helpers.settings import get_bool

        cache, set_bool_mock = _mock_settings_cache({
            "execution.paper_trading_mode": "true",
            "execution.shadow_mode": "true",
        })

        with patch("backend.shared.helpers.settings._CACHE", cache):
            resp = await _do_login(role=role, is_prod=False,
                                    cache=cache, set_bool_mock=set_bool_mock)

            assert resp.access_token == "tok-123"
            assert get_bool("execution.paper_trading_mode") is True
            assert get_bool("execution.shadow_mode") is True

        set_bool_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_bool_failure_does_not_block_login(self):
        """A DB failure (or any exception) inside reset_to_live_mode's
        own set_bool call must never propagate out of login — login's
        success response is unaffected. Runs the real try/except in
        login(), not a mocked wrapper around reset_to_live_mode."""
        cache = {
            "execution.paper_trading_mode": "true",
            "execution.shadow_mode": "true",
        }
        failing_set_bool = AsyncMock(side_effect=RuntimeError("DB unreachable"))

        resp = await _do_login(role="admin", is_prod=True,
                                cache=cache, set_bool_mock=failing_set_bool)

        assert resp.access_token == "tok-123"
        assert resp.username == "opuser"
        assert resp.role == "admin"
        failing_set_bool.assert_awaited_once()
        # The flag was never actually flipped — the exception fired
        # before the side effect could write a value.
        assert cache["execution.paper_trading_mode"] == "true"
