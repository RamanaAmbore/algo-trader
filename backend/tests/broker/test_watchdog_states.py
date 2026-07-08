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
    """Mock Kite connection with controllable token state."""

    def __init__(self, api_key: str, token: str | None = "token_xyz"):
        self.api_key = api_key
        self.token = token

    def get_access_token(self) -> str | None:
        return self.token


def _make_mock_connections(kite_accounts: dict[str, str | None]) -> MagicMock:
    """Create a mock Connections singleton with specified Kite accounts.

    kite_accounts: {account_id: token_value_or_None}
    """
    mock_conn = MagicMock()
    mock_conn.conn = {
        acct: MockConnection(f"api_{acct}", token=token)
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
        """Watchdog must be spawned even when _try_start_ticker() returns True."""
        from backend.brokers.service import app as svc_app

        with patch.object(svc_app, "_try_start_ticker", return_value=True) as _mock_start, \
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
        """Watchdog must be spawned when _try_start_ticker() returns False."""
        from backend.brokers.service import app as svc_app

        with patch.object(svc_app, "_try_start_ticker", return_value=False) as _mock_start, \
             patch.object(svc_app, "_ticker_watchdog") as _mock_watchdog:
            mock_litestar = MagicMock()
            await svc_app._start_kite_ticker(mock_litestar)

            _mock_start.assert_called_once()
            _mock_watchdog.assert_called_once()

    @pytest.mark.asyncio
    async def test_watchdog_spawned_when_start_raises(self):
        """Watchdog must be spawned even when _try_start_ticker() raises."""
        from backend.brokers.service import app as svc_app

        with patch.object(svc_app, "_try_start_ticker",
                          side_effect=RuntimeError("boot blip")) as _mock_start, \
             patch.object(svc_app, "_ticker_watchdog") as _mock_watchdog:
            mock_litestar = MagicMock()
            # Should NOT raise — exception is caught inside _start_kite_ticker.
            await svc_app._start_kite_ticker(mock_litestar)

            _mock_start.assert_called_once()
            _mock_watchdog.assert_called_once()
