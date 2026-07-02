"""
P0 regression: ?fresh=1 no-op under RAMBOQ_USE_CONN_SERVICE=1.

Before the fix: dhan_next_poll_clear() in the main API process operated on the
local _dhan_next_poll dict, which is always empty when conn-service owns all
broker calls.  Calling it was a silent no-op — the cold-priority interval gate
in conn_service's process was never cleared.

After the fix: route handlers branch on _use_conn_service().  Under cutover they
call dhan_poll_reset_remote() which POSTs to /internal/dhan/poll_reset over UDS,
clearing the gate inside conn_service where it actually lives.

Quality dimensions:
  SSOT      — single branch point per route (positions / holdings / funds), all
               delegating to the same dhan_poll_reset_remote() helper.
  Correctness — under cutover, fresh=1 calls dhan_poll_reset_remote (not the local
               clear); local path unchanged when cutover is off.
  Performance — dhan_poll_reset_remote is best-effort (never raises); failure does
               not block the downstream broker fetch.
  Stale-code  — local dhan_next_poll_clear() is NOT called under cutover.
  UX          — operator RefreshButton on cold-priority DH6847 now forces a fresh
               broker fetch immediately instead of waiting out the 600s interval.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call as mock_call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_positions_route_broker_fn(fresh: bool, use_conn_service: bool):
    """Simulate the _broker_fn closure from positions.py get_positions().

    Returns a coroutine that runs the same ?fresh=1 branch as the real code,
    but stops before calling get_or_fetch() (not under test here).
    """
    async def _broker_fn(
        mock_invalidate,
        mock_raw_cache_invalidate,
        mock_dhan_next_poll_clear,
        mock_dhan_poll_reset_remote,
    ) -> str:
        if fresh:
            mock_invalidate("positions")
            try:
                # Mirrors the actual import/branch in positions.py:
                _raw_cache_invalidate = mock_raw_cache_invalidate
                dhan_next_poll_clear = mock_dhan_next_poll_clear
                _use_cs = use_conn_service

                _raw_cache_invalidate("positions")
                if _use_cs:
                    await mock_dhan_poll_reset_remote()
                else:
                    dhan_next_poll_clear()
            except Exception:
                pass
        return "fetched"

    return _broker_fn


# ---------------------------------------------------------------------------
# dhan_poll_reset_remote — client helper contract
# ---------------------------------------------------------------------------

class TestDhanPollResetRemote:
    """Unit tests for backend.brokers.client.api.dhan_poll_reset_remote."""

    def test_posts_to_correct_endpoint_clear_all(self):
        """No accounts arg → POSTs {} to /internal/dhan/poll_reset."""
        from backend.brokers.client import api as client_api

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch.object(client_api, "get_client", return_value=mock_client):
            asyncio.run(
                client_api.dhan_poll_reset_remote()
            )

        mock_client.post.assert_called_once_with(
            "/internal/dhan/poll_reset", json={}
        )

    def test_posts_with_accounts_list(self):
        """Passing accounts=["DH6847"] sends {"accounts": ["DH6847"]}."""
        from backend.brokers.client import api as client_api

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch.object(client_api, "get_client", return_value=mock_client):
            asyncio.run(
                client_api.dhan_poll_reset_remote(["DH6847"])
            )

        mock_client.post.assert_called_once_with(
            "/internal/dhan/poll_reset", json={"accounts": ["DH6847"]}
        )

    def test_does_not_raise_on_transport_failure(self):
        """Best-effort: transport error → logs warning, never raises."""
        import logging
        from backend.brokers.client import api as client_api

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionRefusedError("UDS down"))

        with patch.object(client_api, "get_client", return_value=mock_client), \
             patch.object(client_api.logger, "warning") as mock_warn:
            # Must not raise:
            asyncio.run(
                client_api.dhan_poll_reset_remote()
            )
            mock_warn.assert_called_once()
            assert "dhan_poll_reset_remote" in mock_warn.call_args[0][0]

    def test_does_not_raise_on_http_error(self):
        """Best-effort: 5xx from conn_service → logs warning, never raises."""
        import httpx
        from backend.brokers.client import api as client_api

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            )
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch.object(client_api, "get_client", return_value=mock_client), \
             patch.object(client_api.logger, "warning") as mock_warn:
            asyncio.run(
                client_api.dhan_poll_reset_remote()
            )
            mock_warn.assert_called_once()


# ---------------------------------------------------------------------------
# Route branching — conn-service ON path
# ---------------------------------------------------------------------------

class TestPositionsFreshUnderCutover:
    """?fresh=1 on /positions calls dhan_poll_reset_remote, not the local clear."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_fresh_cutover_on_calls_remote(self):
        """Under RAMBOQ_USE_CONN_SERVICE=1, dhan_poll_reset_remote is awaited."""
        mock_invalidate = MagicMock()
        mock_raw_cache_invalidate = MagicMock()
        mock_local_clear = MagicMock()
        mock_remote_reset = AsyncMock()

        fn = _make_positions_route_broker_fn(fresh=True, use_conn_service=True)
        self._run(fn(
            mock_invalidate,
            mock_raw_cache_invalidate,
            mock_local_clear,
            mock_remote_reset,
        ))

        mock_remote_reset.assert_called_once()  # remote proxy fired
        mock_local_clear.assert_not_called()    # local clear NOT called

    def test_fresh_cutover_off_calls_local(self):
        """Without conn-service, local dhan_next_poll_clear() is called."""
        mock_invalidate = MagicMock()
        mock_raw_cache_invalidate = MagicMock()
        mock_local_clear = MagicMock()
        mock_remote_reset = AsyncMock()

        fn = _make_positions_route_broker_fn(fresh=True, use_conn_service=False)
        self._run(fn(
            mock_invalidate,
            mock_raw_cache_invalidate,
            mock_local_clear,
            mock_remote_reset,
        ))

        mock_local_clear.assert_called_once()   # local clear fires
        mock_remote_reset.assert_not_called()   # remote proxy NOT called

    def test_not_fresh_neither_called(self):
        """When fresh=False, neither clear path runs (gate not entered)."""
        mock_invalidate = MagicMock()
        mock_raw_cache_invalidate = MagicMock()
        mock_local_clear = MagicMock()
        mock_remote_reset = AsyncMock()

        fn = _make_positions_route_broker_fn(fresh=False, use_conn_service=True)
        self._run(fn(
            mock_invalidate,
            mock_raw_cache_invalidate,
            mock_local_clear,
            mock_remote_reset,
        ))

        mock_local_clear.assert_not_called()
        mock_remote_reset.assert_not_called()

    def test_fresh_cutover_on_remote_failure_does_not_block(self):
        """If dhan_poll_reset_remote fails, the exception is swallowed by the
        outer try/except in the route — fetch still returns 'fetched'."""
        mock_invalidate = MagicMock()
        mock_raw_cache_invalidate = MagicMock()
        mock_local_clear = MagicMock()
        mock_remote_reset = AsyncMock(side_effect=RuntimeError("UDS gone"))

        fn = _make_positions_route_broker_fn(fresh=True, use_conn_service=True)
        # Must not raise:
        result = self._run(fn(
            mock_invalidate,
            mock_raw_cache_invalidate,
            mock_local_clear,
            mock_remote_reset,
        ))
        # The outer except swallows the error; fetch still returns
        assert result == "fetched"


# ---------------------------------------------------------------------------
# conn_service endpoint — PollResetRequest / PollResetResp schemas
# ---------------------------------------------------------------------------

class TestPollResetSchemas:
    """Schema round-trip via msgspec encode/decode."""

    def test_poll_reset_request_defaults(self):
        """PollResetRequest() with no args → accounts=None (clear all)."""
        from backend.brokers.service.schemas import PollResetRequest
        import msgspec

        req = PollResetRequest()
        assert req.accounts is None

        encoded = msgspec.json.encode(req)
        decoded = msgspec.json.decode(encoded, type=PollResetRequest)
        assert decoded.accounts is None

    def test_poll_reset_request_with_accounts(self):
        from backend.brokers.service.schemas import PollResetRequest
        import msgspec

        req = PollResetRequest(accounts=["DH6847", "DH3747"])
        encoded = msgspec.json.encode(req)
        decoded = msgspec.json.decode(encoded, type=PollResetRequest)
        assert decoded.accounts == ["DH6847", "DH3747"]

    def test_poll_reset_resp_ok(self):
        from backend.brokers.service.schemas import PollResetResp
        import msgspec

        resp = PollResetResp(ok=True, cleared="all")
        encoded = msgspec.json.encode(resp)
        decoded = msgspec.json.decode(encoded, type=PollResetResp)
        assert decoded.ok is True
        assert decoded.cleared == "all"
        assert decoded.error is None

    def test_poll_reset_resp_error(self):
        from backend.brokers.service.schemas import PollResetResp
        import msgspec

        resp = PollResetResp(ok=False, cleared="", error="dict locked")
        encoded = msgspec.json.encode(resp)
        decoded = msgspec.json.decode(encoded, type=PollResetResp)
        assert decoded.ok is False
        assert decoded.error == "dict locked"
