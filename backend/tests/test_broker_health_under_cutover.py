"""
D1 regression: is_account_healthy() under RAMBOQ_USE_CONN_SERVICE=1.

Before the fix: local _FETCH_HEALTH is empty when the main API process runs
under cutover (all broker calls live in conn_service). is_account_healthy()
would find no local entry and return True ("never tried = benefit of the
doubt") — so the navbar always shows 5/5 even during real Dhan/Groww
auth failures.

After the fix: is_account_healthy() consults fetch_health_snapshot() when
the local dict has no entry AND _use_conn_service() is True.
fetch_health_snapshot() has the conn_service-aware path: it hits the
/internal/health/brokers endpoint and returns the real per-account map.

Quality dimensions:
  SSOT — single is_account_healthy() call site, routed correctly per env.
  Correctness — navbar badge count reflects reality under cutover.
  No stale-code regression — _FETCH_HEALTH local dict untouched for
    non-cutover processes (self-contained conn_service workaround only).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


class TestIsAccountHealthyUnderCutover:
    """When RAMBOQ_USE_CONN_SERVICE=1 and _FETCH_HEALTH is empty,
    is_account_healthy() must consult fetch_health_snapshot() to get the
    real per-account state from conn_service."""

    def _patch_cutover(self, *, on: bool):
        """Return a context manager that flips _USE_CONN_SERVICE."""
        return patch("backend.brokers.broker_apis._USE_CONN_SERVICE", on)

    def _clear_local_health(self):
        """Empty _FETCH_HEALTH so the local fast-path is bypassed."""
        from backend.brokers import broker_apis
        broker_apis._FETCH_HEALTH.clear()

    def test_unhealthy_account_returns_false_under_cutover(self):
        """DH3747 is in the conn_service snapshot with a failed fetch.
        is_account_healthy('DH3747') must return False."""
        self._clear_local_health()
        now = time.time()
        snapshot = {
            "DH3747": {"last_ok_at": 0.0, "last_fail_at": now, "last_fail_msg": "Invalid Token"},
            "ZG0790": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
            "ZJ6294": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
        }
        with self._patch_cutover(on=True), \
             patch("backend.brokers.broker_apis.fetch_health_snapshot", return_value=snapshot):
            from backend.brokers.broker_apis import is_account_healthy
            assert is_account_healthy("DH3747") is False, (
                "DH3747 has last_fail_at > last_ok_at — must be unhealthy"
            )

    def test_healthy_accounts_return_true_under_cutover(self):
        """ZG0790 and ZJ6294 have successful fetches — must still return True."""
        self._clear_local_health()
        now = time.time()
        snapshot = {
            "DH3747": {"last_ok_at": 0.0, "last_fail_at": now, "last_fail_msg": "Invalid Token"},
            "ZG0790": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
            "ZJ6294": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
        }
        with self._patch_cutover(on=True), \
             patch("backend.brokers.broker_apis.fetch_health_snapshot", return_value=snapshot):
            from backend.brokers.broker_apis import is_account_healthy
            assert is_account_healthy("ZG0790") is True
            assert is_account_healthy("ZJ6294") is True

    def test_account_not_in_snapshot_returns_true(self):
        """An account that conn_service has never tried should get benefit of the doubt."""
        self._clear_local_health()
        snapshot: dict = {}  # conn_service has no entry either
        with self._patch_cutover(on=True), \
             patch("backend.brokers.broker_apis.fetch_health_snapshot", return_value=snapshot):
            from backend.brokers.broker_apis import is_account_healthy
            assert is_account_healthy("UNKNOWN_ACCT") is True

    def test_local_dict_takes_precedence_over_snapshot(self):
        """When _FETCH_HEALTH has a local entry, it must win over fetch_health_snapshot().
        This covers the non-cutover path and the case where the local process DID
        run broker calls (e.g. the conn_service process itself)."""
        from backend.brokers import broker_apis
        now = time.time()
        # Inject a local 'ok' entry for DH3747
        broker_apis._FETCH_HEALTH["DH3747"] = {
            "last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""
        }
        # Snapshot says it's unhealthy — must be ignored
        snapshot = {
            "DH3747": {"last_ok_at": 0.0, "last_fail_at": now, "last_fail_msg": "bad"}
        }
        mock_snapshot = MagicMock(return_value=snapshot)
        with self._patch_cutover(on=True), \
             patch("backend.brokers.broker_apis.fetch_health_snapshot", mock_snapshot):
            from backend.brokers.broker_apis import is_account_healthy
            assert is_account_healthy("DH3747") is True, (
                "Local _FETCH_HEALTH entry must take precedence"
            )
        # fetch_health_snapshot must NOT have been called (fast local path)
        mock_snapshot.assert_not_called()
        # Cleanup
        broker_apis._FETCH_HEALTH.pop("DH3747", None)

    def test_non_cutover_missing_account_returns_true_without_snapshot_call(self):
        """Without cutover, a missing local entry returns True and never
        calls fetch_health_snapshot() (no UDS round-trip)."""
        self._clear_local_health()
        mock_snapshot = MagicMock(return_value={})
        with self._patch_cutover(on=False), \
             patch("backend.brokers.broker_apis.fetch_health_snapshot", mock_snapshot):
            from backend.brokers.broker_apis import is_account_healthy
            assert is_account_healthy("ZG0790") is True
        mock_snapshot.assert_not_called()

    def test_loaded_accounts_excludes_unhealthy_under_cutover(self):
        """_loaded_accounts() in brokers.py uses is_account_healthy(). Under
        cutover with DH3747 unhealthy, the returned set must NOT include DH3747.
        Badge count = len(loaded) must reflect reality."""
        self._clear_local_health()
        now = time.time()
        snapshot = {
            "DH3747": {"last_ok_at": 0.0, "last_fail_at": now, "last_fail_msg": "auth"},
            "ZG0790": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
            "ZJ6294": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
            "DH3748": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
            "GR87DF": {"last_ok_at": now, "last_fail_at": 0.0, "last_fail_msg": ""},
        }
        all_accounts = {"DH3747", "ZG0790", "ZJ6294", "DH3748", "GR87DF"}

        with self._patch_cutover(on=True), \
             patch("backend.brokers.broker_apis.fetch_health_snapshot", return_value=snapshot):
            from backend.brokers.broker_apis import is_account_healthy
            loaded = {a for a in all_accounts if is_account_healthy(a)}

        assert "DH3747" not in loaded, "DH3747 is unhealthy — must be excluded from badge count"
        assert loaded == {"ZG0790", "ZJ6294", "DH3748", "GR87DF"}, (
            f"Expected 4/5 loaded accounts, got: {loaded}"
        )
