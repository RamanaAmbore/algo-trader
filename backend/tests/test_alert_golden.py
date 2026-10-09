"""Golden output for the error and fill alerts as they are today. Any migration must keep these exact strings."""
import datetime as _dt
from types import SimpleNamespace

import pytest

from backend.api.algo import fill_notify
from backend.shared.helpers import alert_utils, error_alerts


class _FixedDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 6, 10, 15, 30, tzinfo=tz)


# Fixed dual-tz string every agent-less renderer's new timestamp line
# resolves to in this module — same instant (2026-10-06 04:45:30 UTC)
# used by _error_rec and the other fixed-`ts` records below, via
# format_dual_tz, so it's not an arbitrary guessed value.
_FIXED_TS = "Tue 06 Oct 10:15 IST | Tue 06 Oct 00:45 EDT"


@pytest.fixture(autouse=True)
def _freeze_timestamp_display(monkeypatch):
    """Freeze `timestamp_display()` to `_FIXED_TS` for every test in this
    module. Every new call site added for the 2026-10 notification-header
    work does `from backend.shared.helpers.date_time_utils import
    timestamp_display` lazily inside its own function body (never a
    top-level import — see alert_utils.py docstring on
    format_notification_header), so patching the attribute on the
    date_time_utils module here is picked up by all of them."""
    from backend.shared.helpers import date_time_utils
    monkeypatch.setattr(date_time_utils, "timestamp_display", lambda: _FIXED_TS)


@pytest.fixture
def sent(monkeypatch):
    import backend.shared.helpers.utils as u
    calls = {"ntfy": [], "tg": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(alert_utils, "send_ntfy_alert", lambda t, b, **k: calls["ntfy"].append((t, b)))
    monkeypatch.setattr(alert_utils, "_send_telegram", lambda m, **k: calls["tg"].append(m))
    return calls


def _error_rec(msg, name="backend.x", alert_now=True):
    from datetime import timezone as _tz
    return {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": "ERROR",
            "logger": name, "message": msg, "tags": ["error"],
            "extra": {"alert_now": alert_now} if alert_now else {}}


@pytest.fixture
def error_agent(monkeypatch):
    from backend.api.algo import event_agents
    monkeypatch.setattr(event_agents, "_gate", error_alerts.RepeatGate())
    return SimpleNamespace(slug="error-alert", **{k: event_agents.ERROR_AGENT[k]
                                                  for k in ("conditions", "events", "actions")})


@pytest.mark.asyncio
async def test_error_alert_golden_text(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("boom: bad value")], [error_agent])
    assert sent["ntfy"] == [("RamboQuant error", f"backend.x\nboom: bad value\n{_FIXED_TS}")]
    assert sent["tg"] == [f"<b>RamboQuant error</b>\n<code>backend.x</code>\nboom: bad value\n{_FIXED_TS}"]


@pytest.mark.asyncio
async def test_error_alert_strips_html_like_fragments_as_before(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("boom <bad>")], [error_agent])
    assert sent["ntfy"] == [("RamboQuant error", f"backend.x\nboom\n{_FIXED_TS}")]


@pytest.mark.asyncio
async def test_error_alert_repeat_suffix_after_cooldown(monkeypatch, sent, error_agent):
    from backend.api.algo import event_agents
    clock = {"t": 0.0}
    monkeypatch.setattr(event_agents, "_gate", error_alerts.RepeatGate(clock=lambda: clock["t"]))
    await event_agents.dispatch([_error_rec("down", alert_now=True)], [error_agent])
    clock["t"] = 10.0
    await event_agents.dispatch([_error_rec("down", alert_now=True)], [error_agent])
    clock["t"] = error_alerts.COOLDOWN_S + 1
    await event_agents.dispatch([_error_rec("down", alert_now=True)], [error_agent])
    assert [b for _, b in sent["ntfy"]] == [
        f"backend.x\ndown\n{_FIXED_TS}", f"backend.x\ndown (+1 repeats)\n{_FIXED_TS}",
    ]
    assert sent["tg"][1] == f"<b>RamboQuant error</b>\n<code>backend.x</code>\ndown (+1 repeats)\n{_FIXED_TS}"


@pytest.mark.asyncio
async def test_error_alert_masks_account_numbers(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("order failed for ZG0790.orders")], [error_agent])
    assert "ZG0790" not in sent["ntfy"][0][1]


def test_fill_format_golden():
    from datetime import timezone
    row = SimpleNamespace(id=101, account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="NFO",
                          transaction_type="BUY", quantity=75, fill_price=112.5, product="NRML", mode="live")
    title, body = fill_notify.format_fill_message(
        row, when=_dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc))
    assert title == "Order filled: BUY 75 NIFTY26OCT25000CE"
    assert body == "\n".join([
        "Account: ZG0790",
        "BUY 75 NIFTY26OCT25000CE (NFO) @ 112.50",
        "Product: NRML",
        "Order id: 101",
        f"Time: {_FIXED_TS}",
    ])


@pytest.mark.asyncio
async def test_fill_delivery_golden_via_event_agent(monkeypatch):
    from datetime import timezone
    from backend.api.algo import event_agents
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.utils as u
    sent = {"ntfy": [], "tg": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "send_ntfy_alert", lambda t, b, **k: sent["ntfy"].append((t, b)))
    monkeypatch.setattr(au, "_send_telegram", lambda m, **k: sent["tg"].append(m))
    records = []
    monkeypatch.setattr(fill_notify.logger, "info", lambda msg, **kw: records.append(kw["extra"]))
    row = SimpleNamespace(id=7, account="ZG0790", symbol="CRUDEOIL26OCTFUT", exchange="MCX",
                          transaction_type="SELL", quantity=1, fill_price=8750.0, product="NRML", mode="live")
    await fill_notify.notify_fills([row])
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc), "level": "INFO",
           "tags": ["orders"], "extra": records[0]}
    agent = SimpleNamespace(slug=event_agents.FILL_AGENT["slug"], **{k: event_agents.FILL_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent["ntfy"] == [("Order filled: SELL 1 CRUDEOIL26OCTFUT",
                             f"Account: ZG0790\nSELL 1 CRUDEOIL26OCTFUT (MCX) @ 8750.00\nProduct: NRML\nOrder id: 7\nTime: {_FIXED_TS}")]
    assert sent["tg"] == ["<b>Order filled: SELL 1 CRUDEOIL26OCTFUT</b>\n"
                          f"Account: ZG0790\nSELL 1 CRUDEOIL26OCTFUT (MCX) @ 8750.00\nProduct: NRML\nOrder id: 7\nTime: {_FIXED_TS}"]


def _chase_rec():
    from datetime import timezone as _tz
    return {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": "CRITICAL",
            "logger": "backend.api.algo.chase", "message": "Chase NIFTY: ...", "tags": ["chase"],
            "extra": {"tags": ["chase"], "alert_event": "cancel_unconfirmed", "transaction_type": "BUY",
                      "symbol": "NIFTY26OCTFUT", "account": "ZG0790", "order_id": "O1",
                      "attempt": 2, "quantity": 100, "remaining_qty": 60}}


@pytest.mark.asyncio
async def test_chase_cancel_golden_text_and_urgent_priority(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append((t, b, priority))))
    agent = SimpleNamespace(slug="chase-cancel-alert",
                            **{k: event_agents.CHASE_CANCEL_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([_chase_rec()], [agent])
    assert sent == [(
        "Chase cancel unconfirmed — possible resting duplicate order",
        "BUY NIFTY26OCTFUT — cancel of order O1 on ZG#### could not be confirmed after attempt 2/40 filled. "
        "The chase has been ABORTED without placing a replacement order. "
        "Manually verify the broker's order book — the old order may still be live.\n"
        f"{_FIXED_TS}",
        "urgent",
    )]


@pytest.mark.asyncio
async def test_partial_gtt_golden_text_and_urgent_priority(monkeypatch):
    from datetime import timezone as _tz
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append((t, b, priority))))
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": "CRITICAL",
           "logger": "backend.api.routes.orders_place", "message": "PARTIAL GTT", "tags": ["gtt"],
           "extra": {"tags": ["orders", "gtt"], "alert_event": "partial_gtt", "parent_row_id": 1088,
                     "parent_symbol": "CRUDEOIL26OCTFUT", "planned": 3, "placed": 1,
                     "errors": ["rate limit", "invalid price"]}}
    agent = SimpleNamespace(slug="partial-gtt-alert",
                            **{k: event_agents.PARTIAL_GTT_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent == [("Partial GTT placement",
                     f"parent #1088 CRUDEOIL26OCTFUT: 1/3 GTTs placed. Errors: rate limit; invalid price\n{_FIXED_TS}",
                     "urgent")]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_key, event, level, extra, expected", [
    ("TEMPLATE_ATTACH_URGENT_AGENT", "wing_unprotected", "WARNING",
     {"gtt_ids_text": "['G1', 'G2']", "reason": "no candidate", "parent_order_id": 7,
      "symbol": "NIFTY", "exchange": "NFO"},
     ("Unprotected SELL position",
      f"GTTs placed (ids: ['G1', 'G2']) but wing failed: no candidate | order #7 NIFTY NFO\n{_FIXED_TS}", "urgent")),
    ("TEMPLATE_ATTACH_URGENT_AGENT", "wing_hard_reject", "CRITICAL",
     {"reason": "premium too high", "symbol": "NIFTY", "exchange": "NFO", "target_premium": 12.5},
     ("Wing scan hard-rejected", f"premium too high | NIFTY NFO target ₹12.50\n{_FIXED_TS}", "urgent")),
    ("TEMPLATE_ATTACH_HIGH_AGENT", "wing_skip", "WARNING",
     {"reason": "no candidate", "parent_order_id": 9, "symbol": "BANKNIFTY", "exchange": "NFO"},
     ("Wing attach skipped", f"no candidate | order #9 BANKNIFTY NFO\n{_FIXED_TS}", "high")),
    ("TEMPLATE_ATTACH_HIGH_AGENT", "wing_offset_skip", "WARNING",
     {"reason": "offset outside band", "parent_order_id": 9, "symbol": "BANKNIFTY", "exchange": "NFO"},
     ("Wing offset attach skipped", f"offset outside band | order #9 BANKNIFTY NFO\n{_FIXED_TS}", "high")),
])
async def test_template_attach_golden_text_and_priority(monkeypatch, agent_key, event, level, extra, expected):
    from datetime import timezone as _tz
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append((t, b, priority))))
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": level,
           "logger": "backend.api.algo.template_attach", "message": "x", "tags": ["gtt"],
           "extra": {"tags": ["orders", "gtt"], "alert_event": event, **extra}}
    spec = getattr(event_agents, agent_key)
    agent = SimpleNamespace(slug=spec["slug"], **{k: spec[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent == [expected]


@pytest.mark.asyncio
async def test_order_failure_golden_text_channels_and_html(monkeypatch):
    import hashlib
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    sent = {"tg": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: False)
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    import backend.shared.helpers.mail_utils as mu
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append(tg)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    monkeypatch.setitem(event_agents.CHANNELS, "email", ("ntfy", event_agents._send_email_channel))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "logger": "backend.shared.helpers.alert_utils", "message": "x", "tags": ["orders"],
           "extra": {"tags": ["orders"], "alert_event": "order_failure", "masked": "ZG####",
                     "symbol": "NIFTY26OCTFUT", "exchange": "NFO", "side": "BUY", "qty": 75,
                     "mode": "live", "source": "ticket", "error": "Insufficient funds <x>",
                     "suppressed_count": 2, "ist_disp": "10:15:30 IST", "branch": "main"}}
    agent = SimpleNamespace(slug="order-failure-alert",
                            **{k: event_agents.ORDER_FAILURE_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # 2026-10 fix: every order-failure alert is now prefixed with its
    # origin label (Manual/Manual Bracket/Agent/Agent Bracket) ahead of
    # the mode tag — this record's `extra` has no "agent_id" key (the
    # shape persisted logs had before that fix), so it correctly
    # defaults to "Manual" (see alert_utils._classify_order_origin_label).
    # The dual-tz header line is "10:15:30 IST" unchanged (not _FIXED_TS)
    # because this test's `ist_disp` is passed straight through
    # format_notification_header's `ist_display=` kwarg — no agent name
    # is available at this call site (see alert_utils.order_failure_messages),
    # so no "Agent: ..." line is added, only the reused timestamp string.
    assert sent["tg"] == ['<b>&#10060; Order rejected</b>  [Manual]  [LIVE]  (+2 suppressed)\n10:15:30 IST\nZG####  BUY  75  NIFTY26OCTFUT  (NFO)\nsource: ticket\n<code>Insufficient funds &lt;x&gt;</code>']
    assert sent["ntfy"] == [("Order Rejected: NIFTY26OCTFUT BUY",
                             "❌ Order rejected  [Manual]  [LIVE]  (+2 suppressed)\n10:15:30 IST\nZG####  BUY  75  NIFTY26OCTFUT  (NFO)\nsource: ticket\nInsufficient funds <x>",
                             "urgent")]
    assert len(sent["mail"]) == 1
    _, addr, subj, body = sent["mail"][0]
    assert addr == "a@x.com"
    assert subj == "[Manual] RamboQuant Order Rejected: NIFTY26OCTFUT BUY (live)"
    assert hashlib.sha256(body.encode()).hexdigest() == "3617fd02eb9f98ba63c409024aabfac8764c5e88383b0c6d1c1c038123aab9c1"


@pytest.mark.asyncio
async def test_template_guard_golden_text_and_channels(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = {"tg": [], "ntfy": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append(tg)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.algo.template_attach", "message": "x", "tags": ["info", "orders"],
           "extra": {"tags": ["orders"], "alert_event": "template_guard", "template_slug": "default-bull",
                     "applies_to": "sell_option", "reason": "qty < lot_size", "parent_order_id": 1088,
                     "parent_side": "SELL", "parent_qty": 75, "parent_symbol": "NIFTY26OCTFUT",
                     "parent_fill_price": 112.5, "parent_account": "ZG0790",
                     "ist_label": "Tue, Oct 06 2026, 14:30 IST"}}
    agent = SimpleNamespace(slug="template-guard-alert",
                            **{k: event_agents.TEMPLATE_GUARD_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    expected_tg = ("<b>⚠ Template guard fired — Tue, Oct 06 2026, 14:30 IST</b>\n\n<code>order #1088\nSELL 75 NIFTY26OCTFUT\n"
                   "@ ₹112.50  (ZG0790)\n\ntemplate:    default-bull\napplies_to:  sell_option\nreason:      qty &lt; lot_size\n\n"
                   "Parent order FILLED. Exits NOT attached.\nArm exits manually if needed.\n\n"
                   "Fix: at /admin/templates, change this template's 'applies_to' to 'both' or 'buy_option'.</code>")
    assert sent["tg"] == [expected_tg]
    assert len(sent["ntfy"]) == 1 and sent["ntfy"][0][0] == "Template guard fired" and sent["ntfy"][0][2] == "high"
    assert "qty < lot_size" in sent["ntfy"][0][1] and "<code>" not in sent["ntfy"][0][1]


@pytest.mark.asyncio
async def test_template_attach_fail_golden_text_and_channels(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = {"tg": [], "ntfy": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append(tg)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, 0, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "logger": "backend.api.algo.template_attach", "message": "x", "tags": ["warning", "orders"],
           "extra": {"tags": ["orders"], "alert_event": "template_attach_fail", "order_id": 1088,
                     "symbol": "NIFTY26OCTFUT", "account": "ZG0790",
                     "err_summary": "qty < lot_size; rate limit", "ist_label": "Tue, Oct 06 2026, 14:30 IST"}}
    agent = SimpleNamespace(slug="template-attach-fail-alert",
                            **{k: event_agents.TEMPLATE_ATTACH_FAIL_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent["tg"] == ["<b>⚠ Template attach failed — Tue, Oct 06 2026, 14:30 IST</b>\n\n<code>order #1088\nsymbol:   NIFTY26OCTFUT\naccount:  ZG0790\n\nerrors:   qty &lt; lot_size; rate limit\n\nParent order FILLED. Exits NOT attached.\nArm exits manually if needed.</code>"]
    assert sent["ntfy"][0][0] == "Template attach failed" and sent["ntfy"][0][2] == "urgent"


@pytest.mark.asyncio
async def test_mcp_ping_sends_recorded_html_to_telegram_only(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent.append(tg)))
    tg = "<b>MCP CANCEL [LIVE]</b> order_id=<code>O1</code>\nacct=ZG####"
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.routes.lab", "message": "MCP ping", "tags": ["info", "mcp"],
           "extra": {"tags": ["mcp"], "alert_event": "mcp_ping", "tg": tg}}
    agent = SimpleNamespace(slug="mcp-ping-alert",
                            **{k: event_agents.MCP_PING_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent == [tg]


@pytest.mark.asyncio
async def test_deploy_sync_sends_title_and_body_at_high_priority(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append((t, b, priority))))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "logger": "backend.api.background", "message": "x", "tags": ["warning", "deploy"],
           "extra": {"tags": ["deploy"], "alert_event": "deploy_out_of_sync",
                     "title": "Deploy out of sync — main", "body": "Local HEAD abc12345 != origin/main"}}
    agent = SimpleNamespace(slug="deploy-sync-alert",
                            **{k: event_agents.DEPLOY_SYNC_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent == [("Deploy out of sync — main", "Local HEAD abc12345 != origin/main", "high")]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, sim, mode_tag, expected_title, expected_subject, expected_sha, expected_tg_head", [
    ("live", False, "", "Agent — 10:15:30 IST", "RamboQuant Agent: ZG0790 summary",
     "1af06ceba4520c4eafe151f6374e443db04ce2a2d7897ec49d58625f9b978b8e", "<b>Agent — 10:15:30 IST</b>"),
    ("paper", False, "[PAPER]", "Agent — 10:15:30 IST", "RamboQuant Agent:  [PAPER]ZG0790 summary",
     "1af06ceba4520c4eafe151f6374e443db04ce2a2d7897ec49d58625f9b978b8e", "<b>Agent [PAPER] — 10:15:30 IST</b>"),
    ("sim", True, "", "SIMULATOR Agent — 10:15:30 IST", "SIMULATOR RamboQuant Agent: ZG0790 summary",
     "6096d641965b387d72f6ce35c9001cdae3950a7f7422ef0d08ba86024d823ff4", "<b>SIMULATOR Agent — 10:15:30 IST</b>"),
])
async def test_rich_alert_matches_the_pre_migration_dispatch(monkeypatch, name, sim, mode_tag,
                                                              expected_title, expected_subject,
                                                              expected_sha, expected_tg_head):
    import hashlib
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    sent = {"tg": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "config", {"deploy_branch": "main"})
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    import backend.shared.helpers.mail_utils as mu
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append(tg)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.algo.agent_engine", "message": "x", "tags": ["info", "agent"],
           "extra": {"tags": ["agent"], "alert_event": "rich_alert", "agent_slug": "loss-funds",
                     "ist_display": "10:15:30 IST", "tg_table": "▸ Pos NIFTY  -₹1,200 (-1.2%)\n  rule: pnl < -1000",
                     "email_table_html": "<table><tr><td>NIFTY</td><td>-1200</td></tr></table>",
                     "subject_detail": "ZG0790 summary", "sim_mode": sim, "mode_tag": mode_tag}}
    agent = SimpleNamespace(slug="agent-alert-rich",
                            **{k: event_agents.RICH_ALERT_AGENT[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent["ntfy"][0][0] == expected_title and sent["ntfy"][0][2] == "urgent"
    assert sent["tg"][0].startswith(expected_tg_head)
    subj, body = sent["mail"][0][2], sent["mail"][0][3]
    assert subj == expected_subject
    assert hashlib.sha256(body.encode()).hexdigest() == expected_sha


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type, title, subject, sha", [
    ("open", "Open Summary — 10:15:30 IST", "RamboQuant Open Summary: Summary — 10:15:30 IST",
     "025ab93a9c4cdcc0b31c5aaae84c2ea48415de06d15e685863b669d947a6d925"),
    ("close", "Close Summary — 10:15:30 IST", "RamboQuant Close Summary: Summary — 10:15:30 IST",
     "16114ca79451a6c3576cb73206083948184fdc9574ee9aa2caf2777aa03b9770"),
])
async def test_summary_matches_the_pre_migration_dispatch(monkeypatch, msg_type, title, subject, sha):
    import hashlib
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.mail_utils as mu
    sent = {"info": [], "mail": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "config", {"deploy_branch": "main"})
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram_info",
                        ("telegram_info", lambda t, b, tg=None, **k: sent["info"].append(tg)))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.shared.helpers.alert_utils", "message": "x", "tags": ["info", "summary"],
           "extra": {"tags": ["summary"], "alert_event": "summary", "msg_type": msg_type,
                     "ist_display": "10:15:30 IST", "tg_table": "Holdings  ZG####  ₹1,20,000\nPositions  ZG####  -₹300",
                     "email_table_html": "<table><tr><td>Holdings</td><td>120000</td></tr></table>",
                     "subject_detail": "Summary — 10:15:30 IST"}}
    spec = event_agents.SUMMARY_AGENT
    agent = SimpleNamespace(slug=spec["slug"], **{k: spec[k] for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent["info"] == [f"<b>{title}</b>\n\n<code>Holdings  ZG####  ₹1,20,000\nPositions  ZG####  -₹300</code>"]
    _, addr, subj, body = sent["mail"][0]
    assert subj == subject
    assert hashlib.sha256(body.encode()).hexdigest() == sha
