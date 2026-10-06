"""run_on_main_loop: DB work from worker threads runs on the main event loop."""
import asyncio
import threading

import pytest

from backend.api.persistence import write_queue as wq


@pytest.fixture
def main_loop(monkeypatch):
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(2)
    monkeypatch.setattr(wq, "_main_loop", loop)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(2)
    loop.close()


async def _value(v):
    return v


def test_returns_default_when_no_main_loop(monkeypatch):
    monkeypatch.setattr(wq, "_main_loop", None)
    coro = _value(1)
    assert wq.run_on_main_loop(coro, default="d") == "d"


def test_runs_on_main_loop_from_worker_thread(main_loop):
    seen = {}

    async def probe():
        seen["loop"] = asyncio.get_running_loop()
        return 42

    result = {}

    def worker():
        result["v"] = wq.run_on_main_loop(probe(), default=-1)

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert result["v"] == 42
    assert seen["loop"] is main_loop


def test_returns_default_when_called_on_the_main_loop(main_loop):
    async def caller():
        return wq.run_on_main_loop(_value(7), default="fallback")

    fut = asyncio.run_coroutine_threadsafe(caller(), main_loop)
    assert fut.result(5) == "fallback"


def test_returns_default_on_error(main_loop):
    async def boom():
        raise RuntimeError("db down")

    result = {}

    def worker():
        result["v"] = wq.run_on_main_loop(boom(), default=[])

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    assert result["v"] == []


def test_holiday_and_special_session_readers_use_the_bridge(monkeypatch):
    calls = []

    def fake(coro, default, timeout=5.0):
        calls.append(default)
        coro.close()
        return default

    from backend.brokers import broker_apis as ba
    monkeypatch.setattr(wq, "run_on_main_loop", fake)
    assert ba._read_special_sessions_sync("NSE", None) == []
    assert ba._read_market_holidays_sync("NSE") == set()
    assert calls == [[], set()]
