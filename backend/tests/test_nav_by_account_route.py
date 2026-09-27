"""Tests for `GET /api/nav/by-account` (backend/api/routes/nav.py).

SSOT: this route serves `compute_firm_nav()["by_account"]` directly — the
per-account breakdown PerformancePage's NAV grid now consumes instead of
the removed client-side `navRowForAccount`/`navByAccount` formula.

Follows the SAME public/masked pattern funds.py/positions.py/holdings.py
already use: unguarded route, account codes masked for non-admin callers,
trader role horizontally scoped to assigned_accounts.

Invoked via `.fn(self=None, request=...)` — the raw handler underneath
Litestar's `@get` decorator — matching the established pattern in
`backend/tests/test_ticker_failover_health.py` (calling the bound/decorated
handler directly raises "Controller.__init__() missing 'owner'" since
Litestar controllers require ASGI-app registration to instantiate).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes.nav import NavController


def _fake_request(token_payload=None, auth_header=None):
    req = MagicMock()
    req.state = MagicMock()
    req.state.token_payload = token_payload or {}
    req.headers = {"Authorization": auth_header} if auth_header else {}
    return req


_SNAP = {
    "nav": 26750.0,
    "errors": [],
    "by_account": {
        "ZG0790": {"cash": 10500.0, "pos_m2m": 750.0, "holdings_mtm": 15000.0, "nav": 26250.0},
        "DH6847": {"cash": 500.0, "pos_m2m": 0.0, "holdings_mtm": 0.0, "nav": 500.0},
    },
}


@pytest.mark.asyncio
async def test_anonymous_request_gets_masked_account_codes():
    req = _fake_request(token_payload={"role": "demo"})
    with patch(
        "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
    ):
        resp = await NavController.nav_by_account.fn(self=None, request=req)

    codes = {r.account for r in resp.rows}
    assert "ZG0790" not in codes and "DH6847" not in codes, (
        "anonymous/demo caller must never see raw account codes"
    )
    assert resp.total is not None
    assert resp.total.nav == pytest.approx(26750.0, abs=0.01)
    assert resp.total.cash == pytest.approx(11000.0, abs=0.01)


@pytest.mark.asyncio
async def test_admin_request_gets_raw_account_codes():
    req = _fake_request(
        token_payload={"role": "admin"}, auth_header="Bearer faketoken",
    )
    with patch(
        "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
    ), patch(
        "backend.api.routes.auth.verify_token", return_value={"role": "admin"},
    ):
        resp = await NavController.nav_by_account.fn(self=None, request=req)

    codes = {r.account for r in resp.rows}
    assert codes == {"ZG0790", "DH6847"}


@pytest.mark.asyncio
async def test_by_account_nav_sums_to_total_row():
    req = _fake_request(token_payload={"role": "demo"})
    with patch(
        "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
    ):
        resp = await NavController.nav_by_account.fn(self=None, request=req)

    summed = sum(r.nav for r in resp.rows)
    assert summed == pytest.approx(resp.total.nav, abs=0.02)


@pytest.mark.asyncio
async def test_trader_role_scoped_to_assigned_accounts():
    req = _fake_request(token_payload={"role": "trader"})
    with patch(
        "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
    ), patch(
        "backend.api.rbac.user_scope_for_connection",
        new=AsyncMock(return_value=(["ZG0790"], [])),
    ):
        resp = await NavController.nav_by_account.fn(self=None, request=req)

    # ZG0790 masked but present; DH6847 excluded entirely (out of scope).
    assert len(resp.rows) == 1
    assert resp.total.nav == pytest.approx(26250.0, abs=0.01)


@pytest.mark.asyncio
async def test_empty_by_account_returns_no_total():
    req = _fake_request(token_payload={"role": "demo"})
    empty_snap = {"nav": 0.0, "errors": [], "by_account": {}}
    with patch(
        "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=empty_snap),
    ):
        resp = await NavController.nav_by_account.fn(self=None, request=req)

    assert resp.rows == []
    assert resp.total is None


@pytest.mark.asyncio
async def test_errors_propagate_from_compute_firm_nav():
    req = _fake_request(token_payload={"role": "demo"})
    err_snap = dict(_SNAP)
    err_snap["errors"] = ["holdings: broker timeout"]
    with patch(
        "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=err_snap),
    ):
        resp = await NavController.nav_by_account.fn(self=None, request=req)

    assert resp.errors == ["holdings: broker timeout"]
