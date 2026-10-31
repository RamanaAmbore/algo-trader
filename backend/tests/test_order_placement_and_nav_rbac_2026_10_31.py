"""Tests for the 2026-10-31 security fixes:

Fix 1 — `/api/orders/ticket` and `/api/orders/basket` (`OrdersController` in
backend/api/routes/orders.py) had NO capability guard beyond the
controller-level `auth_or_demo_guard` (any authenticated-or-demo caller).
The capability matrix (`backend/api/rbac.py`) scopes `place_order` to
`{designated, trader}` only — `admin` and `partner` must be rejected.
Fixed by adding `guards=[cap_guard("place_order")]` to both route
decorators. `basket_order_handler`'s own internal `is_admin_request(...)`
role check (which incorrectly ALLOWED `admin` and incorrectly BLOCKED
`trader`, diverging from the matrix) is removed in the same change —
left in place, it would silently re-block a `trader` request that had
just passed the new route guard.

Fix 2 — `/api/nav/by-account` and `/api/nav/` (`list_nav`) exposed the
full firm-wide NAV breakdown to `partner` (LP) callers. Partner's
correctly-scoped view is `/api/nav/me` / `/me/history`. Fixed by
returning an empty breakdown for a verified `partner` caller on both
routes, without touching `designated`/`admin`/`risk` visibility.

`nav_by_account` carries NO guard at all, so `resolve_role_from_connection`
resolves EVERY caller (including designated/admin) to `"partner"` via its
`normalise_role(None)` fallback on this specific unguarded route — using
that resolver for the new partner check would have blanked the grid for
everyone, not just partners. The fix instead decodes the Authorization
header directly (`_nav_caller_role_from_header`, mirroring
`auth_guard.is_admin_request`'s own pattern) so a raw/empty/anonymous
header reads as "unknown", never as "partner" by default. `list_nav` is
guarded by `cap_guard("view_nav")` (demo-eligible), so
`resolve_role_from_connection` IS reliable there — the two routes
deliberately use different mechanisms; see each route's own docstring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException, PermissionDeniedException

from backend.api.rbac import cap_guard
from backend.api.routes.orders import OrdersController
from backend.api.routes.orders_basket import basket_order_handler
from backend.api.routes.nav import NavController
from backend.api.schemas import BasketOrderRequest


# ──────────────────────────────────────────────────────────────────────────
# Fix 1a — static wiring: the route decorators actually carry the guard.
# ──────────────────────────────────────────────────────────────────────────

class TestOrderPlacementRouteGuardWiring:
    def test_ticket_route_carries_place_order_cap_guard(self):
        names = [getattr(g, "__name__", g) for g in OrdersController.ticket_order.guards]
        assert "cap_guard__place_order" in names

    def test_basket_route_carries_place_order_cap_guard(self):
        names = [getattr(g, "__name__", g) for g in OrdersController.basket_order.guards]
        assert "cap_guard__place_order" in names


# ──────────────────────────────────────────────────────────────────────────
# Fix 1b — the real guard logic: admin/partner rejected, designated/trader
# allowed. Exercises the literal `cap_guard("place_order")` closure (the
# same object installed on both routes), not a re-implementation —
# same pattern as test_agents_interpret_auth_fix.py::TestRequireCapability.
# ──────────────────────────────────────────────────────────────────────────

def _fake_connection(role: str):
    conn = MagicMock()
    conn.state.token_payload = {"role": role, "sub": f"{role}-user", "tv": 1}
    return conn


class TestPlaceOrderCapGuardRoleEnforcement:
    @pytest.mark.asyncio
    async def test_admin_is_rejected(self):
        guard = cap_guard("place_order")
        conn = _fake_connection("admin")
        with patch("backend.api.auth_guard.jwt_guard", new=AsyncMock()):
            with pytest.raises(PermissionDeniedException):
                await guard(conn, None)

    @pytest.mark.asyncio
    async def test_partner_is_rejected(self):
        guard = cap_guard("place_order")
        conn = _fake_connection("partner")
        with patch("backend.api.auth_guard.jwt_guard", new=AsyncMock()):
            with pytest.raises(PermissionDeniedException):
                await guard(conn, None)

    @pytest.mark.asyncio
    async def test_designated_is_not_rejected(self):
        guard = cap_guard("place_order")
        conn = _fake_connection("designated")
        with patch("backend.api.auth_guard.jwt_guard", new=AsyncMock()):
            await guard(conn, None)  # must not raise

    @pytest.mark.asyncio
    async def test_trader_is_not_rejected(self):
        guard = cap_guard("place_order")
        conn = _fake_connection("trader")
        with patch("backend.api.auth_guard.jwt_guard", new=AsyncMock()):
            await guard(conn, None)  # must not raise

    @pytest.mark.asyncio
    async def test_risk_is_rejected(self):
        """Regression guard: risk has no trading rights either, same as
        admin/partner — must not regain them by accident."""
        guard = cap_guard("place_order")
        conn = _fake_connection("risk")
        with patch("backend.api.auth_guard.jwt_guard", new=AsyncMock()):
            with pytest.raises(PermissionDeniedException):
                await guard(conn, None)

    @pytest.mark.asyncio
    async def test_anonymous_demo_is_rejected_not_silently_allowed(self):
        """place_order is not demo-eligible (no 'demo' in its CAPS
        frozenset), so `require_capability` routes through the strict
        `jwt_guard`, not `auth_or_demo_guard` — an anonymous caller with
        no Authorization header must be rejected at the guard, never
        reach the handler."""
        from litestar.exceptions import NotAuthorizedException
        guard = cap_guard("place_order")
        conn = MagicMock()
        conn.state.token_payload = None
        conn.headers = {}
        with pytest.raises((PermissionDeniedException, NotAuthorizedException)):
            await guard(conn, None)


# ──────────────────────────────────────────────────────────────────────────
# Fix 1c — regression: basket_order_handler's own internal admin-only
# check is gone, so a non-admin (e.g. trader) request that already
# passed the route guard is not re-blocked inside the handler.
# ──────────────────────────────────────────────────────────────────────────

class TestBasketHandlerNoLongerDoubleGatesOnAdmin:
    @pytest.mark.asyncio
    async def test_non_admin_non_demo_request_is_not_blocked_inside_handler(self):
        """Empty `groups` means the handler never touches the DB or a
        broker — `asyncio.gather()` on an empty list returns `[]`
        immediately. This isolates the ONE thing under test: does the
        handler itself still reject a non-admin caller? Pre-fix, this
        raised HTTPException(403, 'Admin access required for basket
        orders.') for ANY non-admin/non-designated role, including
        trader (who must be allowed per the capability matrix)."""
        request = MagicMock()
        request.state.is_demo = False
        # No `role` attribute consulted by the handler at all post-fix —
        # role gating now lives solely in the route guard.
        data = BasketOrderRequest(groups=[])

        resp = await basket_order_handler(data, request)

        assert resp.groups == []

    @pytest.mark.asyncio
    async def test_demo_is_still_blocked_inside_handler(self):
        """The demo check is unchanged by this fix — still an in-handler
        403, since demo sessions carry no real JWT for the route guard
        to key off (auth_or_demo_guard lets them through as demo)."""
        from litestar.exceptions import HTTPException
        request = MagicMock()
        request.state.is_demo = True
        data = BasketOrderRequest(groups=[])

        with pytest.raises(HTTPException) as exc_info:
            await basket_order_handler(data, request)
        assert exc_info.value.status_code == 403


# ──────────────────────────────────────────────────────────────────────────
# Fix 1d — strategy-scope parity: opening /basket to `trader` (matrix-
# aligned) newly exposed the exact strategy_id bypass /ticket was fixed
# for on 2026-09-27 (_validate_ticket_strategy_scope). Closing it is
# part of this fix, not a change to existing trader logic.
# ──────────────────────────────────────────────────────────────────────────

def _trader_request():
    req = MagicMock()
    req.state = MagicMock()
    req.state.is_demo = False
    req.state.token_payload = {"role": "trader"}
    return req


def _leg(strategy_id=None):
    from backend.api.schemas import BasketLeg
    return BasketLeg(
        tradingsymbol="RELIANCE", exchange="NSE",
        transaction_type="BUY", quantity=1, strategy_id=strategy_id,
    )


class TestBasketStrategyScopeParity:
    @pytest.mark.asyncio
    async def test_trader_with_unassigned_strategy_id_rejected_whole_request(self):
        from backend.api.schemas import BasketGroup
        request = _trader_request()
        data = BasketOrderRequest(groups=[
            BasketGroup(account="ZG0790", legs=[_leg(strategy_id=99)]),
        ])
        with patch(
            "backend.api.routes.orders_basket.user_scope_for_connection",
            new=AsyncMock(return_value=([], [1, 2, 3])),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await basket_order_handler(data, request)
        assert exc_info.value.status_code == 403
        assert "99" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_trader_with_assigned_strategy_id_proceeds(self):
        from backend.api.schemas import BasketGroup
        request = _trader_request()
        data = BasketOrderRequest(groups=[
            BasketGroup(account="ZG0790", legs=[_leg(strategy_id=2)]),
        ])
        with patch(
            "backend.api.routes.orders_basket.user_scope_for_connection",
            new=AsyncMock(return_value=([], [1, 2, 3])),
        ), patch(
            "backend.brokers.registry._loaded_accounts",
            return_value=set(),  # unknown account → per-leg error, not a 403
        ):
            resp = await basket_order_handler(data, request)
        # Proceeded past the scope guard — the per-leg "unknown account"
        # error proves dispatch ran, not a scope rejection.
        assert resp.groups[0].results[0].status == "error"
        assert "unknown account" in resp.groups[0].results[0].error

    @pytest.mark.asyncio
    async def test_trader_with_no_strategy_id_proceeds_unscoped(self):
        """No strategy_id on any leg → the parity check is a no-op,
        same as /ticket's `if not data.strategy_id: return`."""
        from backend.api.schemas import BasketGroup
        request = _trader_request()
        data = BasketOrderRequest(groups=[
            BasketGroup(account="ZG0790", legs=[_leg(strategy_id=None)]),
        ])
        scope_mock = AsyncMock(return_value=([], []))
        with patch(
            "backend.api.routes.orders_basket.user_scope_for_connection",
            new=scope_mock,
        ), patch(
            "backend.brokers.registry._loaded_accounts",
            return_value=set(),
        ):
            resp = await basket_order_handler(data, request)
        scope_mock.assert_not_awaited()
        assert resp.groups[0].results[0].status == "error"

    @pytest.mark.asyncio
    async def test_designated_with_strategy_id_is_not_scoped(self):
        """Only `trader` is horizontally scoped — designated proceeds
        regardless of strategy_id (matches /ticket's exact behaviour)."""
        from backend.api.schemas import BasketGroup
        request = MagicMock()
        request.state = MagicMock()
        request.state.is_demo = False
        request.state.token_payload = {"role": "designated"}
        data = BasketOrderRequest(groups=[
            BasketGroup(account="ZG0790", legs=[_leg(strategy_id=99)]),
        ])
        scope_mock = AsyncMock(return_value=([], []))
        with patch(
            "backend.api.routes.orders_basket.user_scope_for_connection",
            new=scope_mock,
        ), patch(
            "backend.brokers.registry._loaded_accounts",
            return_value=set(),
        ):
            resp = await basket_order_handler(data, request)
        scope_mock.assert_not_awaited()
        assert resp.groups[0].results[0].status == "error"


# ──────────────────────────────────────────────────────────────────────────
# Fix 1e — full HTTP round trip through the real Litestar app, proving
# the route-level guard + controller layering actually works end to end
# (the guard-closure tests above never exercise Litestar's own guard
# resolution/layering). Precedent: test_orders_qty_lot_size.py /
# test_ticket_account.py's `async_client` + stamping-jwt_guard pattern.
# ──────────────────────────────────────────────────────────────────────────

def _stamping_jwt_guard(role: str):
    async def _guard(connection, handler) -> None:
        connection.state.token_payload = {"role": role, "sub": f"{role}-user", "tv": 1}
    return _guard


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "partner", "risk"])
async def test_ticket_http_rejects_non_trading_roles(async_client, stub_connections, role):
    payload = {
        "mode": "live", "side": "BUY", "tradingsymbol": "RELIANCE",
        "exchange": "NSE", "quantity": 1, "price": 100.0,
        "order_type": "LIMIT", "account": "ZG0790",
    }
    with patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        jwt_guard=_stamping_jwt_guard(role),
    ):
        response = await async_client.post("/api/orders/ticket", json=payload)
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["designated", "trader"])
async def test_ticket_http_allows_trading_roles_past_the_guard(async_client, stub_connections, role):
    """No `account` → 400 from `_ticket_validate_account`, which proves
    the request got PAST the new capability guard and reached the
    handler — a 403 here would mean the guard wrongly blocked an
    authorized role."""
    payload = {
        "mode": "live", "side": "BUY", "tradingsymbol": "RELIANCE",
        "exchange": "NSE", "quantity": 1, "price": 100.0,
        "order_type": "LIMIT", "account": "",
    }
    with patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        jwt_guard=_stamping_jwt_guard(role),
    ):
        response = await async_client.post("/api/orders/ticket", json=payload)
    assert response.status_code == 400
    assert "account" in response.json().get("detail", "").lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "partner", "risk"])
async def test_basket_http_rejects_non_trading_roles(async_client, stub_connections, role):
    with patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        jwt_guard=_stamping_jwt_guard(role),
    ):
        response = await async_client.post("/api/orders/basket", json={"groups": []})
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["designated", "trader"])
async def test_basket_http_allows_trading_roles_past_the_guard(async_client, stub_connections, role):
    """Empty groups → 201 with an empty result set — proves the
    request got PAST the new capability guard (and, for trader, past
    the new strategy-scope parity check, which is a no-op with no legs)
    without ever touching the DB or a broker."""
    with patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        jwt_guard=_stamping_jwt_guard(role),
    ):
        response = await async_client.post("/api/orders/basket", json={"groups": []})
    assert response.status_code == 201
    assert response.json() == {"groups": []}


# ──────────────────────────────────────────────────────────────────────────
# Fix 2 — nav_by_account: partner gets no firm-wide breakdown;
# designated/admin/risk are unaffected.
# ──────────────────────────────────────────────────────────────────────────

def _fake_nav_request(token_payload=None, auth_header=None):
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


class TestNavByAccountPartnerScoping:
    @pytest.mark.asyncio
    async def test_partner_with_valid_jwt_gets_empty_breakdown(self):
        """No `token_payload` injected — only the header + `verify_token`
        patch sets the role, matching this specific unguarded route's
        real production behaviour (see `_nav_caller_role_from_header`'s
        docstring in nav.py)."""
        req = _fake_nav_request(auth_header="Bearer faketoken")
        with patch(
            "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
        ) as _compute, patch(
            "backend.api.routes.auth.verify_token", return_value={"role": "partner"},
        ):
            resp = await NavController.nav_by_account.fn(self=None, request=req)

        assert resp.rows == []
        assert resp.total is None
        # Short-circuits BEFORE the firm-wide computation even runs —
        # a partner's request must never trigger the firm-wide fetch.
        _compute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_designated_with_valid_jwt_still_gets_full_breakdown(self):
        """designated must be completely unaffected by this fix —
        operator's explicit, non-negotiable constraint.

        Deliberately does NOT pass `token_payload` — only the header +
        `verify_token` patch sets the role. This route has no guard, so
        `token_payload` is never genuinely populated in production; a
        test that injects it directly would still pass even if the
        partner check were (wrongly) switched to
        `resolve_role_from_connection`, silently reintroducing the
        exact "every caller resolves to partner" trap this fix exists
        to avoid. See `_nav_caller_role_from_header`'s own docstring in
        nav.py."""
        req = _fake_nav_request(auth_header="Bearer faketoken")
        with patch(
            "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
        ), patch(
            "backend.api.routes.auth.verify_token", return_value={"role": "designated"},
        ):
            resp = await NavController.nav_by_account.fn(self=None, request=req)

        codes = {r.account for r in resp.rows}
        assert codes == {"ZG0790", "DH6847"}
        assert resp.total is not None

    @pytest.mark.asyncio
    async def test_admin_still_gets_full_breakdown(self):
        """Pre-existing test (test_nav_by_account_route.py) already
        covers this; re-asserted here alongside the partner case for a
        single before/after comparison in one file. No `token_payload`
        injected — see designated test's docstring above."""
        req = _fake_nav_request(auth_header="Bearer faketoken")
        with patch(
            "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
        ), patch(
            "backend.api.routes.auth.verify_token", return_value={"role": "admin"},
        ):
            resp = await NavController.nav_by_account.fn(self=None, request=req)

        codes = {r.account for r in resp.rows}
        assert codes == {"ZG0790", "DH6847"}

    @pytest.mark.asyncio
    async def test_risk_still_gets_full_breakdown(self):
        """risk is firm-wide (`_FIRM_WIDE_ROLES`) and unaffected by this
        fix — still sees both accounts. Codes are masked because
        `is_admin_request` only unmasks designated/admin (pre-existing,
        unrelated to this fix); the row COUNT is what must be
        unaffected here. No `token_payload` injected — see designated
        test's docstring above."""
        req = _fake_nav_request(auth_header="Bearer faketoken")
        with patch(
            "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
        ), patch(
            "backend.api.routes.auth.verify_token", return_value={"role": "risk"},
        ):
            resp = await NavController.nav_by_account.fn(self=None, request=req)

        assert len(resp.rows) == 2
        assert resp.total is not None
        assert resp.total.nav == pytest.approx(26750.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_anonymous_request_is_unaffected_still_gets_masked_breakdown(self):
        """Critical non-regression: this route is deliberately PUBLIC
        (anonymous /performance investors see the masked firm-aggregate
        panel). A genuinely anonymous caller carries no Authorization
        header at all, so `_nav_caller_role_from_header` returns None
        (not "partner") and the public path is untouched. Using
        `resolve_role_from_connection` for the partner check instead
        would have broken this exact case — see module docstring."""
        req = _fake_nav_request(token_payload={"role": "demo"})
        with patch(
            "backend.api.algo.nav.compute_firm_nav", new=AsyncMock(return_value=_SNAP),
        ):
            resp = await NavController.nav_by_account.fn(self=None, request=req)

        # Row count unaffected (2 accounts), but codes are masked (not
        # raw) for anonymous — same invariant as the pre-existing
        # test_anonymous_request_gets_masked_account_codes.
        assert len(resp.rows) == 2
        masked_codes = {r.account for r in resp.rows}
        assert "ZG0790" not in masked_codes and "DH6847" not in masked_codes
        assert resp.total is not None


# ──────────────────────────────────────────────────────────────────────────
# Fix 2 — list_nav: partner gets an empty history; designated unaffected.
# ──────────────────────────────────────────────────────────────────────────

class _FakeNavDaily:
    def __init__(self, as_of_date, nav):
        import datetime as _dt
        self.as_of_date = _dt.date(2026, 10, 30) if as_of_date is None else as_of_date
        self.nav = nav
        self.cash_total = 0.0
        self.positions_mtm = 0.0
        self.holdings_mtm = 0.0
        self.accounts_snapshot = []
        self.note = None


def _mock_nav_list_session(rows):
    session = MagicMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=result)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


class TestListNavPartnerScoping:
    @pytest.mark.asyncio
    async def test_partner_gets_empty_history(self):
        req = MagicMock()
        req.state = MagicMock()
        req.state.token_payload = {"role": "partner"}

        session_factory = _mock_nav_list_session([_FakeNavDaily(None, 1000.0)])
        with patch("backend.api.routes.nav.async_session", session_factory):
            resp = await NavController.list_nav.fn(self=None, request=req, days=90)

        assert resp.rows == []

    @pytest.mark.asyncio
    async def test_designated_still_gets_full_history(self):
        req = MagicMock()
        req.state = MagicMock()
        req.state.token_payload = {"role": "designated"}

        rows = [_FakeNavDaily(None, 1000.0)]
        session_factory = _mock_nav_list_session(rows)
        with patch("backend.api.routes.nav.async_session", session_factory):
            resp = await NavController.list_nav.fn(self=None, request=req, days=90)

        assert len(resp.rows) == 1
        assert resp.rows[0].nav == pytest.approx(1000.0)

    @pytest.mark.asyncio
    async def test_admin_still_gets_full_history(self):
        req = MagicMock()
        req.state = MagicMock()
        req.state.token_payload = {"role": "admin"}

        rows = [_FakeNavDaily(None, 1000.0)]
        session_factory = _mock_nav_list_session(rows)
        with patch("backend.api.routes.nav.async_session", session_factory):
            resp = await NavController.list_nav.fn(self=None, request=req, days=90)

        assert len(resp.rows) == 1
