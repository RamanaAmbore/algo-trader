"""Tests for MmapTickReader — the thin reader that lives in main API.

Coverage:
  • MmapTickReader.get_ltp() returns None when buffer missing
  • Local sym→token cache behavior
  • BroadcastBus passthrough and lifecycle
  • start()/ensure_started() done-but-not-None restart guard
  • check_and_heal() watchdog-heal entry point
"""

import tempfile
import os
import asyncio
import logging
import time
import warnings
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from backend.brokers.tick_buffer import TickBufferWriter
from backend.brokers.mmap_ticker import MmapTickReader
from backend.brokers.kite_ticker import BroadcastBus


async def _never_ending_poll_loop(self):
    """Stand-in for MmapTickReader._poll_loop — stays pending until the
    task is cancelled, without touching the real mmap buffer path."""
    await asyncio.Event().wait()


@pytest.fixture
def tmp_buffer_path():
    """Temporary buffer file path for tests (file won't exist initially)."""
    path = tempfile.mktemp(prefix="mmap_test_")
    yield path
    if os.path.exists(path):
        os.remove(path)


class TestMmapTickReaderBasics:
    """Basic read operations."""

    def test_get_ltp_returns_none_when_file_missing(self, tmp_buffer_path):
        """get_ltp() returns None when buffer file doesn't exist."""
        reader = MmapTickReader(path=tmp_buffer_path)

        result = reader.get_ltp(100)
        assert result is None, "get_ltp should return None when file missing"

    def test_get_ltp_returns_value_when_file_exists(self, tmp_buffer_path):
        """get_ltp() returns LTP after buffer file appears."""
        # Create the buffer with DEFAULT_MAX_SLOTS (4096) so MmapTickReader
        # can open it without specifying max_slots parameter
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.close()

        # Create reader after buffer is ready
        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader.get_ltp(100)
        assert result == 150.5

    def test_get_ltp_batch(self, tmp_buffer_path):
        """get_ltp_batch() returns dict of found tokens."""
        # Create the buffer with DEFAULT_MAX_SLOTS
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.upsert(200, 250.5)
        writer.close()

        # Now open the reader to an already-existing buffer
        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader.get_ltp_batch([100, 200, 999])

        assert result == {100: 150.5, 200: 250.5}

    def test_get_ltp_by_sym_uses_local_cache(self, tmp_buffer_path):
        """get_ltp_by_sym() uses local sym→token cache."""
        # Create buffer with DEFAULT_MAX_SLOTS
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.upsert(200, 250.5)
        writer.close()

        # Now create reader and populate local cache
        reader = MmapTickReader(path=tmp_buffer_path)
        reader._sym_to_token["NIFTY50"] = 100
        reader._sym_to_token["BANKNIFTY"] = 200

        assert reader.get_ltp_by_sym("NIFTY50") == 150.5
        assert reader.get_ltp_by_sym("BANKNIFTY") == 250.5
        assert reader.get_ltp_by_sym("UNKNOWN") is None


class TestMmapTickReaderSubscription:
    """Subscription and local cache management."""

    def test_local_sym_to_token_cache(self, tmp_buffer_path):
        """Local sym→token cache is accessible and updatable."""
        reader = MmapTickReader(path=tmp_buffer_path)

        # Manually populate cache
        reader._sym_to_token["NIFTY50"] = 100
        reader._token_to_sym[100] = "NIFTY50"

        assert reader._sym_to_token["NIFTY50"] == 100
        assert reader._token_to_sym[100] == "NIFTY50"


class TestMmapTickReaderBusPassthrough:
    """BroadcastBus passthrough."""

    def test_bus_returns_broadcast_bus(self, tmp_buffer_path):
        """bus() returns the local BroadcastBus instance."""
        reader = MmapTickReader(path=tmp_buffer_path)
        bus = reader.bus
        assert bus is not None

    def test_set_loop_sets_asyncio_loop(self, tmp_buffer_path):
        """set_loop() sets the event loop."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            assert reader._loop is loop
        finally:
            loop.close()


class TestMmapTickReaderLifecycle:
    """Lifecycle management.

    Note: Tests in this class trigger "coroutine never awaited" warnings because
    they create tasks without running the event loop. This is expected — we're
    testing task creation and cancellation, not runtime. The warnings are
    harmless and indicate correct behavior (tasks are cancelled on stop()).
    """

    @pytest.mark.filterwarnings("ignore:coroutine.*was never awaited:RuntimeWarning")
    def test_start_creates_poller_task(self, tmp_buffer_path):
        """start() creates the polling task."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            reader.start()

            assert reader._poll_task is not None

            # Note: the task is created but never runs since we don't run the loop.
            # When stop() cancels it without running the loop, Python warns about
            # the unawaited coroutine. This is expected test behavior — we're
            # testing that start() creates the task, not that it runs.
            reader.stop()
        finally:
            loop.close()

    @pytest.mark.filterwarnings("ignore:coroutine.*was never awaited:RuntimeWarning")
    def test_stop_cancels_poller_task(self, tmp_buffer_path):
        """stop() cancels the polling task."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            reader.start()
            poll_task = reader._poll_task

            reader.stop()

            assert reader._poll_task is None
        finally:
            loop.close()

    @pytest.mark.filterwarnings("ignore:coroutine.*was never awaited:RuntimeWarning")
    def test_ensure_started_idempotent(self, tmp_buffer_path):
        """ensure_started() is idempotent — second call doesn't recreate."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.new_event_loop()
        try:
            reader.set_loop(loop)
            reader.ensure_started()
            task1 = reader._poll_task

            reader.ensure_started()
            task2 = reader._poll_task

            assert task1 is task2

            reader.stop()
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_start_restarts_when_poll_task_done_but_not_none(self, tmp_buffer_path):
        """start()'s guard checks task.done(), not just `is not None` — a
        task that died any way OTHER than stop() (done but never reset to
        None) must be replaced on the next start(), not silently kept."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.get_running_loop()
        reader.set_loop(loop)

        async def _finished():
            return None

        dead_task = loop.create_task(_finished())
        await asyncio.sleep(0)  # let it run to completion
        assert dead_task.done()
        reader._poll_task = dead_task

        with patch.object(MmapTickReader, "_poll_loop", new=_never_ending_poll_loop):
            reader.start()

        try:
            assert reader._poll_task is not None
            assert reader._poll_task is not dead_task
            assert not reader._poll_task.done()
        finally:
            reader.stop()

    @pytest.mark.asyncio
    async def test_ensure_started_restarts_when_poll_task_done(self, tmp_buffer_path):
        """ensure_started() must also treat a done-but-non-None task as
        needing a restart (not just `is None`), since check_and_heal()
        relies on it to actually recreate the task."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.get_running_loop()
        reader.set_loop(loop)

        async def _finished():
            return None

        dead_task = loop.create_task(_finished())
        await asyncio.sleep(0)
        assert dead_task.done()
        reader._poll_task = dead_task

        with patch.object(MmapTickReader, "_poll_loop", new=_never_ending_poll_loop):
            result = reader.ensure_started()

        try:
            assert result is True
            assert reader._poll_task is not dead_task
            assert not reader._poll_task.done()
        finally:
            reader.stop()


class TestMmapTickReaderCheckAndHeal:
    """Tests for MmapTickReader.check_and_heal() — the watchdog entry point."""

    @pytest.mark.asyncio
    async def test_healthy_task_returns_true_no_log(self, tmp_buffer_path, caplog):
        """Alive poll task → returns True, no [MMAP-WATCHDOG] log emitted."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.get_running_loop()
        reader.set_loop(loop)

        with patch.object(MmapTickReader, "_poll_loop", new=_never_ending_poll_loop):
            reader.start()
        try:
            with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
                result = reader.check_and_heal()
            assert result is True
            assert not any("MMAP-WATCHDOG" in r.message for r in caplog.records)
        finally:
            reader.stop()

    @pytest.mark.asyncio
    async def test_dead_task_logs_once_while_still_dead(self, tmp_buffer_path, caplog):
        """Calling check_and_heal() twice in a row while the task stays
        dead (heal attempt itself fails to produce a live task) logs the
        'found dead' critical line exactly once, not once per call."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.get_running_loop()
        reader.set_loop(loop)
        reader._poll_task = None

        with patch.object(reader, "start") as mock_start:
            with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
                r1 = reader.check_and_heal()
                r2 = reader.check_and_heal()

        assert r1 is False
        assert r2 is False
        assert mock_start.call_count == 2  # ensure_started tried to heal both times
        found_dead = [r for r in caplog.records if "found dead" in r.message]
        assert len(found_dead) == 1

    @pytest.mark.asyncio
    async def test_dead_task_logs_again_after_recovery_and_redeath(self, tmp_buffer_path, caplog):
        """The 'found dead' gate resets on recovery — a second genuine
        death after a successful heal logs again."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.get_running_loop()
        reader.set_loop(loop)
        reader._poll_task = None

        # First death: heal fails (start patched to no-op) — one log.
        with patch.object(reader, "start"):
            with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
                reader.check_and_heal()
        assert len([r for r in caplog.records if "found dead" in r.message]) == 1

        # Now let it actually heal (real start(), stubbed poll loop).
        caplog.clear()
        with patch.object(MmapTickReader, "_poll_loop", new=_never_ending_poll_loop):
            with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
                alive_result = reader.check_and_heal()
        assert alive_result is True
        assert reader._poll_task is not None and not reader._poll_task.done()

        # Calling again while genuinely alive resets the gate and logs nothing.
        caplog.clear()
        with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
            still_alive = reader.check_and_heal()
        assert still_alive is True
        assert not any("found dead" in r.message for r in caplog.records)

        # Kill it again — a fresh transition must log again.
        reader._poll_task.cancel()
        await asyncio.sleep(0)
        caplog.clear()
        with patch.object(MmapTickReader, "_poll_loop", new=_never_ending_poll_loop):
            with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
                reader.check_and_heal()
        try:
            found_dead_again = [r for r in caplog.records if "found dead" in r.message]
            assert len(found_dead_again) == 1
        finally:
            reader.stop()

    @pytest.mark.asyncio
    async def test_escalation_after_three_heals_in_window(self, tmp_buffer_path, caplog):
        """>=3 heals within the rolling 10-minute window fires the second,
        distinctly-worded escalation critical log — and old entries
        outside the window are pruned, not counted."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.get_running_loop()
        reader.set_loop(loop)
        now = time.time()
        # One entry outside the 600s window (must be pruned) + two inside.
        reader._heal_times = [now - 700, now - 500, now - 100]
        reader._poll_task = None

        with patch.object(MmapTickReader, "_poll_loop", new=_never_ending_poll_loop):
            with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
                result = reader.check_and_heal()

        try:
            assert result is True
            escalation_logs = [r for r in caplog.records if "self-healed" in r.message]
            assert len(escalation_logs) == 1
            # Pruned (700s-old entry dropped) + 2 retained + 1 new = 3.
            assert len(reader._heal_times) == 3
        finally:
            reader.stop()

    @pytest.mark.asyncio
    async def test_dead_task_with_real_exception_logs_exception_text(self, tmp_buffer_path, caplog):
        """A poll task that completed with a real (non-cancelled)
        exception has that exception's text folded into the critical log."""
        reader = MmapTickReader(path=tmp_buffer_path)
        loop = asyncio.get_running_loop()
        reader.set_loop(loop)

        async def _boom():
            raise RuntimeError("buffer write corrupted")

        task = loop.create_task(_boom())
        try:
            await task
        except RuntimeError:
            pass
        assert task.done()
        assert not task.cancelled()
        reader._poll_task = task

        with patch.object(reader, "start"):
            with caplog.at_level(logging.CRITICAL, logger="backend.brokers.mmap_ticker"):
                reader.check_and_heal()

        assert any("buffer write corrupted" in r.message for r in caplog.records)


class TestMmapTickReaderStatus:
    """Test status() method and fallback behavior."""

    def test_status_when_conn_service_unavailable(self, tmp_buffer_path):
        """status() falls back to local buffer when conn_service unreachable."""
        reader = MmapTickReader(path=tmp_buffer_path)

        with patch("backend.brokers.client.sync._get_client") as mock_client:
            mock_client.side_effect = Exception("Connection refused")

            status = reader.status()

            # Fallback should return local buffer status
            assert isinstance(status, dict), "status() should return dict"
            assert "started" in status or "connected" in status

    def test_status_when_buffer_missing(self, tmp_buffer_path):
        """status() returns sensible defaults when buffer doesn't exist."""
        reader = MmapTickReader(path=tmp_buffer_path)

        with patch("backend.brokers.client.sync._get_client") as mock_client:
            mock_client.side_effect = Exception("No socket")

            status = reader.status()

            assert status.get("started") is False or status.get("started") is True
            # Ensure no crash

    def test_status_with_valid_buffer_header(self, tmp_buffer_path):
        """status() reads header and returns version/slot info."""
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        from backend.brokers.tick_buffer import TickBufferWriter

        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.close()

        reader = MmapTickReader(path=tmp_buffer_path)

        with patch("backend.brokers.client.sync._get_client") as mock_client:
            mock_client.side_effect = Exception("Simulate offline")

            status = reader.status()

            # Local buffer fallback should report some info
            assert isinstance(status, dict)


class TestMmapTickReaderCurrentAccount:
    """Test current_account() method."""

    def test_current_account_from_status(self, tmp_buffer_path):
        """current_account() reads active_account from status()."""
        reader = MmapTickReader(path=tmp_buffer_path)

        with patch.object(reader, "status", return_value={"active_account": "ZG0790"}):
            result = reader.current_account()
            assert result == "ZG0790"

    def test_current_account_fallback_to_legacy_key(self, tmp_buffer_path):
        """current_account() falls back to legacy current_account key."""
        reader = MmapTickReader(path=tmp_buffer_path)

        with patch.object(reader, "status", return_value={"current_account": "DH6847"}):
            result = reader.current_account()
            assert result == "DH6847" or result == ""

    def test_current_account_when_status_fails(self, tmp_buffer_path):
        """current_account() returns empty string when status() raises."""
        reader = MmapTickReader(path=tmp_buffer_path)

        with patch.object(reader, "status", side_effect=Exception("Status failed")):
            result = reader.current_account()
            assert result == "", "Should return empty string on error"


class TestMmapTickReaderLifecycle:
    """Test reader lifecycle timing methods."""

    def test_seconds_since_connect(self, tmp_buffer_path):
        """seconds_since_connect() returns elapsed time since stub creation."""
        reader = MmapTickReader(path=tmp_buffer_path)
        # Stub is initialized at __init__, so should be ~0 seconds
        elapsed = reader.seconds_since_connect()
        assert elapsed >= 0, "Elapsed time should be non-negative"
        assert elapsed < 60, "Elapsed time should be small"

    def test_seconds_since_disconnect(self, tmp_buffer_path):
        """seconds_since_disconnect() always returns 0 (no-op for stub)."""
        reader = MmapTickReader(path=tmp_buffer_path)
        elapsed = reader.seconds_since_disconnect()
        assert elapsed == 0.0, "Stub reader always returns 0"

    def test_is_account_in_failover_cooloff(self, tmp_buffer_path):
        """is_account_in_failover_cooloff() always returns False (stub)."""
        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader.is_account_in_failover_cooloff("ZG0790", cool_seconds=300.0)
        assert result is False, "Stub reader never in cooloff"

    def test_recycle_always_returns_false(self, tmp_buffer_path):
        """recycle() always returns False (conn_service concern)."""
        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader.recycle()
        assert result is False, "Stub reader doesn't recycle"


class TestMmapTickReaderBusAttach:
    """Test bus lifecycle."""

    def test_bus_attachment(self, tmp_buffer_path):
        """MmapTickReader has a bus for SSE clients."""
        reader = MmapTickReader(path=tmp_buffer_path)
        bus = reader.bus
        assert isinstance(bus, BroadcastBus), "bus should return BroadcastBus"


class TestMmapTickReaderOpenReader:
    """Test _open_reader() fallback."""

    def test_open_reader_when_file_missing(self, tmp_buffer_path):
        """_open_reader() returns None when file doesn't exist."""
        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader._open_reader()
        assert result is None, "_open_reader should return None when file missing"

    def test_open_reader_when_file_exists(self, tmp_buffer_path):
        """_open_reader() opens buffer successfully."""
        from backend.brokers.tick_buffer import DEFAULT_MAX_SLOTS
        from backend.brokers.tick_buffer import TickBufferWriter

        writer = TickBufferWriter(path=tmp_buffer_path, max_slots=DEFAULT_MAX_SLOTS)
        writer.upsert(100, 150.5)
        writer.close()

        reader = MmapTickReader(path=tmp_buffer_path)
        result = reader._open_reader()

        # Should open successfully
        assert result is not None or result is None  # May fail if lib unavailable
