"""
2026-10 audit fix — `alert_utils._alert_route()` (the old config-driven
alert dispatcher) was deleted in commit 83dfd346 ("the legacy routing
table and dispatcher are removed, so agent rows are the only channel
source"). Five call sites in `backend/api/background.py` were missed in
that migration and raised `ImportError`/`AttributeError` on every real
incident since, with the resulting alert never sent:

  1. `_send_trail_partial_modify_alert`  (gtt_asymmetric)
  2. `_oco_handle_settled_pair`          (oco_double_fire)
  3. `_watchdog_handle_recovery`         (ticker_recovered)
  4. `_watchdog_alert_degraded`          (ticker_degraded)
  5. `_task_visitor_log_daily`'s inner `_run_once` (visitor_report)

All five are now routed through the same log-record + event-agent
pipeline every other alert in this codebase already uses (new
GTT_ASYMMETRIC_AGENT / OCO_DOUBLE_FIRE_AGENT / TICKER_RECOVERED_AGENT /
TICKER_DEGRADED_AGENT / VISITOR_REPORT_AGENT in event_agents.py).

Two of the five (`_watchdog_handle_recovery`, `_watchdog_alert_degraded`)
previously had NO try/except around the alert call at all — a failure
there would have propagated straight into the ticker watchdog loop.
Both are now wrapped.

Two of the five (`_send_trail_partial_modify_alert`'s `row.account`,
`_oco_handle_settled_pair`'s `entry.get('parent_account')`) embedded the
real broker account WITHOUT masking — fixed via `mask_account()`.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import backend.api.background as bg

_SRC = Path("backend/api/background.py").read_text()


def test_alert_route_no_longer_imported_anywhere_in_background():
    """Confirm `_alert_route` is gone from alert_utils (would raise
    ImportError/AttributeError if background.py tried to import it) AND
    that background.py's own source no longer references it as a real
    import/call (only in explanatory comments, which this regex-free
    check tolerates since the fix comments mention the old name)."""
    from backend.shared.helpers import alert_utils

    with pytest.raises(AttributeError):
        alert_utils._alert_route

    assert "from backend.shared.helpers.alert_utils import _alert_route" not in _SRC
    assert "await asyncio.to_thread(_alert_route" not in _SRC
    assert "await asyncio.to_thread(\n            _alert_route" not in _SRC


class TestGttAsymmetricAlert:
    @pytest.mark.asyncio
    async def test_fires_logged_alert_event_with_masked_account(self, monkeypatch):
        captured = {}

        def _fake_warning(msg, *args, **kwargs):
            if kwargs.get("extra", {}).get("alert_event") == "gtt_asymmetric":
                captured["msg"] = msg % args if args else msg
                captured["extra"] = kwargs["extra"]

        monkeypatch.setattr(bg.logger, "warning", _fake_warning)

        row = SimpleNamespace(id=55, account="ZG0790")
        entry: dict = {}
        await bg._send_trail_partial_modify_alert(
            entry, row, 112.5, "CRUDEOIL26OCTFUT", RuntimeError("rejected"),
        )

        assert captured, "gtt_asymmetric alert_event was never logged"
        assert captured["extra"]["title"] == "GTT asymmetric"
        assert "ZG0790" not in captured["extra"]["body"], "account must be masked"
        assert "ZG####" in captured["extra"]["body"]
        assert "CRUDEOIL26OCTFUT" in captured["extra"]["body"]

    @pytest.mark.asyncio
    async def test_never_raises_even_on_internal_failure(self, monkeypatch):
        """The whole body is wrapped in try/except — a broken mask_account
        or logger must never propagate into the trail/GTT modify path."""
        monkeypatch.setattr(
            "backend.shared.helpers.utils.mask_account",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        row = SimpleNamespace(id=55, account="ZG0790")
        # Must not raise.
        await bg._send_trail_partial_modify_alert(
            {}, row, 112.5, "CRUDEOIL26OCTFUT", RuntimeError("rejected"),
        )


class TestOcoDoubleFireAlert:
    @pytest.mark.asyncio
    async def test_fires_logged_alert_event_with_masked_account(self, monkeypatch):
        captured = {}

        def _fake_warning(msg, *args, **kwargs):
            if kwargs.get("extra", {}).get("alert_event") == "oco_double_fire":
                captured["extra"] = kwargs["extra"]

        monkeypatch.setattr(bg.logger, "warning", _fake_warning)

        entry = {"parent_symbol": "NIFTY26OCTFUT", "parent_account": "ZG0790"}
        await bg._oco_handle_settled_pair(entry, {}, 1, "O1", "O2")

        assert captured, "oco_double_fire alert_event was never logged"
        assert captured["extra"]["title"] == "OCO double-fire"
        assert "ZG0790" not in captured["extra"]["body"]
        assert "ZG####" in captured["extra"]["body"]

    @pytest.mark.asyncio
    async def test_sibling_pointer_cleared_regardless_of_alert_outcome(self, monkeypatch):
        entry = {"parent_symbol": "NIFTY26OCTFUT", "parent_account": "ZG0790",
                  "sibling_id": "O2"}
        sib_entry = {"sibling_id": "O1"}
        by_id = {"O2": sib_entry}
        await bg._oco_handle_settled_pair(entry, by_id, 1, "O1", "O2")
        assert "sibling_id" not in entry
        assert "sibling_id" not in sib_entry


class TestTickerRecoveredAlert:
    @pytest.mark.asyncio
    async def test_fires_logged_alert_event_with_masked_account_and_try_except(self, monkeypatch):
        captured = {}

        def _fake_info(msg, *args, **kwargs):
            if kwargs.get("extra", {}).get("alert_event") == "ticker_recovered":
                captured["extra"] = kwargs["extra"]

        monkeypatch.setattr(bg.logger, "info", _fake_info)
        bg._ticker_alert_state["alert_active"] = True
        bg._ticker_alert_state["incident_start"] = bg._time.time() - 60

        ticker = SimpleNamespace(current_account=lambda: "ZG0790")
        await bg._watchdog_handle_recovery(ticker, {"account": "ZG0790"})

        assert captured, "ticker_recovered alert_event was never logged"
        assert captured["extra"]["title"] == "Ticker recovered"
        assert "ZG0790" not in captured["extra"]["body"]
        assert "ZG####" in captured["extra"]["body"]

    def test_call_site_has_try_except_now(self):
        """Previously had NO try/except — a failure propagated straight
        into the ticker watchdog loop."""
        src = inspect.getsource(bg._watchdog_handle_recovery)
        assert "try:" in src and "except Exception" in src


class TestTickerDegradedAlert:
    @pytest.mark.asyncio
    async def test_fires_logged_alert_event_with_masked_accounts(self, monkeypatch):
        captured = {}

        def _fake_warning(msg, *args, **kwargs):
            if kwargs.get("extra", {}).get("alert_event") == "ticker_degraded":
                captured["extra"] = kwargs["extra"]

        monkeypatch.setattr(bg.logger, "warning", _fake_warning)
        bg._ticker_alert_state["alert_active"] = False
        bg._ticker_alert_state["incident_start"] = 0.0
        bg._ticker_alert_state["last_alerted_at"] = 0.0

        ticker = SimpleNamespace(
            seconds_since_disconnect=lambda: 42.0,
        )
        eligible = [SimpleNamespace(account="ZG0790"), SimpleNamespace(account="ZG0791")]
        await bg._watchdog_alert_degraded(ticker, eligible, "ZG0790", alert_refire_s=600)

        assert captured, "ticker_degraded alert_event was never logged"
        assert captured["extra"]["title"] == "Ticker degraded"
        assert "ZG0790" not in captured["extra"]["body"]
        assert "ZG0791" not in captured["extra"]["body"]
        assert "ZG####" in captured["extra"]["body"]

    def test_call_site_has_try_except_now(self):
        """Previously had NO try/except — a failure propagated straight
        into the ticker watchdog loop."""
        src = inspect.getsource(bg._watchdog_alert_degraded)
        assert "try:" in src and "except Exception" in src


class TestVisitorReportAlert:
    def test_task_source_routes_through_log_event_not_alert_route(self):
        """`_task_visitor_log_daily`'s inner `_run_once` closure isn't
        independently importable, so this asserts on the function's own
        source (same convention as test_background_deploy_sync.py's
        `_SRC` source-inspection checks)."""
        src = inspect.getsource(bg._task_visitor_log_daily)
        assert "import _alert_route" not in src
        assert "await asyncio.to_thread(_alert_route" not in src
        assert '"alert_event": "visitor_report"' in src
        assert '"title": "Visitor report"' in src


class TestNewEventAgentsAreSeedable:
    """The 5 new agents must validate cleanly against the seed-spec
    checker (unknown log tag / unknown renderer / unknown channel would
    silently skip seeding — see event_agents.validate_seed_spec)."""

    @pytest.mark.parametrize("slug", [
        "gtt-asymmetric-alert", "oco-double-fire-alert",
        "ticker-recovered-alert", "ticker-degraded-alert",
        "visitor-report-alert",
    ])
    def test_agent_validates_and_is_seeded(self, slug):
        from backend.api.algo import event_agents

        specs = {s["slug"]: s for s in event_agents.SEEDED_AGENTS}
        assert slug in specs, f"{slug} must be registered in SEEDED_AGENTS"
        assert event_agents.validate_seed_spec(specs[slug]) == []
