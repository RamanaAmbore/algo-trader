"""Event agents: match, render, per-channel send, capability gate, failure isolation, startup scope."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.api.algo import event_agents
from backend.api.algo.agent_evaluator import validate
from backend.api.algo.grammar_registry import REGISTRY


def _agent(events=None, actions=None, conditions=None):
    return SimpleNamespace(
        slug="t", conditions=conditions or event_agents.FILL_AGENT["conditions"],
        events=events if events is not None else event_agents.FILL_AGENT["events"],
        actions=actions if actions is not None else event_agents.FILL_AGENT["actions"],
    )


def _rec(mode="live", tags=("orders",), level="INFO", **extra):
    base = dict(order_id=1, account="A", symbol="X", exchange="NSE", transaction_type="BUY",
                quantity=1, fill_price=10.0, product="CNC", mode=mode, event="filled")
    base.update(extra)
    return {"ts": datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc), "level": level,
            "tags": list(tags), "extra": base}


@pytest.fixture
def senders(monkeypatch):
    import backend.shared.helpers.utils as u
    calls = {"ntfy": [], "tg": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, *_: calls["ntfy"].append((t, b))))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, *_: calls["tg"].append((t, b))))
    return calls


@pytest.mark.asyncio
async def test_live_fill_record_sends_to_both_channels(senders):
    sent = await event_agents.dispatch([_rec()], [_agent()])
    assert sent == 2
    assert [t for t, _ in senders["ntfy"]] == ["Order filled: BUY 1 X"]
    assert len(senders["tg"]) == 1


@pytest.mark.asyncio
async def test_paper_fill_record_is_not_sent(senders):
    assert await event_agents.dispatch([_rec(mode="paper")], [_agent()]) == 0
    assert senders == {"ntfy": [], "tg": []}


@pytest.mark.asyncio
async def test_non_order_record_is_not_sent(senders):
    assert await event_agents.dispatch([_rec(tags=("broker",))], [_agent()]) == 0


@pytest.mark.asyncio
async def test_disabled_channel_entry_is_skipped(senders):
    agent = _agent(events=[{"channel": "telegram", "enabled": False}, {"channel": "ntfy", "enabled": True}])
    await event_agents.dispatch([_rec()], [agent])
    assert len(senders["ntfy"]) == 1 and senders["tg"] == []


@pytest.mark.asyncio
async def test_capability_off_sends_nothing(monkeypatch, senders):
    import backend.shared.helpers.utils as u
    monkeypatch.setattr(u, "is_enabled", lambda cap: False)
    assert await event_agents.dispatch([_rec()], [_agent()]) == 0


@pytest.mark.asyncio
async def test_agent_without_render_key_is_ignored(senders):
    assert await event_agents.dispatch([_rec()], [_agent(actions=[])]) == 0


@pytest.mark.asyncio
async def test_one_failing_channel_does_not_block_the_other(monkeypatch, senders, capsys):
    def boom(t, b, *_):
        raise RuntimeError("ntfy down")

    monkeypatch.setitem(event_agents.CHANNELS, "ntfy", ("ntfy", boom))
    sent = await event_agents.dispatch([_rec()], [_agent()])
    assert sent == 1 and len(senders["tg"]) == 1
    assert "ntfy send failed" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_dispatch_rows_swallows_load_errors(monkeypatch, capsys):
    async def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(event_agents, "load_agents", broken)
    await event_agents.dispatch_rows([_rec()])
    assert "dispatch failed" in capsys.readouterr().err


def test_render_key_reads_the_render_action():
    assert event_agents._render_key(_agent()) == "fill"
    assert event_agents._render_key(_agent(actions=[{"type": "place_order"}])) is None


def test_fill_agent_condition_validates_against_a_registered_tag(monkeypatch):
    monkeypatch.setattr(REGISTRY, "log_tags", {"orders": {"table": "log_events"}})
    assert validate(event_agents.FILL_AGENT["conditions"]) == []


def test_fill_agent_is_seeded_as_an_event_agent_with_live_only_match():
    spec = event_agents.FILL_AGENT
    assert spec["conditions"]["log"]["where"] == {"event": "filled", "mode": "live"}
    assert {c["channel"] for c in spec["events"]} == {"telegram", "ntfy"}


@pytest.mark.asyncio
async def test_registered_renderer_is_used(monkeypatch, senders):
    monkeypatch.setitem(event_agents.RENDERS, "custom", lambda rec: ("T", "B"))
    agent = _agent(actions=[{"type": "render", "render": "custom"}])
    await event_agents.dispatch([_rec()], [agent])
    assert senders["ntfy"] == [("T", "B")]


@pytest.mark.asyncio
async def test_unknown_renderer_sends_nothing_and_reports_once(monkeypatch, senders, capsys):
    monkeypatch.setattr(event_agents, "_unknown_renders", set())
    agent = _agent(actions=[{"type": "render", "render": "nope"}])
    assert await event_agents.dispatch([_rec(), _rec()], [agent]) == 0
    assert capsys.readouterr().err.count("unknown renderer 'nope'") == 1


@pytest.mark.asyncio
async def test_dev_process_never_dispatches(monkeypatch, senders):
    from backend.shared.helpers import utils as u
    monkeypatch.setattr(u, "config", {"deploy_branch": "dev"})
    called = []

    async def boom():
        called.append(True)
        return []

    monkeypatch.setattr(event_agents, "load_agents", boom)
    await event_agents.dispatch_rows([_rec()])
    assert called == []
    assert senders == {"ntfy": [], "tg": []}


@pytest.mark.asyncio
async def test_main_process_dispatches(monkeypatch):
    from backend.shared.helpers import utils as u
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    called = []

    async def load():
        called.append(True)
        return []

    monkeypatch.setattr(event_agents, "load_agents", load)
    await event_agents.dispatch_rows([_rec()])
    assert called == [True]


@pytest.mark.asyncio
async def test_dev_origin_records_are_not_dispatched_on_prod(monkeypatch, senders):
    from backend.shared.helpers import utils as u
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    seen = []

    async def load():
        return [SimpleNamespace(slug="x")]

    async def capture(records, agents):
        seen.extend(records)
        return 0

    monkeypatch.setattr(event_agents, "load_agents", load)
    monkeypatch.setattr(event_agents, "dispatch", capture)
    dev = _rec()
    dev["extra"]["origin"] = "dev"
    prod = _rec(msg="prod")
    prod["extra"]["origin"] = "main"
    await event_agents.dispatch_rows([dev, prod])
    assert [r["extra"].get("origin") for r in seen] == ["main"]
