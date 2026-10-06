"""_save_market_to_db writes on the main loop when called from another loop."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.api import background
from backend.api.persistence import write_queue


@pytest.mark.asyncio
async def test_writes_directly_when_already_on_main_loop(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(write_queue, "_main_loop", loop)
    calls = []

    async def fake_row(resp):
        calls.append(("row", asyncio.get_running_loop()))

    with patch.object(background, "_save_market_row", fake_row):
        await background._save_market_to_db(SimpleNamespace())
    assert calls == [("row", loop)]


def test_hands_write_to_main_loop_from_another_loop(monkeypatch):
    main = asyncio.new_event_loop()
    ready = threading.Event()

    def run_main():
        asyncio.set_event_loop(main)
        ready.set()
        main.run_forever()

    t = threading.Thread(target=run_main, daemon=True)
    t.start()
    ready.wait(2)
    monkeypatch.setattr(write_queue, "_main_loop", main)
    seen = {}

    async def fake_row(resp):
        seen["loop"] = asyncio.get_running_loop()

    async def caller():
        with patch.object(background, "_save_market_row", fake_row):
            await background._save_market_to_db(SimpleNamespace())

    try:
        asyncio.run(caller())
    finally:
        main.call_soon_threadsafe(main.stop)
        t.join(2)
        main.close()
    assert seen["loop"] is main
