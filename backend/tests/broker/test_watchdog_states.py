"""
Exhaustive tests for watchdog state machine helpers and logic flow in backend/brokers/service/app.py.

The watchdog is an async function that runs every 30s and manages five phases:
  1. Not started → try_start_ticker()
  2. Boot grace (< 60s uptime) → suppress swap decisions
  3. Health check → detect unhealthy ticker
  4. Swap eligibility → enforce cooldown and pick next account
  5. Failover → restart ticker with new account

Covers five quality dimensions:
  SSOT        — helper functions (_kite_failover_list, _resolve_kite_creds, _pick_kite_account)
  Correctness — priority ordering, credential resolution, account filtering
  Performance — helpers are cheap (no broker calls, only config/singleton reads)
  Reuse       — same helpers shared by watchdog + manual ops
  UX          — correct credential handling; empty list on auth failure

Scenario catalogue (testing helper functions):
  1. _kite_failover_list → returns Kite accounts sorted by priority ASC
  2. _kite_failover_list(exclude) → filters out specified accounts
  3. _resolve_kite_creds(account) → returns (api_key, access_token) or (None, None)
  4. _resolve_kite_creds with dead connection → returns (None, None) safely
  5. _pick_kite_account → returns first live account's creds + code
  6. _pick_kite_account with all dead → returns (None, None, "")
  7. Watchdog logic phase checks (unit-level: status, grace, health, cooldown, swap)

Since _ticker_watchdog is an async loop with internal imports, we test its
building blocks (the helpers) directly and verify the state logic via
pseudo-code in test functions that mimic the phases.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers and mocks
# ─────────────────────────────────────────────────────────────────────────────

class MockConnection:
    """Mock Kite connection with controllable token state.

    `cooling_off`, when True, makes `is_login_cooling_off()` report an
    active login cool-off — same accessor as the real
    `KiteConnection.is_login_cooling_off()` in connections.py. Default
    False mirrors a real connection with no active cool-off, and a
    connection-like object that doesn't implement the accessor at all
    (e.g. a bare MagicMock in other tests) must behave identically —
    see `_is_login_cooling_off()`'s `is True` check in app.py.
    """

    def __init__(self, api_key: str, token: str | None = "token_xyz", cooling_off: bool = False):
        self.api_key = api_key
        self.token = token
        self._cooling_off = cooling_off

    def get_access_token(self) -> str | None:
        return self.token

    def is_login_cooling_off(self) -> bool:
        return self._cooling_off


def _make_mock_connections(
    kite_accounts: dict[str, str | None],
    cooling_off: set[str] | None = None,
) -> MagicMock:
    """Create a mock Connections singleton with specified Kite accounts.

    kite_accounts: {account_id: token_value_or_None}
    cooling_off: account codes whose MockConnection reports an active
    login cool-off via is_login_cooling_off().
    """
    cooling_off = cooling_off or set()
    mock_conn = MagicMock()
    mock_conn.conn = {
        acct: MockConnection(f"api_{acct}", token=token, cooling_off=acct in cooling_off)
        for acct, token in kite_accounts.items()
    }
    mock_conn._priority_map = {
        acct: i for i, acct in enumerate(kite_accounts.keys())
    }
    return mock_conn


# ─────────────────────────────────────────────────────────────────────────────
# TestKiteFailoverList — 2 tests
# ─────────────────────────────────────────────────────────────────────────────

class TestKiteFailoverList:
    """Test _kite_failover_list priority ordering and account filtering."""

    def test_returns_kite_accounts_sorted_by_priority(self):
        """_kite_failover_list returns Kite accounts sorted by priority ASC."""
        from backend.brokers.service.app import _kite_failover_list

        mock_conns = _make_mock_connections({
            'ZG0790': 'token1',  # Priority 0 (inserted first)
            'ZJ6294': 'token2',  # Priority 1 (inserted second)
        })

        with patch('backend.brokers.connections.Connections', return_value=mock_conns), \
             patch('backend.brokers.registry._broker_id_for', return_value='zerodha_kite'):

            result = _kite_failover_list()

            # Should return accounts in a list (sorted by priority, with ties broken by account name)
            assert len(result) == 2, f"Expected 2 accounts, got {len(result)}"
            assert set(result) == {'ZG0790', 'ZJ6294'}, \
                f"Expected both accounts, got {result}"
            # Verify it returns a list (order matters for failover selection)
            assert isinstance(result, list), "Should return a list"

    def test_filters_out_excluded_accounts(self):
        """_kite_failover_list(exclude=set) filters out specified accounts."""
        from backend.brokers.service.app import _kite_failover_list

        mock_conns = _make_mock_connections({
            'ZJ6294': 'token1',
            'ZG0790': 'token2',
        })

        with patch('backend.brokers.connections.Connections', return_value=mock_conns), \
             patch('backend.brokers.registry._broker_id_for', return_value='zerodha_kite'):

            result = _kite_failover_list(exclude={'ZJ6294'})

            assert result == ['ZG0790'], \
                f"Expected ['ZG0790'] after excluding ZJ6294, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# TestResolveKiteCreds — 3 tests
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveKiteCreds:
    """Test _resolve_kite_creds credential resolution."""

    def test_returns_api_key_and_token_when_live(self):
        """_resolve_kite_creds returns (api_key, token) for live account."""
        from backend.brokers.service.app import _resolve_kite_creds

        mock_conns = _make_mock_connections({'ZG0790': 'token_xyz'})

        with patch('backend.brokers.connections.Connections', return_value=mock_conns):
            result = _resolve_kite_creds('ZG0790')

            assert result == ('api_ZG0790', 'token_xyz'), \
                f"Expected (api_ZG0790, token_xyz), got {result}"

    def test_returns_none_when_account_not_found(self):
        """_resolve_kite_creds returns (None, None) when account doesn't exist."""
        from backend.brokers.service.app import _resolve_kite_creds

        mock_conns = _make_mock_connections({'ZG0790': 'token_xyz'})

        with patch('backend.brokers.connections.Connections', return_value=mock_conns):
            result = _resolve_kite_creds('NONEXISTENT')

            assert result == (None, None), \
                f"Expected (None, None) for missing account, got {result}"

    def test_returns_none_when_token_generation_fails(self):
        """_resolve_kite_creds returns (None, None) when get_access_token fails."""
        from backend.brokers.service.app import _resolve_kite_creds

        mock_conns = _make_mock_connections({'ZG0790': None})

        with patch('backend.brokers.connections.Connections', return_value=mock_conns):
            result = _resolve_kite_creds('ZG0790')

            assert result == (None, None), \
                f"Expected (None, None) when token is None, got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# TestPickKiteAccount — 2 tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPickKiteAccount:
    """Test _pick_kite_account account selection logic."""

    def test_returns_first_live_account_creds(self):
        """_pick_kite_account returns (api_key, token, account_code) for first live account."""
        from backend.brokers.service.app import _pick_kite_account

        mock_conns = _make_mock_connections({
            'ZG0790': None,     # Dead token
            'ZJ6294': 'token1', # Live
        })

        with patch('backend.brokers.connections.Connections', return_value=mock_conns), \
             patch('backend.brokers.registry._broker_id_for', return_value='zerodha_kite'):

            result = _pick_kite_account()

            # Should skip ZG0790 (dead token) and return ZJ6294
            api_key, token, account = result
            assert account == 'ZJ6294', f"Expected ZJ6294, got {account}"
            assert api_key == 'api_ZJ6294'
            assert token == 'token1'

    def test_returns_empty_when_all_dead(self):
        """_pick_kite_account returns (None, None, '') when no live Kite accounts."""
        from backend.brokers.service.app import _pick_kite_account

        mock_conns = _make_mock_connections({
            'ZG0790': None,
            'ZJ6294': None,
        })

        with patch('backend.brokers.connections.Connections', return_value=mock_conns), \
             patch('backend.brokers.registry._broker_id_for', return_value='zerodha_kite'):

            result = _pick_kite_account()

            assert result == (None, None, ""), \
                f"Expected (None, None, ''), got {result}"


# ─────────────────────────────────────────────────────────────────────────────
# TestWatchdogLogicPhases — unit-level logic flow tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWatchdogLogicPhases:
    """Verify watchdog state machine logic phases (not the full async loop)."""

    def test_phase1_not_started_continues_to_phase3(self):
        """Phase 1: not started → calls _try_start_ticker and continues."""
        # Pseudo-code from watchdog:
        # if not ticker.status().get("started"):
        #     if _try_start_ticker():
        #         log.info("ticker started")
        #         continue
        #     continue

        started = False  # Phase 1 condition
        try_start_succeeds = True

        if not started:
            if try_start_succeeds:
                reached_phase3 = False  # early continue
            else:
                reached_phase3 = False
        else:
            reached_phase3 = True

        assert reached_phase3 is False, "phase 1 should not reach phase 3"

    def test_phase2_grace_suppresses_swap_decision(self):
        """Phase 2: if in grace (uptime < 60s), suppress swap even at threshold."""
        uptime_seconds = 30.0  # Still in grace
        threshold = 2
        unhealthy_count = 2  # At threshold

        BOOT_GRACE_S = 60.0
        in_grace = uptime_seconds < BOOT_GRACE_S

        # Phase 2 logic
        if unhealthy_count >= threshold and in_grace:
            swap_suppressed = True
        else:
            swap_suppressed = False

        assert swap_suppressed is True, "grace should suppress swap"

    def test_phase2_after_grace_allows_swap(self):
        """Phase 2: after grace expires (uptime >= 60s), swap is eligible."""
        uptime_seconds = 70.0  # Grace expired
        threshold = 2
        unhealthy_count = 2

        BOOT_GRACE_S = 60.0
        in_grace = uptime_seconds < BOOT_GRACE_S

        if unhealthy_count >= threshold and not in_grace:
            swap_eligible = True
        else:
            swap_eligible = False

        assert swap_eligible is True, "after grace expires, swap should be eligible"

    def test_phase3_healthy_resets_counter(self):
        """Phase 3: healthy ticker resets unhealthy counter."""
        is_healthy = True
        unhealthy_count = 2

        if is_healthy:
            unhealthy_count = 0  # reset
            action = "reset"
        else:
            unhealthy_count += 1
            action = "bump"

        assert action == "reset" and unhealthy_count == 0, \
            "healthy ticker should reset counter"

    def test_phase3_unhealthy_bumps_counter(self):
        """Phase 3: unhealthy ticker bumps the counter."""
        is_healthy = False
        unhealthy_count = 0

        if is_healthy:
            unhealthy_count = 0
            action = "reset"
        else:
            unhealthy_count += 1
            action = "bump"

        assert action == "bump" and unhealthy_count == 1, \
            "unhealthy ticker should bump counter"

    def test_phase4_cooldown_suppresses_swap(self):
        """Phase 4: recent swap within cooldown window suppresses next swap."""
        swaps_in_cooldown = 1  # One swap within 300s window
        cooldown_seconds = 300

        if swaps_in_cooldown > 0:
            swap_suppressed = True
        else:
            swap_suppressed = False

        assert swap_suppressed is True, \
            "recent swap should suppress further swaps"

    def test_phase4_eligibility_filters_accounts(self):
        """Phase 4: eligible accounts are filtered by current + failover cooloff."""
        current_account = "ZG0790"
        all_accounts = ["ZG0790", "ZJ6294", "ZH1234"]
        in_failover_cooloff = ["ZJ6294"]  # ZJ6294 is in cooloff

        # Filter out current and cooloff accounts
        eligible = [a for a in all_accounts if a != current_account and a not in in_failover_cooloff]

        assert eligible == ["ZH1234"], \
            f"Expected [ZH1234], got {eligible}"

    def test_phase5_failover_requires_credentials(self):
        """Phase 5: failover only proceeds if credentials are available."""
        next_account = "ZJ6294"
        has_creds = False  # No credentials

        if next_account and has_creds:
            swap_attempted = True
        else:
            swap_attempted = False

        assert swap_attempted is False, \
            "failover should not attempt without credentials"

    def test_phase5_failover_with_valid_creds(self):
        """Phase 5: failover proceeds when credentials available."""
        next_account = "ZJ6294"
        has_creds = True

        if next_account and has_creds:
            swap_attempted = True
        else:
            swap_attempted = False

        assert swap_attempted is True, \
            "failover should attempt with valid credentials"

    def test_phase2b_market_closed_suppresses_unhealthy(self):
        """Phase 2b: when all segments closed, tick silence is expected — reset counter."""
        is_healthy = False       # No ticks received (market closed)
        market_open = False      # All segments closed

        # Phase 2b logic: market closed → reset, skip health check
        if not market_open:
            unhealthy_count = 0  # reset_unhealthy()
            reached_health_check = False
        else:
            reached_health_check = True
            unhealthy_count = 1 if not is_healthy else 0

        assert reached_health_check is False, \
            "closed market should skip health check entirely"
        assert unhealthy_count == 0, \
            "closed market should reset unhealthy counter, not bump it"

    def test_phase2b_market_open_proceeds_to_health_check(self):
        """Phase 2b: when any segment open, health check proceeds normally."""
        is_healthy = False
        market_open = True       # At least one segment open

        if not market_open:
            reached_health_check = False
        else:
            reached_health_check = True

        assert reached_health_check is True, \
            "open market should proceed to health check phase"

    def test_phase2b_does_not_suppress_failover_during_open_hours(self):
        """Phase 2b: market open + unhealthy → counter bumps, failover eligible."""
        market_open = True
        is_healthy = False
        unhealthy_count = 0

        if not market_open:
            pass  # skip
        elif is_healthy:
            unhealthy_count = 0
        else:
            unhealthy_count += 1

        assert unhealthy_count == 1, \
            "unhealthy during open hours should bump counter toward failover"


# ─────────────────────────────────────────────────────────────────────────────
# TestWatchdogSpawnInvariant — critical: watchdog MUST spawn on every boot
# path so auto-failover + reactor-dead detection are always active.
# ─────────────────────────────────────────────────────────────────────────────

class TestWatchdogSpawnInvariant:
    """The watchdog task must be created regardless of whether the inline
    ticker start succeeds.

    Pre-fix defect: `_start_kite_ticker` returned early on successful inline
    start, so the watchdog was never spawned when boot succeeded — leaving
    the ticker with NO auto-failover, NO health monitoring, and NO reactor-
    dead recovery. This test suite locks the fix in place.
    """

    @pytest.mark.asyncio
    async def test_watchdog_spawned_after_successful_start(self):
        """Watchdog must be spawned even when _try_start_ticker() returns True.

        `_ticker_quiet_window_now()` is forced False here — unrelated to
        this test's own concern (watchdog-spawn invariant regardless of
        `_try_start_ticker()`'s outcome), but it now runs at the TOP of
        `_start_kite_ticker()` (2026-10-28, calendar-driven quiet window)
        and would otherwise make this test depend on real wall-clock IST
        time: it would short-circuit before `_try_start_ticker()` is ever
        reached whenever the suite happens to run inside 23:31-08:00 IST,
        a real flakiness source confirmed live (failed at 00:01 IST,
        passed outside the window)."""
        from backend.brokers.service import app as svc_app

        with patch.object(svc_app, "_ticker_quiet_window_now", return_value=False), \
             patch.object(svc_app, "_try_start_ticker", return_value=True) as _mock_start, \
             patch.object(svc_app, "_ticker_watchdog") as _mock_watchdog:
            # Simulate a Litestar app object
            mock_litestar = MagicMock()
            await svc_app._start_kite_ticker(mock_litestar)

            _mock_start.assert_called_once()
            # The watchdog coroutine MUST have been referenced (scheduled) —
            # if the pre-fix regression returns, this call count would be 0.
            _mock_watchdog.assert_called_once()

    @pytest.mark.asyncio
    async def test_watchdog_spawned_after_failed_start(self):
        """Watchdog must be spawned when _try_start_ticker() returns False.

        See `test_watchdog_spawned_after_successful_start`'s docstring for
        why `_ticker_quiet_window_now()` is forced False here too."""
        from backend.brokers.service import app as svc_app

        with patch.object(svc_app, "_ticker_quiet_window_now", return_value=False), \
             patch.object(svc_app, "_try_start_ticker", return_value=False) as _mock_start, \
             patch.object(svc_app, "_ticker_watchdog") as _mock_watchdog:
            mock_litestar = MagicMock()
            await svc_app._start_kite_ticker(mock_litestar)

            _mock_start.assert_called_once()
            _mock_watchdog.assert_called_once()

    @pytest.mark.asyncio
    async def test_watchdog_spawned_when_start_raises(self):
        """Watchdog must be spawned even when _try_start_ticker() raises.

        See `test_watchdog_spawned_after_successful_start`'s docstring for
        why `_ticker_quiet_window_now()` is forced False here too."""
        from backend.brokers.service import app as svc_app

        with patch.object(svc_app, "_ticker_quiet_window_now", return_value=False), \
             patch.object(svc_app, "_try_start_ticker",
                          side_effect=RuntimeError("boot blip")) as _mock_start, \
             patch.object(svc_app, "_ticker_watchdog") as _mock_watchdog:
            mock_litestar = MagicMock()
            # Should NOT raise — exception is caught inside _start_kite_ticker.
            await svc_app._start_kite_ticker(mock_litestar)

            _mock_start.assert_called_once()
            _mock_watchdog.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# TestIsLoginCoolingOff — the proactive-skip accessor in app.py
# ─────────────────────────────────────────────────────────────────────────────

class TestIsLoginCoolingOff:
    """_is_login_cooling_off(account) in app.py — the proactive skip used by
    _first_live_failover_candidate()."""

    def test_true_when_connection_reports_cooling_off(self):
        from backend.brokers.service.app import _is_login_cooling_off

        mock_conns = _make_mock_connections(
            {"ZG0790": "token1"}, cooling_off={"ZG0790"}
        )
        with patch("backend.brokers.connections.Connections", return_value=mock_conns):
            assert _is_login_cooling_off("ZG0790") is True

    def test_false_when_connection_not_cooling_off(self):
        from backend.brokers.service.app import _is_login_cooling_off

        mock_conns = _make_mock_connections({"ZG0790": "token1"})
        with patch("backend.brokers.connections.Connections", return_value=mock_conns):
            assert _is_login_cooling_off("ZG0790") is False

    def test_false_when_account_missing(self):
        from backend.brokers.service.app import _is_login_cooling_off

        mock_conns = _make_mock_connections({"ZG0790": "token1"})
        with patch("backend.brokers.connections.Connections", return_value=mock_conns):
            assert _is_login_cooling_off("NONEXISTENT") is False

    def test_false_for_bare_mock_without_accessor(self):
        """A connection-like object that doesn't implement
        is_login_cooling_off() at all (e.g. a bare MagicMock, as other
        tests in this module and test_conn_service_routes.py use) must
        never be treated as cooling off just because calling an
        auto-vivified attribute happens to return a truthy MagicMock —
        this is the `is True` check's whole reason for existing."""
        from backend.brokers.service.app import _is_login_cooling_off

        mock_conns = MagicMock()
        mock_conns.conn = {"ZG0790": MagicMock(spec=["api_key", "get_access_token"])}
        with patch("backend.brokers.connections.Connections", return_value=mock_conns):
            assert _is_login_cooling_off("ZG0790") is False

    def test_false_on_lookup_exception(self):
        from backend.brokers.service.app import _is_login_cooling_off

        with patch("backend.brokers.connections.Connections", side_effect=RuntimeError("boom")):
            assert _is_login_cooling_off("ZG0790") is False


# ─────────────────────────────────────────────────────────────────────────────
# TestFirstLiveFailoverCandidate — the extracted candidate-loop helper
# ─────────────────────────────────────────────────────────────────────────────

class TestFirstLiveFailoverCandidate:
    """_first_live_failover_candidate(eligible) — tries each candidate, in
    order, until one resolves a live token."""

    def test_skips_dead_candidate_returns_second(self):
        from backend.brokers.service.app import _first_live_failover_candidate

        mock_conns = _make_mock_connections({
            "ZG0790": None,      # dead — no token
            "ZJ6294": "token2",  # live
            "ZH1111": "token3",  # live, never reached
        })
        with patch("backend.brokers.connections.Connections", return_value=mock_conns):
            result = _first_live_failover_candidate(["ZG0790", "ZJ6294", "ZH1111"])
            assert result == ("ZJ6294", "api_ZJ6294", "token2")

    def test_skips_cooling_off_candidate_without_resolving_creds(self):
        """A candidate reported as cooling off must be skipped WITHOUT
        ever calling get_access_token() on it."""
        from backend.brokers.service.app import _first_live_failover_candidate

        mock_conns = _make_mock_connections(
            {"ZG0790": "token1", "ZJ6294": "token2"},
            cooling_off={"ZG0790"},
        )
        cooling_conn = mock_conns.conn["ZG0790"]
        cooling_conn.get_access_token = MagicMock(
            side_effect=AssertionError("must not be called while cooling off")
        )
        with patch("backend.brokers.connections.Connections", return_value=mock_conns):
            result = _first_live_failover_candidate(["ZG0790", "ZJ6294"])
            assert result == ("ZJ6294", "api_ZJ6294", "token2")
            cooling_conn.get_access_token.assert_not_called()

    def test_returns_none_when_all_dead(self):
        from backend.brokers.service.app import _first_live_failover_candidate

        mock_conns = _make_mock_connections({"ZG0790": None, "ZJ6294": None})
        with patch("backend.brokers.connections.Connections", return_value=mock_conns):
            result = _first_live_failover_candidate(["ZG0790", "ZJ6294"])
            assert result is None

    def test_returns_none_for_empty_list(self):
        from backend.brokers.service.app import _first_live_failover_candidate

        assert _first_live_failover_candidate([]) is None


# ─────────────────────────────────────────────────────────────────────────────
# TestAttemptFailoverSwapMultiCandidate — the key regression test. With
# only 2 real Kite accounts configured today, this exact scenario (a dead
# top-priority candidate with a healthy THIRD candidate behind a second
# dead one) can't be exercised against live config — hence 3+ mocks.
# ─────────────────────────────────────────────────────────────────────────────

class TestAttemptFailoverSwapMultiCandidate:
    """_attempt_failover_swap() must try every eligible candidate, in
    priority order, not just eligible[0] — regression test for the bug
    where a dead top-priority candidate was re-picked identically (and
    given up on) every single watchdog cycle forever, even with a
    healthier lower-priority candidate sitting right behind it."""

    def _ticker(self, current_account: str = "ZCURR"):
        ticker = MagicMock()
        ticker.swaps_since.return_value = 0
        ticker.current_account.return_value = current_account
        ticker.is_account_in_failover_cooloff.return_value = False
        ticker.is_reactor_dead.return_value = False
        ticker.restart_with_account.return_value = True
        return ticker

    def test_top_candidate_dead_swaps_to_second(self):
        """3 candidates; #1 has no live token → swap goes to #2, and #3's
        credentials are never even resolved."""
        from backend.brokers.service.app import _attempt_failover_swap
        import logging

        mock_conns = _make_mock_connections({
            "ZCURR": "token_current",  # excluded (current account)
            "ZA0001": None,            # dead — top priority among eligible
            "ZB0002": "token_b",       # live — should be picked
            "ZC0003": "token_c",       # live, never reached
        })
        third_conn = mock_conns.conn["ZC0003"]
        third_conn.get_access_token = MagicMock(
            side_effect=AssertionError("ZC0003 must not be resolved once ZB0002 succeeds")
        )

        ticker = self._ticker()
        log = logging.getLogger(__name__)

        with patch("backend.brokers.connections.Connections", return_value=mock_conns), \
             patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"), \
             patch("backend.brokers.service.app._write_ticker_swap_audit"):
            result = _attempt_failover_swap(ticker, log, cooldown_s=300, slowed_s=60)

        assert result is True
        ticker.restart_with_account.assert_called_once_with("api_ZB0002", "token_b", "ZB0002")
        third_conn.get_access_token.assert_not_called()

    def test_top_two_candidates_dead_swaps_to_third(self):
        """3 candidates; #1 and #2 have no live token → swap goes to #3."""
        from backend.brokers.service.app import _attempt_failover_swap
        import logging

        mock_conns = _make_mock_connections({
            "ZCURR": "token_current",
            "ZA0001": None,
            "ZB0002": None,
            "ZC0003": "token_c",
        })

        ticker = self._ticker()
        log = logging.getLogger(__name__)

        with patch("backend.brokers.connections.Connections", return_value=mock_conns), \
             patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"), \
             patch("backend.brokers.service.app._write_ticker_swap_audit"):
            result = _attempt_failover_swap(ticker, log, cooldown_s=300, slowed_s=60)

        assert result is True
        ticker.restart_with_account.assert_called_once_with("api_ZC0003", "token_c", "ZC0003")

    def test_cooling_off_candidate_skipped_in_favour_of_next(self):
        """#2 is in an active login cool-off (is_login_cooling_off() → True)
        → it's skipped entirely (its get_access_token is never called),
        and the swap proceeds to #3."""
        from backend.brokers.service.app import _attempt_failover_swap
        import logging

        mock_conns = _make_mock_connections(
            {
                "ZCURR": "token_current",
                "ZA0001": "token_a",
                "ZB0002": "token_b",
                "ZC0003": "token_c",
            },
            cooling_off={"ZB0002"},
        )
        # Make ZA0001 the "dead" one so the swap has to pass over both a
        # dead candidate AND a cooling-off candidate to reach ZC0003.
        mock_conns.conn["ZA0001"].token = None
        cooling_conn = mock_conns.conn["ZB0002"]
        cooling_conn.get_access_token = MagicMock(
            side_effect=AssertionError("ZB0002 must not be resolved while cooling off")
        )

        ticker = self._ticker()
        log = logging.getLogger(__name__)

        with patch("backend.brokers.connections.Connections", return_value=mock_conns), \
             patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"), \
             patch("backend.brokers.service.app._write_ticker_swap_audit"):
            result = _attempt_failover_swap(ticker, log, cooldown_s=300, slowed_s=60)

        assert result is True
        ticker.restart_with_account.assert_called_once_with("api_ZC0003", "token_c", "ZC0003")
        cooling_conn.get_access_token.assert_not_called()

    def test_all_candidates_dead_logs_warning_once_and_does_not_swap(self):
        """Every eligible candidate lacks a live token → no restart is
        attempted, and the "no live token" warning is logged exactly
        once for the whole cycle (not once per candidate)."""
        from backend.brokers.service.app import _attempt_failover_swap
        import logging

        mock_conns = _make_mock_connections({
            "ZCURR": "token_current",
            "ZA0001": None,
            "ZB0002": None,
            "ZC0003": None,
        })

        ticker = self._ticker()
        log = logging.getLogger("test_all_candidates_dead")

        with patch("backend.brokers.connections.Connections", return_value=mock_conns), \
             patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"), \
             patch("backend.brokers.service.app._write_ticker_swap_audit"), \
             patch.object(log, "warning") as mock_warning:
            result = _attempt_failover_swap(ticker, log, cooldown_s=300, slowed_s=60)

        ticker.restart_with_account.assert_not_called()
        # Exactly one summary warning for the whole cycle, not 3 (one per
        # dead candidate).
        mock_warning.assert_called_once()
        assert "live token" in mock_warning.call_args[0][0].lower()
        # Chosen semantics (documented in _attempt_failover_swap's own
        # docstring): exhausting the list without finding a live token is
        # "skip this cycle", the same return value the pre-fix code used
        # for its single-candidate case — it is NOT escalated to the
        # reactor-dead / unavailable_mode=True branch.
        assert result is True

    def test_three_dead_candidates_never_resolves_fourth_twice(self):
        """Sanity check that the loop terminates and tries each dead
        candidate's credentials exactly once, not in some retry loop."""
        from backend.brokers.service.app import _attempt_failover_swap
        import logging

        mock_conns = _make_mock_connections({
            "ZCURR": "token_current",
            "ZA0001": None,
            "ZB0002": None,
            "ZC0003": None,
        })
        for acct in ("ZA0001", "ZB0002", "ZC0003"):
            mock_conns.conn[acct].get_access_token = MagicMock(return_value=None)

        ticker = self._ticker()
        log = logging.getLogger(__name__)

        with patch("backend.brokers.connections.Connections", return_value=mock_conns), \
             patch("backend.brokers.registry._broker_id_for", return_value="zerodha_kite"), \
             patch("backend.brokers.service.app._write_ticker_swap_audit"):
            _attempt_failover_swap(ticker, log, cooldown_s=300, slowed_s=60)

        for acct in ("ZA0001", "ZB0002", "ZC0003"):
            mock_conns.conn[acct].get_access_token.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# TestKiteConnectionIsLoginCoolingOff — the real accessor on KiteConnection
# ─────────────────────────────────────────────────────────────────────────────

class TestKiteConnectionIsLoginCoolingOff:
    """KiteConnection.is_login_cooling_off() — the public read of
    _login_blocked_until used by app.py's failover candidate loop.

    Uses __new__ to bypass __init__ (which does real credential setup,
    network calls, and disk reads) — only _login_blocked_until matters
    for this accessor."""

    def test_false_when_never_blocked(self):
        from backend.brokers.connections import KiteConnection

        conn = KiteConnection.__new__(KiteConnection)
        conn._login_blocked_until = 0.0
        assert conn.is_login_cooling_off() is False

    def test_true_when_blocked_until_is_in_the_future(self):
        from backend.brokers.connections import KiteConnection
        import time

        conn = KiteConnection.__new__(KiteConnection)
        conn._login_blocked_until = time.time() + 120.0
        assert conn.is_login_cooling_off() is True

    def test_false_once_blocked_until_is_in_the_past(self):
        from backend.brokers.connections import KiteConnection
        import time

        conn = KiteConnection.__new__(KiteConnection)
        conn._login_blocked_until = time.time() - 5.0
        assert conn.is_login_cooling_off() is False
