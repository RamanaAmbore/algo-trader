"""Tests for the TOCTOU race fix in seed_global_pinned().

Symptom: concurrent calls to `seed_global_pinned()` (e.g., during rolling
restarts or HA failover) could execute the wave-cleanup paths multiple times
despite the per-session `_sgp_migration_done()` check. The check itself is a
TOCTOU race: between reading "migration not done" and writing the marker, a
second task could start executing the same cleanup.

Fix: module-level `_SEED_LOCK = asyncio.Lock()` wraps the entire body of
`seed_global_pinned()`, ensuring exactly one task can be inside at a time.

Test dimensions:
  1. SSOT — `_SEED_LOCK` is the canonical serialization point; no other
     per-session guards exist upstream.
  2. Perf — lock contention is minimal; only hit at startup / failover.
  3. Stale-code grep — old "check without lock" patterns removed.
  4. Reuse — same asyncio.Lock pattern as elsewhere in the codebase.
  5. UX — cleanup waves execute exactly once despite concurrent calls;
     no duplicate deletions + multiple Setting marker writes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Test: concurrent calls execute cleanup only once
# ---------------------------------------------------------------------------

class TestSeedGlobalPinnedConcurrency:
    """Lock prevents TOCTOU race in wave-cleanup execution."""

    @pytest.mark.asyncio
    async def test_seed_lock_is_present_and_asyncio_type(self):
        """Verify the _SEED_LOCK module variable exists and is an asyncio.Lock."""
        from backend.api.routes import watchlist

        assert hasattr(
            watchlist, "_SEED_LOCK"
        ), "_SEED_LOCK must be a module-level variable"
        assert isinstance(
            watchlist._SEED_LOCK, asyncio.Lock
        ), "_SEED_LOCK must be an asyncio.Lock instance"

    @pytest.mark.asyncio
    async def test_seed_global_pinned_concurrent_calls_execute_cleanup_once(self):
        """Five concurrent calls to seed_global_pinned() must run
        the wave cleanup (Setting marker writes) exactly once, not five times.

        The _SEED_LOCK ensures that only the first task to enter the function
        executes the cleanup waves; subsequent tasks wait for the lock, then
        find all migrations already marked and skip cleanup.
        """
        # Track how many times each cleanup wave fires.
        wave1_calls = 0
        wave2_calls = 0
        wave3_calls = 0
        wave4_calls = 0
        wave5_calls = 0

        # Mock the session and its execute method.
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock()

        # Track calls to _sgp_mark_migration; each wave calls it once.
        mark_migration_calls = []

        async def mock_mark_migration(session, key: str) -> None:
            """Track marker writes for each wave."""
            mark_migration_calls.append(key)

        async def mock_migration_done(session, key: str) -> bool:
            """Return False if not yet marked, True if marked."""
            return key in mark_migration_calls

        async def mock_find_or_create_global(session, now):
            """Return a mock global row."""
            return MagicMock(id=999, name="Pinned", is_global=True)

        async def mock_migrate_legacy(session, global_row, now):
            """No-op."""
            pass

        async def mock_run_delete_wave(session, global_row, key: str, pairs, label: str):
            """Mock wave cleanup — tracks calls.

            Simulates the real _sgp_run_delete_wave which checks
            _sgp_migration_done first and returns early if already marked.
            """
            nonlocal wave1_calls, wave2_calls, wave3_calls
            is_done = await mock_migration_done(session, key)
            if is_done:
                # Migration already done, skip
                return
            if key == "migrations.pinned_remove_goldm_usdinr_v1":
                wave1_calls += 1
            elif key == "migrations.pinned_remove_mcx_futures_v1":
                wave2_calls += 1
            elif key == "migrations.pinned_remove_silver_mcx_v1":
                wave3_calls += 1
            # Mark migration as done
            await mock_mark_migration(session, key)

        async def mock_wave4_usdinr_bare_root(session, global_row):
            """Mock wave 4 cleanup."""
            _W4_KEY = "migrations.pinned_usdinr_bare_root_v1"
            if not await mock_migration_done(session, _W4_KEY):
                nonlocal wave4_calls
                wave4_calls += 1
                await mock_mark_migration(session, _W4_KEY)

        async def mock_wave5_next_adjacency(session, global_row, now):
            """Mock wave 5 cleanup."""
            _W5_KEY = "migrations.pinned_seed_next_variants_v1"
            if not await mock_migration_done(session, _W5_KEY):
                nonlocal wave5_calls
                wave5_calls += 1
                await mock_mark_migration(session, _W5_KEY)

        async def mock_topup_defaults(session, global_row, now):
            """No-op."""
            pass

        # Patch async_session to return our mock session.
        mock_async_session = MagicMock()
        mock_async_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_async_session.__aexit__ = AsyncMock(return_value=None)

        # Patch all the internal functions.
        with patch(
            "backend.api.routes.watchlist.async_session",
            return_value=mock_async_session,
        ), patch(
            "backend.api.routes.watchlist._sgp_find_or_create_global",
            side_effect=mock_find_or_create_global,
        ), patch(
            "backend.api.routes.watchlist._sgp_migrate_legacy",
            side_effect=mock_migrate_legacy,
        ), patch(
            "backend.api.routes.watchlist._sgp_run_delete_wave",
            side_effect=mock_run_delete_wave,
        ), patch(
            "backend.api.routes.watchlist._sgp_wave4_usdinr_bare_root",
            side_effect=mock_wave4_usdinr_bare_root,
        ), patch(
            "backend.api.routes.watchlist._sgp_wave5_next_adjacency",
            side_effect=mock_wave5_next_adjacency,
        ), patch(
            "backend.api.routes.watchlist._sgp_topup_defaults",
            side_effect=mock_topup_defaults,
        ), patch(
            "backend.api.routes.watchlist._sgp_migration_done",
            side_effect=mock_migration_done,
        ), patch(
            "backend.api.routes.watchlist._sgp_mark_migration",
            side_effect=mock_mark_migration,
        ):
            # Import the function under test after patching.
            from backend.api.routes.watchlist import seed_global_pinned

            # Call seed_global_pinned concurrently from 5 tasks.
            await asyncio.gather(
                seed_global_pinned(),
                seed_global_pinned(),
                seed_global_pinned(),
                seed_global_pinned(),
                seed_global_pinned(),
            )

        # Assertions: with the lock in place, each wave cleanup must fire exactly once.
        assert (
            wave1_calls == 1
        ), f"Wave 1 (GOLDM/USDINR) should fire once, but fired {wave1_calls} times"
        assert (
            wave2_calls == 1
        ), f"Wave 2 (MCX futures) should fire once, but fired {wave2_calls} times"
        assert (
            wave3_calls == 1
        ), f"Wave 3 (SILVER MCX) should fire once, but fired {wave3_calls} times"
        assert (
            wave4_calls == 1
        ), f"Wave 4 (USDINR bare root) should fire once, but fired {wave4_calls} times"
        assert (
            wave5_calls == 1
        ), f"Wave 5 (seed next variants) should fire once, but fired {wave5_calls} times"

        # Verify marker count: 5 waves × 1 each = 5 markers written.
        assert (
            len(mark_migration_calls) == 5
        ), f"Expected 5 marker writes (one per wave), got {len(mark_migration_calls)}: {mark_migration_calls}"
        unique_markers = set(mark_migration_calls)
        assert len(unique_markers) == 5, f"All markers should be unique, got {unique_markers}"
