"""Event agents: match, render, per-channel send, capability gate, failure isolation, startup scope."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.api.algo import event_agents
from backend.api.algo.agent_evaluator import validate
from backend.api.algo.grammar_registry import REGISTRY


# Literal, not read off `event_agents.FILL_AGENT["events"]` — that key no
# longer exists post-routing-tree-migration (channel selection moved to
# `alert_routing.py`'s per-slug override table). This generic test
# double uses slug "t" (not a real seeded slug), so an explicit non-empty
# `events` list here is what makes `alert_routing.resolve_channels()`
# return it outright (step 1 of its precedence order) without needing a
# real per-slug/tier match — exactly the same channel list FILL_AGENT's
# own dict used to carry directly, kept here only because these generic
# dispatch-mechanics tests don't care about the exact channel set, only
# that dispatch has SOMETHING real to iterate.
_DEFAULT_TEST_EVENTS = [{"channel": "telegram", "enabled": True}]


def _agent(events=None, actions=None, conditions=None):
    return SimpleNamespace(
        slug="t", conditions=conditions or event_agents.FILL_AGENT["conditions"],
        events=events if events is not None else _DEFAULT_TEST_EVENTS,
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


_TG_NTFY_EVENTS = [{"channel": "telegram", "enabled": True}, {"channel": "ntfy", "enabled": True}]


@pytest.mark.asyncio
async def test_live_fill_record_sends_to_both_channels(senders):
    """2026-10-23 chase robustness (fix #4) — FILL_AGENT's own stored
    `events` no longer includes ntfy (a fill is FYI, never action-
    required); this test is about generic 2-channel dispatch mechanics,
    not FILL_AGENT's specific config, so it supplies its own explicit
    telegram+ntfy `events` list instead of relying on FILL_AGENT's."""
    agent = _agent(events=_TG_NTFY_EVENTS)
    sent = await event_agents.dispatch([_rec()], [agent])
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
    agent = _agent(events=_TG_NTFY_EVENTS)  # see _TG_NTFY_EVENTS comment above
    sent = await event_agents.dispatch([_rec()], [agent])
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
    from backend.api.algo import alert_routing
    spec = event_agents.FILL_AGENT
    assert spec["conditions"]["log"]["where"] == {"alert_event": "filled", "mode": "live"}
    # 2026-10-23 chase robustness (fix #4) — a fill is FYI, never
    # action-required: ntfy removed from FILL_AGENT's own RESOLVED
    # channel list (cosmetic — the real enforcement is the binary
    # action_required gate in _dispatch_record_channels, which skips
    # ntfy for every fill record regardless of this list's content).
    #
    # 2026-10 routing-tree plan — `spec` itself no longer carries an
    # `events` key; the real channel set now comes from
    # `alert_routing.resolve_channels()`'s per-slug override table.
    resolved = alert_routing.resolve_channels(
        name=spec["slug"], priority=spec.get("priority"), agent_events=spec.get("events", []))
    assert {c["channel"] for c in resolved} == {"telegram"}


@pytest.mark.asyncio
async def test_registered_renderer_is_used(monkeypatch, senders):
    monkeypatch.setitem(event_agents.RENDERS, "custom", lambda rec: ("T", "B"))
    agent = _agent(actions=[{"type": "render", "render": "custom"}], events=_TG_NTFY_EVENTS)
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
    """On a non-main branch, real notification channels (telegram/ntfy/
    email) never fire for an ordinary (non-'log') event-agent record.

    Behaviour change (fix for the Sprint 6 regression, see
    dispatch_rows's docstring): `load_agents()`/`dispatch()` ARE now
    called even on a non-main branch — the branch gate moved from a
    blanket row-level early-return to a per-channel filter, so 'log' can
    still fire for a record that carries it (see the mixed-channel tests
    below). This test now asserts the actual guarantee that matters:
    telegram/ntfy stay dark, not that load_agents is skipped."""
    from backend.shared.helpers import utils as u
    monkeypatch.setattr(u, "config", {"deploy_branch": "dev"})

    async def load():
        return [_agent()]

    monkeypatch.setattr(event_agents, "load_agents", load)
    await event_agents.dispatch_rows([_rec()])
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


def test_validation_reports_unknown_priority():
    """2026-10 routing-tree plan — a seeded agent's `priority` (the
    `alert_routing.py` matcher key) must be a recognised tier, or it
    would silently never match `_TIER_DEFAULTS` and fall straight
    through to the catch-all with no error reported anywhere."""
    from backend.api.algo import event_agents as ea
    bad = {"slug": "x", "conditions": {"log": {"tag": "orders", "min_level": "INFO"}},
           "actions": [{"type": "render", "render": "fill"}], "priority": "not-a-real-tier"}
    assert any("unknown priority" in e for e in ea.validate_seed_spec(bad))
    ok = {**bad, "priority": "high"}
    assert not any("unknown priority" in e for e in ea.validate_seed_spec(ok))
    no_priority = {k: v for k, v in bad.items() if k != "priority"}
    assert not any("unknown priority" in e for e in ea.validate_seed_spec(no_priority))


@pytest.mark.asyncio
async def test_simulated_records_are_skipped_unless_switch_on(monkeypatch):
    """A sim-mode record is now stamped `_log_only` (real channels
    suppressed, 'log' still eligible) rather than dropped outright, when
    `simulator.notify_during_run` is off. Behaviour change (fix for the
    Sprint 6 regression): the record still reaches `dispatch()` in both
    cases — only the stamped flag differs — so 'log' can fire during a
    sim run regardless of this setting (see the dedicated sim/log tests
    below)."""
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
    assert len(seen) == 1 and seen[0]["_log_only"] is True
    seen.clear()
    monkeypatch.setattr(event_agents, "_sim_notify_allowed", lambda: True)
    await event_agents.dispatch_rows([sim])
    assert len(seen) == 1 and seen[0]["_log_only"] is False


def _breach_rec(channels, sim_mode=False):
    return {"ts": datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "level": "INFO",
            "logger": "backend.api.algo.events", "message": "x", "tags": ["info", "agent"],
            "extra": {"tags": ["agent"], "alert_event": "breach", "agent_slug": "loss-funds",
                      "agent_name": "Account funds gone negative", "condition_text": "cash < 0",
                      "channels": channels, "telegram_body": "Alert — Account funds gone negative",
                      "ntfy_body": "Alert — Account funds gone negative\nCondition: cash < 0",
                      "email_subject": "RamboQuant Agent: Account funds gone negative",
                      "email_body": "<html>cash</html>", "sim_mode": sim_mode}}


def _breach_agent():
    spec = event_agents.BREACH_AGENT
    return SimpleNamespace(slug=spec["slug"], **{k: spec[k] for k in ("conditions", "events", "actions")})


def _log_ch(sim_mode=False):
    """A 'log' channel entry shaped exactly like events.py's
    _classify_recorded_channel would pre-build onto a BREACH_AGENT record."""
    return {"channel": "log", "enabled": True, "gate": True,
            "log_line": "ALERT [loss-funds]: Account funds gone negative — cash < 0",
            "log_agent_slug": "loss-funds", "log_sim_mode": sim_mode}


# ═══════════════════════════════════════════════════════════════════════════
#  Regression coverage: 'log' must be exempt from the deploy-branch gate and
#  the simulator notify-during-run gate, while every other channel (telegram/
#  email/ntfy) keeps both gates exactly as before — including when 'log' and
#  a real channel share the SAME record (the actual Sprint 6 bug scenario).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_log_only_record_fires_on_non_main_branch(monkeypatch):
    import backend.shared.helpers.utils as u
    from backend.shared.helpers import utils as cfg_u
    captured = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(cfg_u, "config", {"deploy_branch": "dev"})
    monkeypatch.setattr(
        event_agents, "_EVENTS_LOGGER",
        SimpleNamespace(warning=lambda msg, **k: captured.append(msg)),
    )

    async def load():
        return [_breach_agent()]

    monkeypatch.setattr(event_agents, "load_agents", load)
    await event_agents.dispatch_rows([_breach_rec([_log_ch()])])
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_real_channels_do_not_fire_on_non_main_branch_regression_guard(monkeypatch):
    """Regression guard — the existing branch gate must still correctly
    block real notification channels; only 'log' is exempt."""
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(u, "config", {"deploy_branch": "dev"})
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent.append(msg))

    async def load():
        return [_breach_agent()]

    monkeypatch.setattr(event_agents, "load_agents", load)
    tg_ch = {"channel": "telegram", "enabled": True, "gate": True}
    await event_agents.dispatch_rows([_breach_rec([tg_ch])])
    assert sent == []


@pytest.mark.asyncio
async def test_mixed_log_and_telegram_record_on_non_main_branch_only_logs(monkeypatch):
    """The actual bug scenario: a single shared record carrying BOTH 'log'
    and 'telegram' (e.g. an agent with events: [{"channel": "telegram"},
    {"channel": "log"}] and no custom per-channel message, which group
    into ONE record per events.py's message-grouping). On a non-main
    branch, 'log' must still fire while 'telegram' stays dark — in the
    SAME dispatch call, not just in two separately-tested pure cases."""
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    captured, sent = [], []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(u, "config", {"deploy_branch": "dev"})
    monkeypatch.setattr(
        event_agents, "_EVENTS_LOGGER",
        SimpleNamespace(warning=lambda msg, **k: captured.append(msg)),
    )
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent.append(msg))

    async def load():
        return [_breach_agent()]

    monkeypatch.setattr(event_agents, "load_agents", load)
    mixed = [{"channel": "telegram", "enabled": True, "gate": True}, _log_ch()]
    await event_agents.dispatch_rows([_breach_rec(mixed)])
    assert len(captured) == 1
    assert sent == []


@pytest.mark.asyncio
async def test_log_only_record_fires_during_sim_regardless_of_notify_setting(monkeypatch):
    import backend.shared.helpers.utils as u
    captured = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    monkeypatch.setattr(event_agents, "_sim_notify_allowed", lambda: False)
    monkeypatch.setattr(
        event_agents, "_EVENTS_LOGGER",
        SimpleNamespace(warning=lambda msg, **k: captured.append(msg)),
    )

    async def load():
        return [_breach_agent()]

    monkeypatch.setattr(event_agents, "load_agents", load)
    await event_agents.dispatch_rows([_breach_rec([_log_ch(sim_mode=True)], sim_mode=True)])
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_real_channels_respect_sim_notify_setting_regression_guard(monkeypatch):
    """Regression guard — the existing sim-notify gate must still
    correctly block real notification channels during an un-switched-on
    simulator run; only 'log' is exempt."""
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    monkeypatch.setattr(event_agents, "_sim_notify_allowed", lambda: False)
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent.append(msg))

    async def load():
        return [_breach_agent()]

    monkeypatch.setattr(event_agents, "load_agents", load)
    tg_ch = {"channel": "telegram", "enabled": True, "gate": True}
    await event_agents.dispatch_rows([_breach_rec([tg_ch], sim_mode=True)])
    assert sent == []


@pytest.mark.asyncio
async def test_mixed_log_and_telegram_record_during_sim_only_logs(monkeypatch):
    """Sim-mode sibling of the branch-gate mixed-record bug fix: 'log' and
    'telegram' sharing one record during an un-switched-on simulator run —
    'log' fires, 'telegram' doesn't, in the same dispatch call."""
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    captured, sent = [], []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    monkeypatch.setattr(event_agents, "_sim_notify_allowed", lambda: False)
    monkeypatch.setattr(
        event_agents, "_EVENTS_LOGGER",
        SimpleNamespace(warning=lambda msg, **k: captured.append(msg)),
    )
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent.append(msg))

    async def load():
        return [_breach_agent()]

    monkeypatch.setattr(event_agents, "load_agents", load)
    mixed = [{"channel": "telegram", "enabled": True, "gate": True}, _log_ch(sim_mode=True)]
    await event_agents.dispatch_rows([_breach_rec(mixed, sim_mode=True)])
    assert len(captured) == 1
    assert sent == []


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


def test_telegram_ntfy_email_and_log_are_recorded_channels():
    """Sprint 6 moved 'log' off events.py's direct-dispatch branches onto
    this same recorded-channel path. websocket/inapp are an explicit,
    operator-confirmed exclusion — they stay UI broadcasts, dispatched
    directly inside events.py:_dispatch_channel, and must never join
    _RECORDED_CHANNELS."""
    from backend.api.algo import events
    assert events._RECORDED_CHANNELS == {"telegram", "email", "ntfy", "log"}
    assert "websocket" not in events._RECORDED_CHANNELS
    assert "inapp" not in events._RECORDED_CHANNELS
    assert event_agents.CHANNELS["email"][0] == "mail"


@pytest.mark.asyncio
async def test_log_channel_entry_logs_via_logger_warning(monkeypatch):
    """event_agents.py's new 'log' CHANNELS entry writes a logger.warning
    line — the self-contained plain-log send ported from events.py's old
    `_log_channel` now that 'log' renders through this table instead of
    events.py's direct-dispatch branches."""
    import backend.shared.helpers.utils as u
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    captured = []
    monkeypatch.setattr(
        event_agents, "_EVENTS_LOGGER",
        SimpleNamespace(warning=lambda msg, **k: captured.append((msg, k.get("extra")))),
    )
    ch = {"channel": "log", "enabled": True, "gate": True,
          "log_line": "ALERT [loss-funds]: Account funds gone negative — cash < 0",
          "log_agent_slug": "loss-funds", "log_sim_mode": False}
    out = ("Account funds gone negative", "Alert — body text", "Alert — body text")
    agent = SimpleNamespace(slug="loss-funds", tier="critical")

    sent = await event_agents._send_channel(ch, out, agent)

    assert sent is True
    assert len(captured) == 1
    msg, extra = captured[0]
    assert msg == "ALERT [loss-funds]: Account funds gone negative — cash < 0"
    assert extra == {"tags": ["agent"], "agent_slug": "loss-funds", "sim_mode": False}
    assert "alert_event" not in extra, (
        "must never carry alert_event — would re-match BREACH_AGENT's own "
        "condition on the next log flush and loop forever"
    )


@pytest.mark.asyncio
async def test_log_channel_falls_back_to_title_and_body_without_prebuilt_line(monkeypatch):
    """A caller that doesn't pre-build log_line/log_agent_slug (e.g. a
    future renderer) still gets a sane 'title — body' log line instead of
    crashing."""
    import backend.shared.helpers.utils as u
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    captured = []
    monkeypatch.setattr(
        event_agents, "_EVENTS_LOGGER",
        SimpleNamespace(warning=lambda msg, **k: captured.append((msg, k.get("extra")))),
    )
    ch = {"channel": "log", "enabled": True}
    out = ("Some Title", "Some Body")
    agent = SimpleNamespace(slug="x", tier="low")

    sent = await event_agents._send_channel(ch, out, agent)

    assert sent is True
    assert captured[0][0] == "Some Title — Some Body"
    assert captured[0][1] == {"tags": ["agent"], "agent_slug": None, "sim_mode": False}


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


# ═══════════════════════════════════════════════════════════════════════════
#  PLAN Part 1 — generalize the token-driven priority resolver (item 1),
#  collapse the 5 generic_notice agents onto a shared table (item 2), fix
#  SEED_VERSION silently overwriting an operator edit (item 3), per-record
#  dispatch trace (item 4), and the fail-loud unrouted-record catch-all
#  (item 5). See .claude/PLAN.md Part 1 / docs/specs/ALERT_AGENTS_DESIGN.md.
# ═══════════════════════════════════════════════════════════════════════════

# ── Item 1 — generalized token-driven priority resolver ──────────────────

@pytest.mark.asyncio
async def test_generalized_priority_resolver_applies_to_any_agent_not_just_rich_alert(monkeypatch):
    """`_rec_tracked_ntfy_priority`/`_channel_with_tracked_priority` are
    not special-cased to RICH_ALERT_AGENT — ANY agent whose own ntfy
    channel carries no hardcoded `priority` resolves it from the
    record's own explicit `priority` token (the `log(priority=...)` DSL
    param — see `actions.py:emit_log`)."""
    from backend.api.algo import event_agents
    sent = []
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append(priority)))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    agent = _agent(events=[{"channel": "ntfy", "enabled": True}])  # no hardcoded priority
    rec = _rec(priority="low")
    await event_agents.dispatch([rec], [agent])
    assert sent == ["low"]


@pytest.mark.asyncio
async def test_generalized_priority_resolver_falls_back_to_tier_when_no_explicit_token(monkeypatch):
    """No explicit `priority` token -> falls back to the agent's own
    `tier`-derived default, same as RICH_ALERT_AGENT's existing path —
    zero-regression fallback chain for an agent that opts in to the
    generalized resolver by omitting a hardcoded priority."""
    from backend.api.algo import event_agents
    from backend.shared.helpers.alert_utils import _TIER_TO_NTFY_PRIORITY
    sent = []
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append(priority)))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    agent = SimpleNamespace(
        slug="t", tier="high", conditions=event_agents.FILL_AGENT["conditions"],
        events=[{"channel": "ntfy", "enabled": True}], actions=event_agents.FILL_AGENT["actions"],
    )
    rec = _rec()  # no priority, no tier token on the record itself
    await event_agents.dispatch([rec], [agent])
    assert sent == [_TIER_TO_NTFY_PRIORITY["high"]]


@pytest.mark.asyncio
async def test_generalized_priority_resolver_never_overrides_an_explicit_hardcoded_priority(monkeypatch):
    """Explicit per-channel priority always wins over a record's token —
    every currently-hardcoded agent (e.g. CHASE_CANCEL_AGENT's ntfy
    "urgent") is unaffected if it doesn't opt in to the generalized
    resolver."""
    from backend.api.algo import event_agents
    sent = []
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append(priority)))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    agent = _agent(events=[{"channel": "ntfy", "enabled": True, "priority": "urgent"}])
    rec = _rec(priority="low")
    await event_agents.dispatch([rec], [agent])
    assert sent == ["urgent"]


# ── Item 2 — 5 generic_notice agents sourced from one shared table ───────

def test_generic_notice_agents_sourced_from_shared_table_byte_identical():
    """Each of the 5 agents is still a separately-named dict built from
    `_GENERIC_NOTICE_SPECS` + `_build_generic_notice_agent()` — the
    collapse is to the MECHANISM, not the identity (council item 2 /
    UX review). Pins the exact `conditions`/`actions` content for every
    one, plus the `priority` token each now carries (2026-10
    routing-tree plan — channel selection moved to
    `alert_routing.py`'s per-slug override table; see
    `test_alert_routing_matches_frozen_pre_migration_oracle` for proof
    the RESOLVED channel list for each of these 5 slugs is unchanged)."""
    from backend.api.algo import event_agents as ea
    assert ea.GTT_ASYMMETRIC_AGENT == {
        "slug": "gtt-asymmetric-alert", "name": "GTT asymmetric",
        "conditions": {"log": {"tag": "gtt", "min_level": "WARNING",
                               "where": {"alert_event": "gtt_asymmetric"}}},
        "priority": "high",
        "actions": [{"type": "render", "render": "generic_notice"}],
    }
    assert ea.OCO_DOUBLE_FIRE_AGENT == {
        "slug": "oco-double-fire-alert", "name": "OCO double-fire",
        "conditions": {"log": {"tag": "gtt", "min_level": "WARNING",
                               "where": {"alert_event": "oco_double_fire"}}},
        "priority": "urgent",
        "actions": [{"type": "render", "render": "generic_notice"}],
    }
    assert ea.TICKER_RECOVERED_AGENT == {
        "slug": "ticker-recovered-alert", "name": "Ticker recovered",
        "conditions": {"log": {"tag": "broker", "min_level": "INFO",
                               "where": {"alert_event": "ticker_recovered"}}},
        "priority": "high",
        "actions": [{"type": "render", "render": "generic_notice"}],
    }
    assert ea.TICKER_DEGRADED_AGENT == {
        "slug": "ticker-degraded-alert", "name": "Ticker degraded",
        "conditions": {"log": {"tag": "broker", "min_level": "WARNING",
                               "where": {"alert_event": "ticker_degraded"}}},
        "priority": "urgent",
        "actions": [{"type": "render", "render": "generic_notice"}],
    }
    assert ea.VISITOR_REPORT_AGENT == {
        "slug": "visitor-report-alert", "name": "Visitor report",
        "conditions": {"log": {"tag": "visitor", "min_level": "INFO",
                               "where": {"alert_event": "visitor_report"}}},
        "priority": "info",
        "actions": [{"type": "render", "render": "generic_notice"}],
    }


def test_generic_notice_agents_are_every_spec_in_the_shared_table():
    """Every row in `_GENERIC_NOTICE_SPECS` produces a SEEDED_AGENTS
    entry, and no OTHER TABLE-SOURCED `generic_notice` agent exists
    outside the table — guards against a future table-style agent being
    added as a one-off literal instead of a table row.

    `UNROUTED_ALERT_AGENT` (council item 5 fix) and `USER_REGISTERED_AGENT`
    (2026-10 operator-reported registration-alert gap) are DELIBERATE,
    documented exceptions — each is a single standalone agent that
    reuses the shared `generic_notice` renderer (neither needs bespoke
    title/body logic; both build `title`/`body` directly onto the log
    record's own `extra` at the producer) without belonging to the
    tag-shaped table above, so both are excluded from this equality by
    name, not silently allowed to drift the count."""
    from backend.api.algo import event_agents as ea
    built_slugs = {ea._build_generic_notice_agent(spec)["slug"] for spec in ea._GENERIC_NOTICE_SPECS}
    generic_notice_slugs = {
        spec["slug"] for spec in ea.SEEDED_AGENTS
        if any(isinstance(a, dict) and a.get("render") == "generic_notice" for a in spec.get("actions") or [])
    } - {ea.UNROUTED_ALERT_AGENT["slug"], ea.USER_REGISTERED_AGENT["slug"]}
    assert built_slugs == generic_notice_slugs == {
        "gtt-asymmetric-alert", "oco-double-fire-alert", "ticker-recovered-alert",
        "ticker-degraded-alert", "visitor-report-alert",
    }


# ── Item 3 — SEED_VERSION no longer silently overwrites an operator edit ──

def test_seed_action_updates_when_row_matches_its_last_seed_write():
    """No operator edit happened — the row's current content still
    hashes to what seeding last wrote — so a version bump proceeds to
    update, same as before this fix for an unmodified row."""
    from backend.api.algo.event_agents import seed_action, compute_seed_spec_hash
    cond, ev, act = {"log": {"tag": "gtt"}}, [{"channel": "ntfy"}], []
    row = SimpleNamespace(seed_version=0, seed_spec_hash=compute_seed_spec_hash(cond, ev, act),
                          conditions=cond, events=ev, actions=act)
    assert seed_action(row, current_version=1) == "update"


def test_seed_action_skips_overwrite_of_an_operator_modified_row():
    """The row's CURRENT events no longer match what seeding last wrote
    (`seed_spec_hash`) — an operator edited it via the UI since. A
    version bump must not silently discard that edit."""
    from backend.api.algo.event_agents import seed_action, compute_seed_spec_hash
    cond, act = {"log": {"tag": "gtt"}}, []
    original_hash = compute_seed_spec_hash(cond, [{"channel": "ntfy"}], act)
    row = SimpleNamespace(seed_version=0, seed_spec_hash=original_hash,
                          conditions=cond, events=[{"channel": "telegram"}], actions=act)
    assert seed_action(row, current_version=1) == "modified_skip"


def test_seed_action_still_updates_a_legacy_row_with_no_hash_history():
    """A row that predates `seed_spec_hash` (or was never seeded) has no
    write history to compare against — falls through to 'update',
    identical to this function's pre-fix behavior for legacy data."""
    from backend.api.algo.event_agents import seed_action
    row = SimpleNamespace(seed_version=0, conditions={}, events=[], actions=[])
    assert seed_action(row, current_version=1) == "update"
    assert not hasattr(row, "seed_spec_hash")


def test_compute_seed_spec_hash_is_deterministic_and_key_order_independent():
    from backend.api.algo.event_agents import compute_seed_spec_hash
    a = compute_seed_spec_hash({"b": 1, "a": 2}, [{"channel": "ntfy"}], [])
    b = compute_seed_spec_hash({"a": 2, "b": 1}, [{"channel": "ntfy"}], [])
    assert a == b
    assert a == compute_seed_spec_hash({"a": 2, "b": 1}, [{"channel": "ntfy"}], [])
    assert a != compute_seed_spec_hash({"a": 3, "b": 1}, [{"channel": "ntfy"}], [])


@pytest.mark.asyncio
async def test_seed_event_agents_preserves_operator_edit_and_logs_skip(monkeypatch, capsys):
    """End-to-end through `seed_event_agents()` itself: an operator-edited
    row (current content diverges from its stored seed hash) is NOT
    overwritten when the code's `SEED_VERSION` is bumped, and the skip
    is reported — but `seed_version` IS bumped so this isn't re-reported
    on every restart at the same code version."""
    from backend.api.algo import event_agents as ea

    spec = ea.FILL_AGENT
    original_hash = ea.compute_seed_spec_hash(spec["conditions"], spec.get("events", []), spec["actions"])
    edited_events = [{"channel": "email", "enabled": True}]  # operator's edit via the UI
    row = SimpleNamespace(
        slug=spec["slug"], name=spec["name"], status="active", kind="event",
        seed_version=0, seed_spec_hash=original_hash,
        conditions=spec["conditions"], events=edited_events, actions=spec["actions"],
    )

    class _FakeResult:
        def scalar_one_or_none(self):
            return row

    class _FakeSession:
        def __init__(self):
            self.committed = False

        async def execute(self, *_a, **_k):
            return _FakeResult()

        async def commit(self):
            self.committed = True

        def add(self, *_a, **_k):
            raise AssertionError("insert must not be attempted for an existing row")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _session_factory():
        return _FakeSession()

    monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))
    import backend.api.database as db
    monkeypatch.setattr(db, "async_session", _session_factory)
    await ea.seed_event_agents()
    assert row.events == edited_events, "operator's edit must survive the seed pass"
    assert row.seed_version == ea.SEED_VERSION, "seed_version still advances so the skip isn't re-reported forever"
    assert row.seed_spec_hash == original_hash, "hash stays at the ORIGINAL write so a future bump re-detects the same edit"
    assert "operator-modified" in capsys.readouterr().err


# ── 2026-10 routing-tree plan — `priority` threaded through seeding ──────

@pytest.mark.asyncio
async def test_seed_event_agents_insert_writes_priority(monkeypatch):
    """A brand-new row (no existing match -> 'insert') is constructed
    with the spec's own `priority` field, the new `Agent.priority`
    column — same seed pass that writes `conditions`/`events`/`actions`.
    `events` must also be written as `[]` (via `spec.get("events", [])`,
    NOT a literal `spec["events"]` KeyError) for a migrated agent whose
    dict no longer carries that key at all."""
    from backend.api.algo import event_agents as ea

    spec = ea.FILL_AGENT
    assert "events" not in spec, "fixture sanity: FILL_AGENT must be post-migration (no 'events' key)"
    assert spec.get("priority"), "fixture sanity: FILL_AGENT must carry a real priority token"
    added = []

    class _FakeResult:
        def scalar_one_or_none(self):
            return None  # no existing row -> action == "insert"

    class _FakeSession:
        async def execute(self, *_a, **_k):
            return _FakeResult()

        async def commit(self):
            pass

        def add(self, obj):
            added.append(obj)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ea, "SEEDED_AGENTS", (spec,))
    import backend.api.database as db
    monkeypatch.setattr(db, "async_session", lambda: _FakeSession())
    await ea.seed_event_agents()
    # Real `Agent(...)` ORM constructor (not mocked — mocking it breaks
    # SQLAlchemy's lambda-SQL analysis of `select(Agent).where(...)` a
    # few lines earlier in `seed_event_agents`, since that analysis
    # inspects `Agent.slug` as a real mapped class attribute). Just
    # constructing an instance does no DB I/O; `_FakeSession.add` is a
    # no-op capture, so nothing is actually persisted.
    assert len(added) == 1
    row = added[0]
    assert row.priority == spec["priority"]
    assert row.events == []
    assert row.conditions == spec["conditions"]
    assert row.actions == spec["actions"]


# ── Item 4 — per-record dispatch trace ────────────────────────────────────

class _FakeTraceLogger:
    def __init__(self):
        self.debug_calls: list[str] = []
        self.warning_calls: list[str] = []

    def isEnabledFor(self, _level):
        return True

    def debug(self, msg, *args):
        self.debug_calls.append(msg % args if args else msg)

    def warning(self, msg, *args):
        self.warning_calls.append(msg % args if args else msg)


@pytest.mark.asyncio
async def test_dispatch_trace_logs_sent_decision_per_channel(monkeypatch, senders):
    """One DEBUG trace line per channel: matched agent slug/tag ->
    resolved channel/priority -> final decision. Does not change what
    actually gets sent (senders fixture still records the real sends)."""
    from backend.api.algo import event_agents
    fake = _FakeTraceLogger()
    monkeypatch.setattr(event_agents, "_logger", fake)
    agent = _agent(events=_TG_NTFY_EVENTS)
    await event_agents.dispatch([_rec()], [agent])
    assert any("channel=telegram" in c and "decision=sent" in c for c in fake.debug_calls)
    assert any("channel=ntfy" in c and "decision=sent" in c for c in fake.debug_calls)
    assert any("agent=t" in c for c in fake.debug_calls)


@pytest.mark.asyncio
async def test_dispatch_trace_logs_action_required_gate_override(monkeypatch):
    """The hidden second gate (`_apply_action_required_ntfy_gate`) is
    exactly the kind of "what's configured != what fired" divergence
    the UX review flagged — the trace must show it was applied."""
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    fake = _FakeTraceLogger()
    monkeypatch.setattr(event_agents, "_logger", fake)
    agent = _agent(events=[{"channel": "ntfy", "enabled": True}])
    rec = _rec(action_required=False)  # pure-FYI -> ntfy gate suppresses it entirely
    sent = await event_agents.dispatch([rec], [agent])
    assert sent == 0
    assert any("suppressed_action_required_gate" in c for c in fake.debug_calls)


@pytest.mark.asyncio
async def test_dispatch_trace_logs_log_only_suppression(monkeypatch):
    """A `_log_only` record (branch/sim gate) suppressing a non-'log'
    channel is also traced."""
    from backend.api.algo import event_agents
    fake = _FakeTraceLogger()
    monkeypatch.setattr(event_agents, "_logger", fake)
    agent = _agent(events=[{"channel": "telegram", "enabled": True}])
    rec = _rec()
    rec["_log_only"] = True
    sent = await event_agents.dispatch([rec], [agent])
    assert sent == 0
    assert any("suppressed_log_only" in c for c in fake.debug_calls)


# ── Item 5 — fail-loud catch-all for an unrouted alert-shaped record ─────
#
# 2026-10 fix: the first version of this catch-all logged its self-alert
# via `_logger` (this module's own `log_store.py`-skip-listed logger),
# which is "recursion-safe" only because it can NEVER be captured by
# `LogStoreHandler`/`dispatch_rows()` at all — which also means it could
# NEVER reach a real channel (Telegram/ntfy/email), only stderr. That's
# exactly the silent-failure mode this item exists to prevent. Fixed:
# the self-alert now logs through `_EVENTS_LOGGER` (captured normally)
# tagged with the reserved `alert_event="unrouted_alert"`, which
# `UNROUTED_ALERT_AGENT` matches and actually sends. Recursion safety is
# now an explicit exclusion in `_looks_alert_shaped()` for that exact
# `alert_event`, tested directly below (not inferred from a logger-name
# side effect).

def _capture_real_channels(monkeypatch):
    """Patch telegram + ntfy to capture real sends, same pattern every
    other golden/dispatch test in this suite uses."""
    import backend.shared.helpers.utils as u
    sent = {"tg": [], "ntfy": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append((t, b, tg))))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    return sent


@pytest.mark.asyncio
async def test_unrouted_alert_shaped_record_reaches_a_real_channel(monkeypatch):
    """Two-stage, faithful to the real pipeline: (1) an unrouted record
    makes `_emit_unrouted_self_alert()` call the REAL `_EVENTS_LOGGER`,
    captured here via a real `logging.Handler` (not a monkeypatched
    stand-in) and converted through the REAL `log_store.row_for()` —
    exactly what `LogStoreHandler` does on the next flush cycle in
    production. (2) that row is fed back through `dispatch_rows()` with
    `UNROUTED_ALERT_AGENT` active, confirming it ACTUALLY reaches a real
    channel — not just a log line. This is the defect the coordinator
    flagged: the prior version of this test only asserted a log call was
    made via a module logger that `log_store.py` structurally excludes
    from capture, which passed even though that call could never reach
    Telegram/ntfy/email in production."""
    import logging
    from backend.api.algo import event_agents as ea
    from backend.shared.helpers import utils as u
    from backend.shared.helpers.log_store import row_for
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})

    captured_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured_records.append(record)

    handler = _Capture()
    ea._EVENTS_LOGGER.addHandler(handler)
    try:
        async def load_no_match():
            # A real agent on a DIFFERENT (still known/valid) tag, so it
            # legitimately does not match the unrouted record below.
            return [SimpleNamespace(slug="x", conditions={"log": {"tag": "orders", "min_level": "INFO"}},
                                    events=[], actions=[])]

        monkeypatch.setattr(ea, "load_agents", load_no_match)
        unrouted_rec = {"ts": datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "level": "WARNING",
                       "logger": "backend.api.algo.chase", "message": "boom", "tags": ["chase"], "extra": {}}
        await ea.dispatch_rows([unrouted_rec])
    finally:
        ea._EVENTS_LOGGER.removeHandler(handler)

    assert len(captured_records) == 1, "exactly one self-alert log call for the one unrouted record"
    self_alert_row = row_for(captured_records[0])
    assert self_alert_row["extra"]["alert_event"] == ea._UNROUTED_ALERT_EVENT
    assert "agent" in self_alert_row["tags"]

    sent = _capture_real_channels(monkeypatch)

    async def load_with_unrouted_agent():
        return [SimpleNamespace(slug=ea.UNROUTED_ALERT_AGENT["slug"],
                                **{k: ea.UNROUTED_ALERT_AGENT[k] for k in ("conditions", "events", "actions")})]

    monkeypatch.setattr(ea, "load_agents", load_with_unrouted_agent)
    await ea.dispatch_rows([self_alert_row])
    assert len(sent["tg"]) == 1 and "chase" in sent["tg"][0][2]
    assert len(sent["ntfy"]) == 1 and sent["ntfy"][0][2] == "high"
    assert "unrouted alertable record" in sent["ntfy"][0][1]


@pytest.mark.asyncio
async def test_unrouted_catch_all_self_alert_record_cannot_trigger_a_second_self_alert(monkeypatch):
    """The actual recursion-safety property, tested directly: feeding
    the self-alert's OWN record shape (tag=agent, alert_event=
    unrouted_alert) back through `dispatch_rows()` — simulating it
    having been captured and re-flushed exactly like any other real log
    row — must never produce a second self-alert, REGARDLESS of whether
    any agent happens to match it."""
    from backend.api.algo import event_agents as ea
    from backend.shared.helpers import utils as u
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    sent = _capture_real_channels(monkeypatch)
    self_alert_calls = []
    monkeypatch.setattr(ea, "_emit_unrouted_self_alert", lambda rec: self_alert_calls.append(rec))

    async def load_no_match():
        return []  # no agent matches -> would be flagged unrouted if not for the guard

    monkeypatch.setattr(ea, "load_agents", load_no_match)
    self_alert_shaped_rec = {
        "ts": datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "level": "WARNING",
        "logger": "backend.api.algo.events", "message": "unrouted alertable record: ...",
        "tags": ["warning", "agent"], "extra": {"alert_event": "unrouted_alert"},
    }
    await ea.dispatch_rows([self_alert_shaped_rec])
    assert self_alert_calls == [], "a record carrying the reserved alert_event must never re-trigger the catch-all"


@pytest.mark.asyncio
async def test_matched_alert_shaped_record_does_not_trigger_the_catch_all(monkeypatch):
    """A record matching at least one agent's conditions is routed, not
    unrouted — no self-alert, even if its channels end up suppressed
    downstream (that's a delivery decision, not a routing failure)."""
    from backend.api.algo import event_agents as ea
    from backend.shared.helpers import utils as u
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    self_alert_calls = []
    monkeypatch.setattr(ea, "_emit_unrouted_self_alert", lambda rec: self_alert_calls.append(rec))

    async def load():
        return [SimpleNamespace(slug="x", conditions={"log": {"tag": "chase", "min_level": "INFO"}},
                                events=[], actions=[])]

    monkeypatch.setattr(ea, "load_agents", load)
    rec = {"ts": datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "level": "WARNING",
           "logger": "backend.api.algo.chase", "message": "boom", "tags": ["chase"], "extra": {}}
    await ea.dispatch_rows([rec])
    assert self_alert_calls == []


@pytest.mark.asyncio
async def test_info_level_record_never_triggers_the_catch_all(monkeypatch):
    """Below WARNING is never "alert-shaped" at all, matched or not —
    the catch-all only concerns records that LOOK like they should have
    alerted."""
    from backend.api.algo import event_agents as ea
    from backend.shared.helpers import utils as u
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    self_alert_calls = []
    monkeypatch.setattr(ea, "_emit_unrouted_self_alert", lambda rec: self_alert_calls.append(rec))
    monkeypatch.setattr(ea, "load_agents", lambda: _async_empty_agents())
    rec = {"ts": datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc), "level": "INFO",
           "logger": "backend.api.algo.chase", "message": "fyi", "tags": ["chase"], "extra": {}}
    await ea.dispatch_rows([rec])
    assert self_alert_calls == []


@pytest.mark.asyncio
async def test_unknown_tag_never_triggers_the_catch_all():
    """A tag outside the known `LOG_TAG_TOKENS` vocabulary is not
    "alert-shaped" by this catch-all's definition — only a record
    carrying a RECOGNISED alert-tag counts."""
    from backend.api.algo import event_agents as ea
    rec = {"level": "WARNING", "tags": ["totally-made-up-tag"], "extra": {}}
    assert ea._looks_alert_shaped(rec) is False


def test_recursion_guard_is_the_reserved_alert_event_not_logger_exclusion():
    """The real recursion guard is `_looks_alert_shaped()` explicitly
    rejecting the reserved `alert_event` — direct unit test, independent
    of dispatch plumbing or agent matching. This is the property that
    replaces the old (broken) logger-exclusion guard."""
    from backend.api.algo import event_agents as ea
    self_alert_rec = {"level": "WARNING", "tags": ["agent"],
                      "extra": {"alert_event": ea._UNROUTED_ALERT_EVENT}}
    assert ea._looks_alert_shaped(self_alert_rec) is False
    # Sanity: an otherwise-identical record WITHOUT the reserved
    # alert_event IS alert-shaped — the guard is specific to this one
    # value, not a blanket exclusion of the "agent" tag.
    ordinary_rec = {"level": "WARNING", "tags": ["agent"], "extra": {"alert_event": "breach"}}
    assert ea._looks_alert_shaped(ordinary_rec) is True


def test_unrouted_alert_agent_is_seeded_and_uses_the_reserved_event():
    """`UNROUTED_ALERT_AGENT`'s own condition must match exactly the
    shape `_emit_unrouted_self_alert()` builds, or the self-alert can
    log successfully but still never reach a real channel."""
    from backend.api.algo import event_agents as ea
    assert ea.UNROUTED_ALERT_AGENT in ea.SEEDED_AGENTS
    assert ea.validate_seed_spec(ea.UNROUTED_ALERT_AGENT) == []
    leaf = ea.UNROUTED_ALERT_AGENT["conditions"]["log"]
    assert leaf["tag"] == "agent"
    assert leaf["where"]["alert_event"] == ea._UNROUTED_ALERT_EVENT == "unrouted_alert"
    assert {c["channel"] for c in ea.UNROUTED_ALERT_AGENT["events"]} >= {"telegram", "ntfy"}


async def _async_empty_agents():
    return []
