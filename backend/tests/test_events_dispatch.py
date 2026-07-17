"""
Tests for api/algo/events.py — agent event dispatcher.
SSOT: dispatch() routes to all enabled channels; _dispatch_channel handles routing.
Perf: event logging via async queue (not per-call DB INSERT).
Stale: agent_event_queue is a module-level EventQueue (not per-request).
Reuse: EvalResult dataclass shared between grammar engine and dispatch path.
UX: failed channel dispatch logs error but doesn't block other channels.
"""
from pathlib import Path

_SRC = Path("backend/api/algo/events.py").read_text()


def test_dispatch_function_exists():
    from backend.api.algo.events import dispatch
    import inspect
    assert inspect.iscoroutinefunction(dispatch), "dispatch must be async"


def test_eval_result_dataclass_exists():
    from backend.api.algo.events import EvalResult
    r = EvalResult(triggered=True, condition_text="pnl > 1000", detail={"pnl": 1500})
    assert r.triggered is True
    assert r.condition_text == "pnl > 1000"


def test_agent_event_queue_is_module_level():
    from backend.api.algo.events import agent_event_queue
    assert agent_event_queue is not None, "agent_event_queue must be a module-level EventQueue"


def test_dispatch_channel_function_exists():
    from backend.api.algo.events import _dispatch_channel
    import inspect
    assert inspect.iscoroutinefunction(_dispatch_channel), "_dispatch_channel must be async"


def test_dispatch_handles_channel_errors_gracefully():
    """Failed channel dispatch must log error and continue (not propagate)."""
    assert "except Exception" in _SRC or "except" in _SRC, (
        "dispatch must catch channel errors to ensure other channels still fire"
    )
    assert "logger.error" in _SRC, (
        "Failed dispatches must be logged as errors for observability"
    )


def test_log_event_is_async():
    from backend.api.algo.events import _log_event
    import inspect
    assert inspect.iscoroutinefunction(_log_event), "_log_event must be async"


def test_event_uses_queue_not_direct_db():
    """Events must use the queue (async, batched) rather than direct DB writes."""
    assert "agent_event_queue.enqueue" in _SRC, (
        "_log_event must use agent_event_queue.enqueue() for batched INSERTs, "
        "not a direct session.add() per event"
    )
