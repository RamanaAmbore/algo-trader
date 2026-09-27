"""
Tests for the 2026-09 council audit fix — postback authentication.

Background: /dhan_postback and /groww_postback had `guards=[]` with NO
authentication at all (Kite's postback has HMAC verification; these two
did not). Groww's payload carries no account identifier at all, and the
fallback order-matching path (_pb_fallback_lookup_row) needs no order_id
and no account — just a guessed symbol+side+qty within a 60s window,
matched across EVERY account on the platform. A forged "COMPLETE" fill
could flow into template-attach, which places real GTT/wing orders.

Two-part fix covered here:
  1. _pb_verify_shared_token — a shared-secret query-string token,
     since Dhan/Groww don't sign their webhook payloads (no per-message
     HMAC to verify, unlike Kite).
  2. _pb_fallback_lookup_row's new broker_id narrowing — when account
     can't be filtered directly (Groww), restrict candidate AlgoOrder
     rows to accounts that resolve to the SAME broker as the postback
     claims, instead of searching every account platform-wide.

Five quality dimensions:
  1. SSOT   — tests the real _pb_verify_shared_token / route handlers,
              not a reimplementation.
  2. Perf   — pure unit / mocked DB, no real broker or DB calls.
  3. Stale  — directly reproduces the reported gap (unauthenticated
              postback, cross-broker/cross-account fallback match) and
              proves the fix closes it.
  4. Reuse  — same _handler_fn(Controller.method).fn pattern already
              established in test_order_pair.py for calling Litestar
              Controller methods without a full app/test client.
  5. UX     — the TRANSITIONAL fail-open (secret not yet configured)
              must log CRITICAL once, not spam, and must not silently
              disable itself without any operator-visible signal.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _handler_fn(handler):
    """Return the raw coroutine function from a Litestar route handler
    (same helper as test_order_pair.py — OrdersController.<method> is a
    litestar handler instance; the real coroutine lives at handler.fn)."""
    return handler.fn


def _mock_request(query_params: dict):
    req = MagicMock()
    req.query_params = query_params
    return req


# ── _pb_verify_shared_token ────────────────────────────────────────────────

class TestVerifySharedToken:
    def setup_method(self):
        # Each test gets a clean "already warned" set so the once-per-process
        # log-dedup logic doesn't leak state across tests.
        from backend.api.routes import orders as orders_mod
        orders_mod._PB_TOKEN_WARNED.clear()

    def test_matching_token_verifies(self):
        from backend.api.routes.orders import _pb_verify_shared_token
        req = _mock_request({"token": "s3cr3t"})
        with patch("backend.shared.helpers.utils.secrets", {"dhan_postback_token": "s3cr3t"}):
            assert _pb_verify_shared_token(req, "dhan") is True

    def test_mismatched_token_rejected(self):
        from backend.api.routes.orders import _pb_verify_shared_token
        req = _mock_request({"token": "wrong-guess"})
        with patch("backend.shared.helpers.utils.secrets", {"dhan_postback_token": "s3cr3t"}):
            assert _pb_verify_shared_token(req, "dhan") is False

    def test_missing_token_query_param_rejected(self):
        from backend.api.routes.orders import _pb_verify_shared_token
        req = _mock_request({})  # no 'token' param at all
        with patch("backend.shared.helpers.utils.secrets", {"dhan_postback_token": "s3cr3t"}):
            assert _pb_verify_shared_token(req, "dhan") is False

    def test_unconfigured_secret_fails_open_with_warning(self):
        """TRANSITIONAL behavior: no secret configured yet for this broker
        -> verification passes (True) so existing webhook processing isn't
        broken by deploying this fix, but a CRITICAL log fires."""
        from backend.api.routes.orders import _pb_verify_shared_token
        req = _mock_request({})
        with patch("backend.shared.helpers.utils.secrets", {}), \
             patch("backend.api.routes.orders.logger") as mock_logger:
            result = _pb_verify_shared_token(req, "groww")
        assert result is True
        mock_logger.critical.assert_called_once()

    def test_unconfigured_secret_warns_only_once_per_process(self):
        """The CRITICAL log must not spam on every postback — only once
        per broker_id per process lifetime."""
        from backend.api.routes.orders import _pb_verify_shared_token
        req = _mock_request({})
        with patch("backend.shared.helpers.utils.secrets", {}), \
             patch("backend.api.routes.orders.logger") as mock_logger:
            _pb_verify_shared_token(req, "groww")
            _pb_verify_shared_token(req, "groww")
            _pb_verify_shared_token(req, "groww")
        assert mock_logger.critical.call_count == 1

    def test_two_brokers_warn_independently(self):
        """Each broker_id gets its own once-per-process warning."""
        from backend.api.routes.orders import _pb_verify_shared_token
        req = _mock_request({})
        with patch("backend.shared.helpers.utils.secrets", {}), \
             patch("backend.api.routes.orders.logger") as mock_logger:
            _pb_verify_shared_token(req, "dhan")
            _pb_verify_shared_token(req, "groww")
        assert mock_logger.critical.call_count == 2

    def test_configured_secret_for_one_broker_does_not_leak_to_another(self):
        """A token valid for dhan must not authenticate a groww postback
        that has its OWN, different configured secret."""
        from backend.api.routes.orders import _pb_verify_shared_token
        both_secrets = {"dhan_postback_token": "dhan-secret", "groww_postback_token": "groww-secret"}
        req = _mock_request({"token": "dhan-secret"})
        with patch("backend.shared.helpers.utils.secrets", both_secrets):
            assert _pb_verify_shared_token(req, "groww") is False


# ── Route-level rejection ──────────────────────────────────────────────────

class TestPostbackRouteRejectsInvalidToken:
    @pytest.mark.asyncio
    async def test_dhan_postback_rejects_missing_token(self):
        from backend.api.routes.orders import OrdersController
        from litestar.exceptions import HTTPException

        fn = _handler_fn(OrdersController.order_postback_dhan)
        req = _mock_request({})
        with patch("backend.shared.helpers.utils.secrets", {"dhan_postback_token": "s3cr3t"}):
            with pytest.raises(HTTPException) as exc_info:
                await fn(MagicMock(), req)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_groww_postback_rejects_missing_token(self):
        from backend.api.routes.orders import OrdersController
        from litestar.exceptions import HTTPException

        fn = _handler_fn(OrdersController.order_postback_groww)
        req = _mock_request({})
        with patch("backend.shared.helpers.utils.secrets", {"groww_postback_token": "s3cr3t"}):
            with pytest.raises(HTTPException) as exc_info:
                await fn(MagicMock(), req)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_dhan_postback_with_valid_token_proceeds_past_auth(self):
        """A correctly-authenticated request must NOT be rejected — it
        should proceed to (and fail/skip inside) body parsing, not the
        401 auth gate. Body is intentionally malformed JSON so the test
        doesn't need to mock the entire downstream processing pipeline;
        it only proves the auth gate let a valid token through."""
        from backend.api.routes.orders import OrdersController

        fn = _handler_fn(OrdersController.order_postback_dhan)
        req = _mock_request({"token": "s3cr3t"})
        req.json = AsyncMock(side_effect=ValueError("not json — stub body"))
        with patch("backend.shared.helpers.utils.secrets", {"dhan_postback_token": "s3cr3t"}):
            result = await fn(MagicMock(), req)
        # Non-JSON body path returns {"status": "ok"} without raising —
        # reaching this line at all (not a 401) proves auth passed.
        assert result == {"status": "ok"}


# ── _pb_fallback_lookup_row broker_id narrowing ────────────────────────────

class TestFallbackLookupBrokerNarrowing:
    @pytest.mark.asyncio
    async def test_empty_account_narrows_to_same_broker_accounts(self):
        """account='' (Groww's real payload shape) + broker_id='groww'
        must restrict the query to accounts _broker_id_for() resolves as
        'groww' — proving the where-clause actually includes the narrowing
        filter rather than searching every account platform-wide."""
        from backend.api.routes.orders_postback import _pb_fallback_lookup_row

        captured_stmt = {}

        class _FakeResult:
            def scalars(self):
                class _S:
                    def first(self_inner):
                        return None
                return _S()

        async def _fake_execute(stmt):
            captured_stmt["stmt"] = stmt
            return _FakeResult()

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(side_effect=_fake_execute)

        with patch(
            "backend.brokers.registry._loaded_accounts",
            return_value=["ZG0001", "GR9999", "DH5555"],
        ), patch(
            "backend.brokers.registry._broker_id_for",
            side_effect=lambda a: {
                "ZG0001": "kite", "GR9999": "groww", "DH5555": "dhan",
            }[a],
        ):
            await _pb_fallback_lookup_row(
                fake_session,
                order_id="X1", tradingsymbol="NIFTY25JUL24000CE",
                txn="SELL", qty=50, account="", broker_id="groww",
            )

        stmt_str = str(captured_stmt["stmt"])
        # The compiled statement must reference the account IN-list
        # narrowing — presence of an IN clause on the account column is
        # the observable proxy for "narrowing was applied" without
        # depending on exact SQLAlchemy string formatting.
        assert "account" in stmt_str.lower()
        assert "in_1" in stmt_str.lower() or "in (" in stmt_str.lower() or "in(" in stmt_str.lower()

    @pytest.mark.asyncio
    async def test_registry_failure_falls_back_gracefully(self):
        """If the registry lookup itself raises, the function must not
        crash the postback — it degrades to the unnarrowed match (logged)
        rather than dropping the postback entirely."""
        from backend.api.routes.orders_postback import _pb_fallback_lookup_row

        class _FakeResult:
            def scalars(self):
                class _S:
                    def first(self_inner):
                        return None
                return _S()

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=_FakeResult())

        with patch(
            "backend.brokers.registry._loaded_accounts",
            side_effect=RuntimeError("registry unavailable"),
        ):
            # Must not raise.
            result = await _pb_fallback_lookup_row(
                fake_session,
                order_id="X1", tradingsymbol="NIFTY25JUL24000CE",
                txn="SELL", qty=50, account="", broker_id="groww",
            )
        assert result is None
