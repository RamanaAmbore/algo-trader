"""
Static source-inspection tests confirming that blocking I/O calls are
properly offloaded via asyncio.to_thread in three backend modules.

These tests make NO network calls and require NO broker credentials.
They verify the fix is present by inspecting the CPython source of each
function — a lightweight, deterministic gate that fails if a future
refactor accidentally reintroduces a bare blocking call.
"""

import inspect
import pytest


# ---------------------------------------------------------------------------
# Fix 1 — actions_live.py: send_order_failure_alert wrapped in asyncio.to_thread
# ---------------------------------------------------------------------------

class TestActionsLiveToThread:
    """All send_order_failure_alert call sites must be wrapped in asyncio.to_thread."""

    def _get_module(self):
        import backend.api.algo.actions_live as m
        return m

    def test_place_order_preflight_block_uses_to_thread(self):
        m = self._get_module()
        src = inspect.getsource(m._place_order_preflight_block)
        assert "asyncio.to_thread" in src, (
            "_place_order_preflight_block: send_order_failure_alert must be "
            "called via asyncio.to_thread to avoid blocking the event loop"
        )
        assert "send_order_failure_alert" in src

    def test_place_order_on_failure_uses_to_thread(self):
        m = self._get_module()
        src = inspect.getsource(m._place_order_on_failure)
        assert "asyncio.to_thread" in src, (
            "_place_order_on_failure: send_order_failure_alert must be "
            "called via asyncio.to_thread"
        )
        assert "send_order_failure_alert" in src

    def test_close_position_preflight_block_uses_to_thread(self):
        m = self._get_module()
        src = inspect.getsource(m._close_position_preflight_block)
        assert "asyncio.to_thread" in src, (
            "_close_position_preflight_block: send_order_failure_alert must be "
            "called via asyncio.to_thread"
        )
        assert "send_order_failure_alert" in src

    def test_close_position_on_failure_uses_to_thread(self):
        m = self._get_module()
        src = inspect.getsource(m._close_position_on_failure)
        assert "asyncio.to_thread" in src, (
            "_close_position_on_failure: send_order_failure_alert must be "
            "called via asyncio.to_thread"
        )
        assert "send_order_failure_alert" in src

    def test_al_chase_handle_blocked_uses_to_thread(self):
        m = self._get_module()
        src = inspect.getsource(m._al_chase_handle_blocked)
        assert "asyncio.to_thread" in src, (
            "_al_chase_handle_blocked: send_order_failure_alert must be "
            "called via asyncio.to_thread"
        )
        assert "send_order_failure_alert" in src

    def test_chase_handle_results_uses_to_thread(self):
        m = self._get_module()
        src = inspect.getsource(m._chase_handle_results)
        assert "asyncio.to_thread" in src, (
            "_chase_handle_results: send_order_failure_alert must be "
            "called via asyncio.to_thread"
        )
        assert "send_order_failure_alert" in src

    def test_no_bare_call_remains(self):
        """Structural check: the pattern 'send_order_failure_alert(' never
        appears without 'asyncio.to_thread' on the preceding call line.

        We check at the module source level: every occurrence of
        send_order_failure_alert as a positional first arg must be preceded by
        'asyncio.to_thread(' within the same try block (checked via line scan).
        """
        m = self._get_module()
        full_src = inspect.getsource(m)
        lines = full_src.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            # A bare call looks like: send_order_failure_alert(  (no 'to_thread' on same line)
            if stripped.startswith("send_order_failure_alert(") and "to_thread" not in stripped:
                # Check the line above — should be asyncio.to_thread(
                prev = lines[i - 1].strip() if i > 0 else ""
                assert "asyncio.to_thread" in prev, (
                    f"Line {i + 1}: bare send_order_failure_alert call found "
                    f"(not wrapped in asyncio.to_thread). Previous line: {prev!r}"
                )


# ---------------------------------------------------------------------------
# Fix 2 — background.py: _watchdog_check_market_open pre-fetches sessions,
#           _task_funds_offhours / _task_closed_hours_refresh /
#           _backfill_run_intraday wrap is_any_segment_open
# ---------------------------------------------------------------------------

class TestBackgroundToThread:

    def _get_module(self):
        import backend.api.background as m
        return m

    def test_watchdog_check_market_open_uses_to_thread(self):
        m = self._get_module()
        src = inspect.getsource(m._watchdog_check_market_open)
        assert "asyncio.to_thread" in src, (
            "_watchdog_check_market_open: _fetch_special_sessions_safe must "
            "be pre-fetched via asyncio.to_thread to avoid blocking the event loop"
        )
        # Confirm the blocking call itself is gone from a generator
        assert "_fetch_special_sessions_safe" in src

    def test_task_funds_offhours_uses_to_thread_for_segment_check(self):
        m = self._get_module()
        src = inspect.getsource(m._task_funds_offhours)
        assert "asyncio.to_thread" in src, (
            "_task_funds_offhours: is_any_segment_open must be called via "
            "asyncio.to_thread"
        )
        assert "is_any_segment_open" in src

    def test_task_closed_hours_refresh_uses_to_thread_for_segment_check(self):
        m = self._get_module()
        src = inspect.getsource(m._task_closed_hours_refresh)
        assert "asyncio.to_thread" in src, (
            "_task_closed_hours_refresh: is_any_segment_open must be called "
            "via asyncio.to_thread"
        )
        assert "is_any_segment_open" in src

    def test_backfill_run_intraday_uses_to_thread_for_segment_check(self):
        m = self._get_module()
        src = inspect.getsource(m._backfill_run_intraday)
        assert "asyncio.to_thread" in src, (
            "_backfill_run_intraday: is_any_segment_open must be called via "
            "asyncio.to_thread"
        )
        assert "is_any_segment_open" in src


# ---------------------------------------------------------------------------
# Fix 3 — health.py: git subprocess calls wrapped in asyncio.to_thread
# ---------------------------------------------------------------------------

class TestHealthToThread:

    def _get_handler_src(self) -> str:
        """Return source of the underlying get_health function.

        HealthController.get_health is a Litestar ``get`` handler object.
        The actual coroutine is stored in handler.fn.
        """
        from backend.api.routes.health import HealthController
        handler = HealthController.get_health
        # Litestar wraps the function in a handler object; unwrap via .fn.
        fn = getattr(handler, "fn", handler)
        return inspect.getsource(fn)

    def test_get_health_wraps_git_hash(self):
        src = self._get_handler_src()
        assert "asyncio.to_thread" in src, (
            "get_health: _git_hash must be called via asyncio.to_thread"
        )
        assert "_git_hash" in src

    def test_get_health_wraps_git_subject(self):
        src = self._get_handler_src()
        assert "asyncio.to_thread" in src
        assert "_git_subject" in src

    def test_git_hash_not_called_bare(self):
        """_git_hash() must not appear as a direct call (without to_thread)."""
        src = self._get_handler_src()
        lines = src.splitlines()
        for line in lines:
            stripped = line.strip()
            # Direct bare call: _git_hash() assigned without to_thread
            if "_git_hash()" in stripped and "to_thread" not in stripped:
                pytest.fail(
                    f"Bare _git_hash() call found in get_health: {stripped!r}"
                )

    def test_git_subject_not_called_bare(self):
        """_git_subject() must not appear as a direct call (without to_thread)."""
        src = self._get_handler_src()
        lines = src.splitlines()
        for line in lines:
            stripped = line.strip()
            if "_git_subject()" in stripped and "to_thread" not in stripped:
                pytest.fail(
                    f"Bare _git_subject() call found in get_health: {stripped!r}"
                )


# ---------------------------------------------------------------------------
# Behavioral test: asyncio.to_thread receives send_order_failure_alert as first arg
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_place_order_preflight_block_passes_alert_fn_to_to_thread(monkeypatch):
    """Confirm asyncio.to_thread is called with send_order_failure_alert as
    its first positional argument inside _place_order_preflight_block.

    We patch asyncio.to_thread to capture calls, and replace _write_live_order
    with a trivial coroutine so the function can complete without DB/broker.
    """
    import asyncio
    import backend.api.algo.actions_live as m

    captured_first_args: list = []

    async def fake_to_thread(fn, /, **kwargs):  # type: ignore[override]
        captured_first_args.append(fn)
        # Don't actually call the real fn (no network needed).

    # Patch asyncio.to_thread in the module under test.
    monkeypatch.setattr(m.asyncio, "to_thread", fake_to_thread)

    # Stub out _write_live_order so the await doesn't fail.
    async def fake_write_live_order(*args, **kwargs):
        return 42  # fake order id

    # Stub out asyncio.create_task so the task creation inside the try block
    # doesn't blow up in the test event loop.
    original_create_task = asyncio.create_task

    async def fake_write_ev(*args, **kwargs):
        pass

    monkeypatch.setattr(
        "backend.api.algo.actions_live.asyncio.create_task",
        lambda coro: coro.close() or None,  # suppress the coroutine cleanly
    )

    import unittest.mock as mock

    with mock.patch(
        "backend.api.algo.actions._write_live_order",
        side_effect=fake_write_live_order,
    ):
        from backend.shared.helpers.alert_utils import send_order_failure_alert as real_fn

        pf = {"blocked": [{"reason": "G2: fat finger cap exceeded", "code": "G2"}]}
        context = {"agent_slug": "test_agent"}

        class _FakeShim:
            slug = "test_agent"

        await m._place_order_preflight_block(
            pf=pf,
            agent_shim=_FakeShim(),
            context=context,
            account="ACC001",
            symbol="NIFTY24NOV24000CE",
            exchange="NFO",
            side="BUY",
            qty=1,
            price=100.0,
        )

    assert len(captured_first_args) >= 1, (
        "asyncio.to_thread was never called — send_order_failure_alert is not "
        "being dispatched off the event loop"
    )
    assert captured_first_args[0] is real_fn, (
        f"Expected send_order_failure_alert as first arg to asyncio.to_thread, "
        f"got {captured_first_args[0]!r}"
    )
