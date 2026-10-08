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
                quantity=1, fill_price=10.0, product="CNC", mode=mode, alert_event="filled")
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
    assert spec["conditions"]["log"]["where"] == {"alert_event": "filled", "mode": "live"}
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


def test_seed_action_inserts_missing_and_keeps_current_rows():
    from types import SimpleNamespace
    from backend.api.algo.event_agents import seed_action, SEED_VERSION
    assert seed_action(None) == "insert"
    assert seed_action(SimpleNamespace(seed_version=SEED_VERSION)) == "keep"


def test_seed_action_updates_only_older_seed_versions():
    from types import SimpleNamespace
    from backend.api.algo.event_agents import seed_action
    assert seed_action(SimpleNamespace(seed_version=0), current_version=1) == "update"
    assert seed_action(SimpleNamespace(seed_version=1), current_version=2) == "update"
    assert seed_action(SimpleNamespace(seed_version=2), current_version=2) == "keep"


def test_seeded_agent_list_covers_every_constant():
    from backend.api.algo import event_agents as ea
    assert len(ea.SEEDED_AGENTS) == len({spec["slug"] for spec in ea.SEEDED_AGENTS})
    assert ea.FILL_AGENT in ea.SEEDED_AGENTS and ea.SUMMARY_AGENT in ea.SEEDED_AGENTS


def test_every_seeded_agent_passes_validation():
    from backend.api.algo import event_agents as ea
    for spec in ea.SEEDED_AGENTS:
        assert ea.validate_seed_spec(spec) == [], spec["slug"]


def test_validation_reports_unknown_tag_renderer_and_channel():
    from backend.api.algo import event_agents as ea
    bad = {"slug": "x", "conditions": {"log": {"tag": "nope", "min_level": "INFO"}},
           "actions": [{"type": "render", "render": "missing"}],
           "events": [{"channel": "pager"}]}
    errs = ea.validate_seed_spec(bad)
    assert any("unknown log tag 'nope'" in e for e in errs)
    assert any("unknown renderer 'missing'" in e for e in errs)
    assert any("unknown channel" in e for e in errs)


@pytest.mark.asyncio
async def test_simulated_records_are_skipped_unless_switch_on(monkeypatch):
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
    sim = _rec()
    sim["extra"]["sim_mode"] = True
    monkeypatch.setattr(event_agents, "_sim_notify_allowed", lambda: False)
    await event_agents.dispatch_rows([sim])
    assert seen == []
    monkeypatch.setattr(event_agents, "_sim_notify_allowed", lambda: True)
    await event_agents.dispatch_rows([sim])
    assert len(seen) == 1


def _breach_rec(channels):
    return {"ts": datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "level": "INFO",
            "logger": "backend.api.algo.events", "message": "x", "tags": ["info", "agent"],
            "extra": {"tags": ["agent"], "alert_event": "breach", "agent_slug": "loss-funds",
                      "agent_name": "Account funds gone negative", "condition_text": "cash < 0",
                      "channels": channels, "telegram_body": "Alert — Account funds gone negative",
                      "ntfy_body": "Alert — Account funds gone negative\nCondition: cash < 0",
                      "email_subject": "RamboQuant Agent: Account funds gone negative",
                      "email_body": "<html>cash</html>", "sim_mode": False}}


@pytest.mark.asyncio
async def test_breach_record_sends_only_its_stored_channels(monkeypatch):
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.mail_utils as mu
    sent = {"tg": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent["tg"].append(msg))
    monkeypatch.setattr(au, "send_ntfy_alert", lambda title, message, priority=None: sent["ntfy"].append((title, message, priority)))
    channels = [{"channel": "telegram", "enabled": True, "gate": True},
                {"channel": "ntfy", "enabled": True, "priority": "urgent", "gate": True},
                {"channel": "email", "enabled": True, "gate": True}]
    spec = event_agents.BREACH_AGENT
    agent = SimpleNamespace(slug=spec["slug"], **{k: spec[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([_breach_rec(channels)], [agent])
    assert sent["tg"] == ["Alert — Account funds gone negative"]
    assert sent["ntfy"] == [("Account funds gone negative", "Alert — Account funds gone negative\nCondition: cash < 0", "urgent")]
    assert sent["mail"][0][1] == "a@x.com" and sent["mail"][0][2] == "RamboQuant Agent: Account funds gone negative"
    assert sent["mail"][0][0] == "RamboQuant"


@pytest.mark.asyncio
async def test_breach_record_respects_disabled_capability(monkeypatch):
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: False)
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent.append(msg))
    spec = event_agents.BREACH_AGENT
    agent = SimpleNamespace(slug=spec["slug"], **{k: spec[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([_breach_rec([{"channel": "telegram", "enabled": True, "gate": True}])], [agent])
    assert sent == []


def test_telegram_ntfy_and_email_are_recorded_channels():
    from backend.api.algo import events
    assert events._RECORDED_CHANNELS == {"telegram", "email", "ntfy"}
    assert event_agents.CHANNELS["email"][0] == "mail"


@pytest.mark.asyncio
async def test_dispatch_rows_restores_bulk_bodies_for_sending(monkeypatch):
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
    rec = _rec()
    rec["extra"]["alert_event"] = "summary"
    rec["_bulk"] = {"tg_table": "BIG"}
    await event_agents.dispatch_rows([rec])
    assert seen[0]["extra"]["tg_table"] == "BIG"


def test_email_recipients_default_to_alert_list(monkeypatch):
    import backend.shared.helpers.alert_utils as au
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["ops@x.com"])
    assert event_agents.resolve_email_recipients(None) == ["ops@x.com"]
    assert event_agents.resolve_email_recipients("alert") == ["ops@x.com"]


def test_email_recipients_explicit_list_drops_non_addresses():
    assert event_agents.resolve_email_recipients(["a@x.com", "nope", "b@y.com"]) == ["a@x.com", "b@y.com"]
    assert event_agents.resolve_email_recipients("partners") == []


def test_email_channel_sends_only_to_its_configured_recipients(monkeypatch):
    import backend.shared.helpers.mail_utils as mu
    sent = []
    monkeypatch.setattr(mu, "send_email", lambda name, addr, subj, body: sent.append(addr))
    event_agents._send_email_channel("t", "b", None, email=("s", "<html/>"), recipients=["only@x.com"])
    assert sent == ["only@x.com"]


def test_gtt_acceptance_accepts_active_and_rejects_refused(monkeypatch):
    from backend.api.algo import template_attach as ta
    # "not present" now retries a few times before giving up (see
    # _GTT_VERIFY_RETRIES) — patch out the real sleep so this still-valid
    # outcome doesn't add real wall-clock delay to the suite.
    monkeypatch.setattr(ta.time, "sleep", lambda *_a, **_k: None)

    class _B:
        def __init__(self, rows):
            self.rows = rows

        def get_gtts(self):
            return self.rows

    assert ta._verify_gtt_accepted(_B([{"id": 7, "status": "active"}]), "7") is None
    assert "rejected" in ta._verify_gtt_accepted(_B([{"id": 7, "status": "rejected"}]), "7")
    assert "not present" in ta._verify_gtt_accepted(_B([]), "7")


def test_gtt_acceptance_reports_a_failed_read():
    from backend.api.algo import template_attach as ta
    class _B:
        def get_gtts(self):
            raise RuntimeError("down")

    assert "status read failed" in ta._verify_gtt_accepted(_B(), "7")


def test_gtt_not_accepted_agent_is_seeded_and_renders():
    from backend.api.algo import event_agents as ea
    assert ea.GTT_NOT_ACCEPTED_AGENT in ea.SEEDED_AGENTS
    assert ea.validate_seed_spec(ea.GTT_NOT_ACCEPTED_AGENT) == []
