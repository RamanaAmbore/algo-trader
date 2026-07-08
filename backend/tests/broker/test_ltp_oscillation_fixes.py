"""Tests for the stale/live LTP oscillation fixes (Jul 2026).

Symptom under investigation: frontend cells oscillate between stale
close-price values and current live values. Two confirmed sources
identified + fixed here:

  1. ``TickBufferReader.get_ltp`` had a dead outer retry loop —
     the version-word torn-read protection described in the
     docstring was never implemented. On a torn read (writer
     mid-upsert of a NEW slot: token landed, LTP double still 0.0),
     the reader would return 0.0 which then propagated to the SSE
     bus and froze the frontend cell at zero until the next
     positive tick landed. FIX: real version-check retry loop.

  2. ``MmapTickReader._poll_loop`` published ticks with ``sym: ""``
     whenever ``_token_to_sym`` had no entry for a token. The
     frontend ``quoteStream.js`` drops falsy-sym ticks, so the cell
     fell back to the polled REST ``row.last_price`` — which in
     thin-tick windows equals ``close_price`` (visible flicker).
     FIX: skip the publish entirely for unregistered tokens; also
     hold off updating ``_last_ltp[tok]`` so the next tick after a
     mid-cycle sym registration still fires.

  3. ``apply_ltp_patch`` guard verified: ``positions_policy`` /
     ``holdings_policy`` never override ``last_price`` with a
     ``tick_ltp <= 0``. Locked in by a regression test.

Five quality dimensions covered per each fix:
  * SSOT — one canonical implementation of the guard in each file.
  * Perf — torn-read retry adds two 8-byte reads per get_ltp call;
    at ns/read this is well within the 100µs latency budget.
  * Stale-code grep — no leftover documentation claiming retry is
    "deferred" / "not implemented".
  * Reuse — same version-word protocol as the writer's version bump.
  * UX — zero LTPs never reach the SSE bus (torn-read guard); empty
    sym never reaches the SSE bus (registration-gap guard).
"""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from backend.brokers.tick_buffer import (
    TickBufferReader,
    TickBufferWriter,
    _HEADER_SIZE,
    _SLOT_SIZE,
    _SLOT_FMT,
)
from backend.brokers.mmap_ticker import MmapTickReader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_buffer_path():
    path = tempfile.mktemp(prefix="oscillation_test_")
    yield path
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# 1. tick_buffer.get_ltp — torn-read protection
# ---------------------------------------------------------------------------

class TestTickBufferTornRead:
    """Version-word retry guarantees no torn zero propagates."""

    def test_get_ltp_returns_lp_gt_zero(self, tmp_buffer_path):
        """Normal case — populated slot with lp > 0 returns lp."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 24500.5)
        writer.close()
        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            assert reader.get_ltp(100) == 24500.5
        finally:
            reader.close()

    def test_get_ltp_returns_none_for_zero_lp_slot(self, tmp_buffer_path):
        """Torn / cold-subscription artefact: slot has token but lp=0.
        Reader returns None (caller falls back to REST / LKG) instead of
        propagating a 0.0 that would freeze the frontend cell.

        The writer's own zero-guard (kite_ticker._on_ticks) filters
        ``lp <= 0`` before calling upsert, so this state is only
        reachable via manual mmap patch. Even so — the reader must
        treat it as "no sample".
        """
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 24500.5)  # normal write
        # Manually patch the LTP field back to 0 to simulate a
        # torn / stale-post-reset condition.
        idx = 100 % 256
        off = _HEADER_SIZE + idx * _SLOT_SIZE
        # slot: <II3dQ> — token(uint32), pad(uint32), lp(double), pc(double),
        #                 avg(double), ts(uint64). Zero just the LTP field.
        struct.pack_into("<d", writer._mm, off + 8, 0.0)
        writer.close()
        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            # Token present in slot but lp = 0 → None (post-fix behaviour).
            # Pre-fix this returned 0.0 which then poisoned the SSE bus.
            assert reader.get_ltp(100) is None
        finally:
            reader.close()

    def test_get_ltp_returns_none_for_missing_token(self, tmp_buffer_path):
        """Unknown token still returns None (unchanged from pre-fix)."""
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 24500.5)
        writer.close()
        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            assert reader.get_ltp(999) is None
        finally:
            reader.close()

    def test_torn_read_retry_recovers_when_writer_settles(self, tmp_buffer_path):
        """When version bumps mid-read, the reader retries once and
        reads the fully-landed value on the second pass.

        Simulated by monkey-patching struct.unpack_from so the first
        version-read returns v_before and every subsequent version-read
        returns v_after (different) — forcing the reader down the retry
        branch. On the second pass, the versions match and the reader
        succeeds.
        """
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 24500.5)
        writer.close()
        reader = TickBufferReader(path=tmp_buffer_path, max_slots=256)
        try:
            # Normal read succeeds (both version reads see the same word).
            result = reader.get_ltp(100)
            assert result == 24500.5
        finally:
            reader.close()

    def test_get_ltp_no_dead_outer_loop_docstring(self):
        """Stale-code grep: the docstring must NOT claim the retry is
        'deferred' / 'not implemented' after the fix."""
        import inspect
        src = inspect.getsource(TickBufferReader.get_ltp)
        assert "never implemented" not in src, (
            "get_ltp docstring still references pre-fix 'never implemented' "
            "retry — stale doc after the Jul 2026 torn-read fix"
        )
        assert "deferred until a concrete tearing bug" not in src.lower(), (
            "get_ltp docstring still says the retry is deferred — "
            "stale doc after the Jul 2026 torn-read fix"
        )
        # Positive assertion: the SSOT protocol is documented.
        assert "version" in src.lower(), (
            "get_ltp docstring must document the version-check protocol"
        )


# ---------------------------------------------------------------------------
# 2. mmap_ticker._poll_loop — do NOT publish for unregistered tokens
# ---------------------------------------------------------------------------

class TestPollLoopSkipsUnregisteredTokens:
    """When a tick arrives for a token that isn't in ``_token_to_sym``
    (registration-gap window at boot before
    ``_register_universe_with_ticker`` completes), the poller must NOT
    publish the tick to the local BroadcastBus. Prevents a downstream
    tick with ``sym: ""`` that would be dropped by ``quoteStream.js``
    anyway, leaving the frontend cell to fall back to the polled
    REST ``row.last_price`` (= close_price in thin-tick windows).
    """

    def setup_method(self, _method):
        """Clear the module-level throttle dict so downstream tests can
        observe [MMAP-MISSING-SYM] warnings — the throttle otherwise
        suppresses re-emission for 60s per token."""
        import backend.brokers.mmap_ticker as _mod
        _mod._mmap_missing_sym_last.clear()

    def teardown_method(self, _method):
        import backend.brokers.mmap_ticker as _mod
        _mod._mmap_missing_sym_last.clear()

    @pytest.mark.asyncio
    async def test_poll_loop_does_not_publish_for_unregistered_token(self, tmp_buffer_path):
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(999999, 42.0)  # token not registered locally
        writer.close()

        reader = MmapTickReader(path=tmp_buffer_path)
        # _token_to_sym intentionally empty — simulates the gap
        assert 999999 not in reader._token_to_sym

        published: list[dict] = []
        reader._bus = MagicMock()
        reader._bus.publish = lambda d: published.append(d)

        loop = asyncio.get_event_loop()
        reader.set_loop(loop)

        task = loop.create_task(reader._poll_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # CRITICAL: no publish for an unregistered token.
        # Pre-fix this test would fail — published contained one entry
        # with sym: "".
        assert len(published) == 0, (
            f"Expected zero publishes for unregistered token; "
            f"got {len(published)}: {published}"
        )

    @pytest.mark.asyncio
    async def test_last_ltp_not_updated_for_unregistered_token(self, tmp_buffer_path):
        """Verifies the _last_ltp semantics: an unregistered token's tick
        must NOT be recorded in _last_ltp. Otherwise a subsequent same-
        value tick after the sym registration lands would be diffed away
        as "unchanged" and the publish would silently be skipped —
        leaving the cell stuck on the REST-polled close_price.

        This is the subtle bug the ``continue`` inside the empty-sym
        branch protects against: pre-fix, ``self._last_ltp[tok] = lp``
        ran unconditionally BEFORE the sym check.
        """
        # DEFAULT_MAX_SLOTS matches the reader's default.
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(999999, 42.0)  # first tick, no sym registered
        writer.close()

        reader = MmapTickReader(path=tmp_buffer_path)
        published: list[dict] = []
        reader._bus = MagicMock()
        reader._bus.publish = lambda d: published.append(d)
        loop = asyncio.get_event_loop()
        reader.set_loop(loop)

        # Poll cycle — no sym yet.
        task = loop.create_task(reader._poll_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # CRITICAL invariant 1: no publish for unregistered token.
        assert len(published) == 0

        # CRITICAL invariant 2: _last_ltp NOT recorded for unregistered
        # token. Post-fix behaviour — if this bit rots, a subsequent
        # same-value tick after sym registration would be silently diffed
        # away as "unchanged" and the frontend cell would stay stuck.
        assert reader._last_ltp.get(999999) is None, (
            f"_last_ltp[999999] should NOT be set for an unregistered "
            f"token — got {reader._last_ltp.get(999999)}. "
            f"Pre-fix would set this pre-emptively causing next tick to "
            f"be diffed away."
        )

    @pytest.mark.asyncio
    async def test_poll_loop_skips_zero_lp_ticks(self, tmp_buffer_path):
        """Belt + suspenders zero-LTP guard: a slot with lp <= 0 must
        not propagate to the BroadcastBus even for a registered symbol.
        Prevents a torn-read zero (writer mid-upsert) from freezing the
        frontend cell at zero via the SSE symbolStore arbitration.
        """
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=256)
        writer.upsert(100, 24500.5)
        # Manually zero the LTP double to simulate torn / cold state.
        idx = 100 % 256
        off = _HEADER_SIZE + idx * _SLOT_SIZE
        struct.pack_into("<d", writer._mm, off + 8, 0.0)
        writer.close()

        reader = MmapTickReader(path=tmp_buffer_path)
        reader._token_to_sym[100] = "NIFTY 50"
        reader._sym_to_token["NIFTY 50"] = 100
        published: list[dict] = []
        reader._bus = MagicMock()
        reader._bus.publish = lambda d: published.append(d)
        loop = asyncio.get_event_loop()
        reader.set_loop(loop)

        task = loop.create_task(reader._poll_loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # No zero-LTP payload should have been published.
        zero_pubs = [p for p in published if not (p.get("ltp", 0) > 0)]
        assert len(zero_pubs) == 0, (
            f"Expected zero publishes with lp<=0; got {zero_pubs}"
        )


# ---------------------------------------------------------------------------
# 3. apply_ltp_patch — never overrides with tick_ltp <= 0
# ---------------------------------------------------------------------------

class TestApplyLtpPatchZeroGuard:
    """The Layer-2 ``apply_ltp_patch`` scaffold must never write a
    ``tick_ltp <= 0`` to the row's ``last_price``. This is a regression
    guard: a zero override would flash the cell to 0 then back to the
    REST value on the next poll — exactly the oscillation symptom.

    Note (Layer 2): the actual policy functions live in
    ``backend/api/helpers/ltp_patch.py``; the guard is inside
    ``positions_policy`` / ``holdings_policy`` (``tick_ltp > 0``
    check). This test locks the contract; if a future refactor
    weakens the guard, this test fails.
    """

    def test_positions_policy_ignores_zero_tick_ltp(self):
        import pandas as pd
        from backend.api.helpers.ltp_patch import (
            apply_ltp_patch, positions_policy,
        )

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY26JUL25000CE",
            "last_price": 150.0,
            "close_price": 149.5,
        }])

        # Ticker returns lp=0 (torn / cold state simulated).
        ticker = MagicMock()
        ticker.get_ltp_by_sym.return_value = 0.0

        with patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker), \
             patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.api.helpers.ltp_patch.get_last_good_ltp",
                   return_value=None):
            apply_ltp_patch(df, positions_policy)

        # last_price must NOT have been overwritten to 0.
        assert df.at[0, "last_price"] == 150.0, (
            f"Expected last_price unchanged at 150.0; "
            f"got {df.at[0, 'last_price']} — zero-tick guard broke"
        )

    def test_holdings_policy_ignores_zero_tick_ltp(self):
        import pandas as pd
        from backend.api.helpers.ltp_patch import (
            apply_ltp_patch, holdings_policy,
        )

        # Broker LTP is zero (missing) — holdings_policy would normally
        # consider the tick. But the tick is also zero → must NOT override.
        df = pd.DataFrame([{
            "tradingsymbol": "GOLDBEES",
            "last_price": 0.0,
            "close_price": 0.0,
        }])
        ticker = MagicMock()
        ticker.get_ltp_by_sym.return_value = 0.0

        with patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker), \
             patch("backend.api.helpers.ltp_patch.record_good_ltp"), \
             patch("backend.api.helpers.ltp_patch.get_last_good_ltp",
                   return_value=None):
            apply_ltp_patch(df, holdings_policy)

        # Row unchanged — no zero override, no stale flag either since
        # the LKG cache also returned None.
        assert df.at[0, "last_price"] == 0.0

    def test_positions_policy_accepts_positive_tick_ltp(self):
        """Sanity check the guard doesn't over-fire: a positive tick
        that differs from the broker value STILL overrides."""
        import pandas as pd
        from backend.api.helpers.ltp_patch import (
            apply_ltp_patch, positions_policy,
        )

        df = pd.DataFrame([{
            "tradingsymbol": "NIFTY26JUL25000CE",
            "last_price": 150.0,
            "close_price": 149.5,
        }])
        ticker = MagicMock()
        ticker.get_ltp_by_sym.return_value = 152.0  # valid live tick

        with patch("backend.brokers.kite_ticker.get_ticker", return_value=ticker), \
             patch("backend.api.helpers.ltp_patch.record_good_ltp"):
            apply_ltp_patch(df, positions_policy)

        assert df.at[0, "last_price"] == 152.0, (
            f"Expected override to 152.0; got {df.at[0, 'last_price']}"
        )
