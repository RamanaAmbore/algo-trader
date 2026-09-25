"""Regression coverage for the conn-service → sync-client failure boundary.

Bug (A1, 2026-09 plan "Fix 0-instead-of-last-known-good"): conn_service's
/internal/holdings|positions|margins handlers (backend/brokers/service/
routes.py) catch every exception raised during the per-account fetch loop
and return HTTP 200 with `accounts: [], errors: [...]`. Before this fix,
backend.brokers.client.sync._fetch_per_account never read `payload.errors`
and treated `accounts: []` as a genuine empty result — no `fetch_failed`
sentinel was ever attached, so a real broker-layer exception rendered as
an empty-but-"successful" fetch. Downstream (NavStrip, Payoff chart) then
displayed 0 instead of freezing on the last-known-good value.

Fix: `_fetch_per_account` now treats a non-empty `payload.errors` list as
a degraded response and returns the same `fetch_failed` sentinel shape
already used on transport/HTTP failure — `[pd.DataFrame()]` with
`attrs["fetch_failed"] = True`.

These tests reproduce the exact scenario end-to-end (conn_service's real
route handler raising internally → sync client decoding its response) and
guard the negative case (a genuinely empty book must NOT be marked failed,
or the 08:00 daily-rollover / "operator closed everything" state would be
permanently stuck showing "degraded").
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import msgspec
import pandas as pd
import pytest

from backend.brokers.client import sync as conn_sync
from backend.brokers.service.routes import InternalBrokerController
from backend.brokers.service.schemas import InternalAccountEnvelope, InternalPerAccountResp


def _mock_resp(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    return resp


class TestSyncClientDegradedResponse:
    """`payload.errors` non-empty ⇒ fetch_failed sentinel, not a silent []."""

    @pytest.mark.parametrize("fetch_fn_name,path", [
        ("fetch_holdings", "/internal/holdings"),
        ("fetch_positions", "/internal/positions"),
        ("fetch_margins", "/internal/margins"),
    ])
    def test_degraded_empty_200_becomes_fetch_failed(self, fetch_fn_name, path):
        """conn_service's internal exception path (200, accounts=[],
        errors=[...]) must surface as a fetch_failed sentinel, matching
        the existing transport-error convention — never a silent []."""
        payload = InternalPerAccountResp(accounts=[], errors=["boom: connection reset"])
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            fn = getattr(conn_sync, fetch_fn_name)
            result = fn()

        assert result != [], (
            f"{fetch_fn_name}: a degraded 200 response must never silently "
            f"collapse to an empty list — downstream treats [] as a "
            f"confirmed-empty book, not an outage"
        )
        assert len(result) == 1
        assert isinstance(result[0], pd.DataFrame)
        assert result[0].attrs.get("fetch_failed") is True, (
            f"{fetch_fn_name}: fetch_failed sentinel must be attached when "
            f"conn_service reports errors"
        )

    def test_genuinely_empty_book_is_not_marked_failed(self):
        """The negative case: accounts=[] with NO errors (e.g. zero broker
        accounts configured on this box, or a legitimately empty response
        entry) must NOT be treated as a failure — this is what lets the
        08:00 daily-rollover / 'operator closed everything' states still
        show a real 0 instead of being stuck permanently 'degraded'."""
        payload = InternalPerAccountResp(accounts=[], errors=[])
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            result = conn_sync.fetch_positions()

        assert result == [], "zero configured accounts + no errors is a genuine empty result"

    def test_genuinely_empty_account_book_is_not_marked_failed(self):
        """A real account with a real empty book (ok=True, rows=[]) and no
        top-level errors must round-trip as a normal empty-but-healthy
        frame — not fetch_failed."""
        payload = InternalPerAccountResp(
            accounts=[InternalAccountEnvelope(account="ZG0790", ok=True, rows=[])],
            errors=[],
        )
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            result = conn_sync.fetch_holdings()

        assert len(result) == 1
        assert result[0].attrs.get("fetch_failed") is not True

    def test_partial_failure_still_marks_only_the_failing_account(self):
        """ok=False on one envelope entry (per-account failure, NOT the
        whole-batch exception path) must still mark only that frame —
        unrelated to the errors-list check, this is the pre-existing
        per-entry `ok` contract and must keep working unchanged."""
        payload = InternalPerAccountResp(
            accounts=[
                InternalAccountEnvelope(account="ZG0790", ok=True, rows=[{"tradingsymbol": "RELIANCE"}]),
                InternalAccountEnvelope(account="DH6847", ok=False, rows=[]),
            ],
            errors=[],
        )
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            result = conn_sync.fetch_positions()

        assert len(result) == 2
        assert result[0].attrs.get("fetch_failed") is not True
        assert result[1].attrs.get("fetch_failed") is True


class TestConnServiceRouteToClientEndToEnd:
    """Full repro of the brief's exact scenario: conn_service's real route
    handler raises internally → produces the real wire bytes → sync client
    decodes them. Confirms the fix at the actual process boundary, not
    just against a hand-built payload."""

    @pytest.mark.parametrize("handler_name,broker_apis_fn,client_fn_name", [
        ("holdings", "fetch_holdings", "fetch_holdings"),
        ("positions", "fetch_positions", "fetch_positions"),
        ("margins", "fetch_margins", "fetch_margins"),
    ])
    def test_internal_route_exception_never_reads_as_silent_empty(
        self, handler_name, broker_apis_fn, client_fn_name,
    ):
        handler = InternalBrokerController.__dict__[handler_name].fn

        with patch(
            f"backend.brokers.broker_apis.{broker_apis_fn}",
            side_effect=RuntimeError("kite session expired"),
        ):
            route_resp = asyncio.run(handler(object()))

        assert route_resp.accounts == []
        assert route_resp.errors, "route handler must record the exception in errors"

        wire_bytes = msgspec.json.encode(route_resp)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(wire_bytes))
            mock_get_client.return_value = mock_client

            client_fn = getattr(conn_sync, client_fn_name)
            result = client_fn()

        assert result != []
        assert result[0].attrs.get("fetch_failed") is True


class TestBrokerApisSurvivesConnServiceDegradedResponse:
    """Confirms the fetch_failed marker set by the client-boundary fix
    actually survives broker_apis.py's post-processing (_apply_backfill_
    to_list) and reaches the public fetch_positions()/fetch_holdings()/
    fetch_margins() entry points that routes/positions.py etc. call.

    Read-only w.r.t. broker_apis.py — no changes made there; this test
    exists to prove the A1 client-boundary fix is not silently undone by
    a downstream transform living in a file owned by a parallel agent."""

    def _run(self, monkeypatch, fetch_name: str):
        from backend.brokers import broker_apis

        monkeypatch.setattr(broker_apis, "_use_conn_service", lambda: True)
        payload = InternalPerAccountResp(accounts=[], errors=["boom"])
        content = msgspec.json.encode(payload)

        with patch("backend.brokers.client.sync._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.get = MagicMock(return_value=_mock_resp(content))
            mock_get_client.return_value = mock_client

            broker_apis._raw_cache_invalidate(None)
            try:
                fn = getattr(broker_apis, fetch_name)
                result = fn()
            finally:
                broker_apis._raw_cache_invalidate(None)

        assert result, f"{fetch_name}: degraded conn_service response must not vanish into []"
        assert any(
            bool(getattr(df, "attrs", {}).get("fetch_failed")) for df in result
        ), f"{fetch_name}: fetch_failed marker must survive broker_apis post-processing"

    def test_fetch_positions_public_entry_sees_fetch_failed(self, monkeypatch):
        self._run(monkeypatch, "fetch_positions")

    def test_fetch_holdings_public_entry_sees_fetch_failed(self, monkeypatch):
        self._run(monkeypatch, "fetch_holdings")

    def test_fetch_margins_public_entry_sees_fetch_failed(self, monkeypatch):
        self._run(monkeypatch, "fetch_margins")
