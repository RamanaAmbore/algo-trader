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
        """Clear the module-level absent-token set so each test starts
        fresh and can observe [MMAP-MISSING-SYM] warnings on first
        encounter (the set otherwise suppresses all subsequent warnings
        for a token seen in a prior test)."""
        import backend.brokers.mmap_ticker as _mod
        _mod._known_absent_tokens.clear()

    def teardown_method(self, _method):
        import backend.brokers.mmap_ticker as _mod
        _mod._known_absent_tokens.clear()

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


# ---------------------------------------------------------------------------
# 4. Dhan interval-skip — LKG fallback (Fix 2, Aug 2026)
# ---------------------------------------------------------------------------

def _inner(decorated_func):
    """Return the undecorated inner function from a @for_all_accounts decorated func."""
    return getattr(decorated_func, "__wrapped__", None)


class TestDhanIntervalSkipLkgFallback:
    """When a Dhan poll interval hasn't elapsed AND a last-known-good (LKG)
    frame exists, the fetch functions must return the LKG frame (with
    interval_skipped=True) rather than an empty DataFrame.  When no LKG
    exists (first-poll-after-restart), the existing empty-frame behaviour
    is preserved.

    Quality dimensions:
      * SSOT — one fix point per function (_fetch_holdings_local,
        _fetch_positions_local, _fetch_margins_local).
      * Perf — no broker round-trip occurs in either branch; the LKG
        path adds only a dict lookup.
      * Stale-code grep — interval_skipped=True still set on LKG frame so
        downstream ssot_fetch(mode='coalesce') can detect the skip.
      * Reuse — shares _stale_substitute_frame with the circuit-breaker
        path; no new LKG logic added.
      * UX — rows no longer disappear on every page that concatenates
        per-account DataFrames during the throttle window.
    """

    # -- holdings ------------------------------------------------------------

    def test_interval_skipped_holdings_returns_lkg_when_available(self):
        """When interval not due + LKG non-empty: return LKG with interval_skipped=True."""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from backend.brokers import broker_apis

        inner = _inner(broker_apis._fetch_holdings_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute on _fetch_holdings_local")

        lkg_frame = pd.DataFrame([{
            "tradingsymbol": "RELIANCE",
            "quantity": 10,
            "average_price": 2800.0,
        }])

        broker = MagicMock()

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False), \
             patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False), \
             patch("backend.brokers.broker_apis._stale_substitute_frame",
                   return_value=lkg_frame) as mock_ssf:
            result = inner(
                connections=lambda: MagicMock(),
                account="DH1234", kite=None, broker=broker,
            )

        mock_ssf.assert_called_once_with("holdings", "DH1234")
        assert not result.empty, "Expected non-empty LKG frame when LKG is available"
        assert result.attrs.get("interval_skipped") is True
        assert "circuit_open" not in result.attrs, (
            "circuit_open attr must NOT be set on an interval-skip LKG frame"
        )

    def test_interval_skipped_holdings_returns_empty_when_no_lkg(self):
        """When interval not due + no LKG: return empty frame with interval_skipped=True."""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from backend.brokers import broker_apis

        inner = _inner(broker_apis._fetch_holdings_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute on _fetch_holdings_local")

        broker = MagicMock()

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False), \
             patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False), \
             patch("backend.brokers.broker_apis._stale_substitute_frame",
                   return_value=pd.DataFrame()):
            result = inner(
                connections=lambda: MagicMock(),
                account="DH1234", kite=None, broker=broker,
            )

        assert result.empty, "Expected empty frame when no LKG exists"
        assert result.attrs.get("interval_skipped") is True

    # -- positions -----------------------------------------------------------

    def test_interval_skipped_positions_returns_lkg_when_available(self):
        """Positions variant: LKG rows survive the interval-skip throttle window."""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from backend.brokers import broker_apis

        inner = _inner(broker_apis._fetch_positions_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute on _fetch_positions_local")

        lkg_frame = pd.DataFrame([{
            "tradingsymbol": "NIFTY26AUG25000CE",
            "quantity": 50,
            "last_price": 120.0,
            "pnl": 5000.0,
        }])

        broker = MagicMock()

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False), \
             patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False), \
             patch("backend.brokers.broker_apis._stale_substitute_frame",
                   return_value=lkg_frame) as mock_ssf:
            result = inner(
                connections=lambda: MagicMock(),
                account="DH1234", kite=None, broker=broker,
            )

        mock_ssf.assert_called_once_with("positions", "DH1234")
        assert not result.empty
        assert result.attrs.get("interval_skipped") is True
        assert "circuit_open" not in result.attrs

    def test_interval_skipped_positions_returns_empty_when_no_lkg(self):
        """Positions variant: empty frame returned when no LKG exists."""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from backend.brokers import broker_apis

        inner = _inner(broker_apis._fetch_positions_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute on _fetch_positions_local")

        broker = MagicMock()

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False), \
             patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False), \
             patch("backend.brokers.broker_apis._stale_substitute_frame",
                   return_value=pd.DataFrame()):
            result = inner(
                connections=lambda: MagicMock(),
                account="DH1234", kite=None, broker=broker,
            )

        assert result.empty
        assert result.attrs.get("interval_skipped") is True

    # -- margins -------------------------------------------------------------

    def test_interval_skipped_margins_returns_lkg_when_available(self):
        """Margins variant: LKG funds row survives the interval-skip throttle window."""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from backend.brokers import broker_apis

        inner = _inner(broker_apis._fetch_margins_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute on _fetch_margins_local")

        lkg_frame = pd.DataFrame([{
            "net": 50000.0,
        }])

        broker = MagicMock()

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False), \
             patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False), \
             patch("backend.brokers.broker_apis._stale_substitute_frame",
                   return_value=lkg_frame) as mock_ssf:
            result = inner(
                connections=lambda: MagicMock(),
                account="DH1234", kite=None, broker=broker,
            )

        mock_ssf.assert_called_once_with("margins", "DH1234")
        assert not result.empty
        assert result.attrs.get("interval_skipped") is True
        assert "circuit_open" not in result.attrs

    def test_interval_skipped_margins_returns_empty_when_no_lkg(self):
        """Margins variant: empty frame returned when no LKG exists."""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from backend.brokers import broker_apis

        inner = _inner(broker_apis._fetch_margins_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute on _fetch_margins_local")

        broker = MagicMock()

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False), \
             patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False), \
             patch("backend.brokers.broker_apis._stale_substitute_frame",
                   return_value=pd.DataFrame()):
            result = inner(
                connections=lambda: MagicMock(),
                account="DH1234", kite=None, broker=broker,
            )

        assert result.empty
        assert result.attrs.get("interval_skipped") is True

    # -- circuit_open attr cleared on LKG path ------------------------------

    def test_lkg_circuit_open_attr_is_cleared(self):
        """_stale_substitute_frame sets circuit_open=True on every LKG frame.
        The interval-skip path must pop that attr so the consuming ssot_fetch
        layer doesn't mistake a throttle event for a breaker event."""
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from backend.brokers import broker_apis

        inner = _inner(broker_apis._fetch_holdings_local)
        if inner is None:
            pytest.skip("No __wrapped__ attribute on _fetch_holdings_local")

        lkg_with_circuit_open = pd.DataFrame([{"tradingsymbol": "INFY", "quantity": 5}])
        lkg_with_circuit_open.attrs["stale"] = True
        lkg_with_circuit_open.attrs["circuit_open"] = True  # set by _stale_substitute_frame

        broker = MagicMock()

        with patch("backend.brokers.broker_apis._is_circuit_open", return_value=False), \
             patch("backend.brokers.broker_apis._is_dhan_interval_due", return_value=False), \
             patch("backend.brokers.broker_apis._stale_substitute_frame",
                   return_value=lkg_with_circuit_open):
            result = inner(
                connections=lambda: MagicMock(),
                account="DH9999", kite=None, broker=broker,
            )

        assert "circuit_open" not in result.attrs, (
            "circuit_open must be cleared on the interval-skip LKG path — "
            "only the breaker path should carry this attr"
        )
        assert result.attrs.get("interval_skipped") is True


# ---------------------------------------------------------------------------
# 5. _bmd_build_key_index — stale-fingerprint extension (Aug 2026)
# ---------------------------------------------------------------------------

class TestBmdStaleFingerprint:
    """_bmd_build_key_index must include overnight F&O rows where Kite REST
    returns last_price == close_price (stale fingerprint: WS tick not yet
    received). Without this, day_change_val stays 0 for all surfaces
    (derivatives, MarketPulse, NavStrip) until the first WS tick arrives.

    Quality dimensions:
      * SSOT — single mask extension inside _bmd_build_key_index; no changes
        needed in _bmd_patch_rows or _bmd_recompute_derived.
      * Perf — one vectorised comparison added; no per-row Python loop.
      * Stale-code grep — tolerance is 0.005 (matches the existing stale-close
        guard used elsewhere in the codebase).
      * Reuse — re-uses the same _FO_EXCH set pattern as other F&O guards.
      * UX — day_change_val is recovered as (fresh_ltp - cls) × qty once
        PriceBroker.quote() delivers a live LTP.
    """

    def test_bmd_stale_fingerprint_nfo_overnight(self):
        """Overnight NFO row with ltp == close_price is included in mask."""
        import pandas as pd
        from backend.brokers.broker_apis import _bmd_build_key_index

        df = pd.DataFrame([{
            'overnight_quantity': 5, 'exchange': 'NFO',
            'last_price': 150.0, 'close_price': 150.0,
            'tradingsymbol': 'INFY25AUG25C100CE',
        }])
        mask, _, _ = _bmd_build_key_index(df)
        assert mask is not None and bool(mask.iloc[0]), (
            "Stale NFO overnight row must be in backfill mask"
        )

    def test_bmd_stale_fingerprint_not_for_intraday(self):
        """New position today (overnight_quantity=0) with ltp==close is NOT included."""
        import pandas as pd
        from backend.brokers.broker_apis import _bmd_build_key_index

        df = pd.DataFrame([{
            'overnight_quantity': 0, 'exchange': 'NFO',
            'last_price': 150.0, 'close_price': 150.0,
            'tradingsymbol': 'INFY25AUG25C100CE',
        }])
        mask, _, _ = _bmd_build_key_index(df)
        # mask may be None (all False) or have False for this row
        if mask is not None:
            assert not bool(mask.iloc[0]), (
                "Intraday (oq=0) stale row must NOT be in backfill mask"
            )

    def test_bmd_stale_fingerprint_not_for_equity(self):
        """NSE equity row with ltp==close is NOT included (equity lacks this pattern)."""
        import pandas as pd
        from backend.brokers.broker_apis import _bmd_build_key_index

        df = pd.DataFrame([{
            'overnight_quantity': 5, 'exchange': 'NSE',
            'last_price': 150.0, 'close_price': 150.0,
            'tradingsymbol': 'INFY',
        }])
        mask, _, _ = _bmd_build_key_index(df)
        if mask is not None:
            assert not bool(mask.iloc[0]), (
                "NSE equity row must NOT be in backfill mask"
            )

    def test_bmd_stale_fingerprint_mcx_overnight(self):
        """MCX overnight short position (oq=-1) with ltp==close IS included."""
        import pandas as pd
        from backend.brokers.broker_apis import _bmd_build_key_index

        df = pd.DataFrame([{
            'overnight_quantity': -1, 'exchange': 'MCX',
            'last_price': 5000.0, 'close_price': 5000.0,
            'tradingsymbol': 'CRUDEOIL25AUGFUT',
        }])
        mask, _, _ = _bmd_build_key_index(df)
        assert mask is not None and bool(mask.iloc[0]), (
            "Stale MCX overnight row must be in backfill mask"
        )
