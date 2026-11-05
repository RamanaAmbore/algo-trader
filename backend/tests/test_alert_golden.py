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
    return SimpleNamespace(slug="error-alert", **{k: event_agents.ERROR_AGENT.get(k, [])
                                                  for k in ("conditions", "events", "actions")})


@pytest.mark.asyncio
async def test_error_alert_golden_text(sent, error_agent):
    # 2026-10 audit fix: `_render_error` now leads with an origin line
    # ("Agent: manual" — a generic ERROR+ log line almost never carries
    # a real `agent_id`, see `_render_error`'s own docstring for why
    # that's an accepted, honest default rather than a claim of cause).
    # 2026-10 plain-text reversal: no `<b>`/`<code>` tags.
    # 2026-10-31 — trailing action-agent/alert-agent tag appended by
    # `_send_channel` (see its own comment). No `agent_id`/`agent_name`
    # on this record's extra -> action agent "manual"; ERROR_AGENT's own
    # slug is "error-alert" (tag-token'd "error_alert") — no
    # `ALERT_EVENT_DISPATCH` entry for a bare error log line.
    # 2026-11-04 channel-routing audit — error-alert no longer sends
    # ntfy at all (alert_routing.py's own per-slug override), telegram
    # only; `sent["ntfy"]` is correctly empty now.
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("boom: bad value")], [error_agent])
    assert sent["ntfy"] == []
    assert sent["tg"] == [f"Agent: manual\nRamboQuant error\nbackend.x\nboom: bad value\n{_FIXED_TS}\n#manual #error_alert"]


@pytest.mark.asyncio
async def test_error_alert_strips_html_like_fragments_as_before(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("boom <bad>")], [error_agent])
    # 2026-11-04 — error-alert is telegram-only (see test_error_alert_golden_text).
    assert sent["tg"] == [f"Agent: manual\nRamboQuant error\nbackend.x\nboom\n{_FIXED_TS}\n#manual #error_alert"]


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
    # 2026-11-04 — error-alert is telegram-only (see test_error_alert_golden_text).
    assert sent["tg"] == [
        f"Agent: manual\nRamboQuant error\nbackend.x\ndown\n{_FIXED_TS}\n#manual #error_alert",
        f"Agent: manual\nRamboQuant error\nbackend.x\ndown (+1 repeats)\n{_FIXED_TS}\n#manual #error_alert",
    ]


@pytest.mark.asyncio
async def test_error_alert_masks_account_numbers(sent, error_agent):
    from backend.api.algo import event_agents
    await event_agents.dispatch([_error_rec("order failed for ZG0790.orders")], [error_agent])
    # 2026-11-04 — error-alert is telegram-only (see test_error_alert_golden_text).
    assert "ZG0790" not in sent["tg"][0]


def test_fill_format_golden():
    from datetime import timezone
    # No agent_name resolved (agent_id=None, the manual-order case) —
    # origin resolves to "manual", the same signal the Agents log's own
    # chip shows for a human-placed order (2026-10-17 fix: origin
    # reflects WHO triggered the order, not AlgoOrder.engine, which is
    # the execution mode and is "live" even for manual orders).
    #
    # 2026-10 audit fix: the ad-hoc "Source:"/"Time:" lines are replaced
    # by the shared `format_notification_header` — origin is now the
    # message's FIRST line (with its own dual-tz timestamp as line 2).
    row = SimpleNamespace(id=101, account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="NFO",
                          transaction_type="BUY", quantity=75, fill_price=112.5, product="NRML",
                          mode="live", engine="live")
    title, body = fill_notify.format_fill_message(
        row, when=_dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc))
    assert title == "Order filled: BUY 75 NIFTY26OCT25000CE"
    # 2026-10 audit fix: account is now masked — this is the
    # HIGHEST-VOLUME live notification (every fill, Telegram+ntfy), and
    # was previously the one unmasked account surface in the app.
    # 2026-11-05 live fix — `title` is now ALSO an explicit line in
    # `body` (right after the origin header), restoring "filled" to the
    # alert text once `tg=body` stopped relying on the old
    # `f"{title}\n{body}"` Telegram fallback to carry it.
    # Amended same day — operator: "remove symbol just after order
    # filled text" — the body-embedded summary line drops the symbol
    # (side+qty only); the symbol still appears once, on the
    # trade-detail line below.
    assert body == "\n".join([
        "Agent: manual",
        _FIXED_TS,
        "Order filled: BUY 75",
        "Account: ZG####",
        "BUY 75 NIFTY26OCT25000CE (NFO) @ 112.50",
        "Product: NRML",
        "Order id: 101",
    ])


def test_fill_format_golden_missing_fill_price_renders_dash_not_zero():
    """2026-10 audit fix: a genuinely-absent fill price (None) must
    render as "—", never a fabricated "0.00" — this codebase's own
    missing-vs-zero convention (CLAUDE.md)."""
    from datetime import timezone
    row = SimpleNamespace(id=103, account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="NFO",
                          transaction_type="BUY", quantity=75, fill_price=None, product="NRML",
                          mode="live", engine="live")
    _, body = fill_notify.format_fill_message(
        row, when=_dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc))
    assert "@ —" in body
    assert "0.00" not in body


def test_fill_format_golden_agent_triggered():
    from datetime import timezone
    row = SimpleNamespace(id=102, account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="NFO",
                          transaction_type="BUY", quantity=75, fill_price=112.5, product="NRML",
                          mode="live", engine="live", agent_name="Chase close positions")
    _, body = fill_notify.format_fill_message(
        row, when=_dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc))
    assert body.splitlines()[0] == "Agent: Chase close positions"


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
                          transaction_type="SELL", quantity=1, fill_price=8750.0, product="NRML",
                          mode="live", engine="live")
    await fill_notify.notify_fills([row])
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc), "level": "INFO",
           # 2026-10-25 tag-unification: FILL_AGENT's condition now matches
           # on "alert" (the real producer tags ["orders", "alert"] — see
           # `records[0]` captured from the real `notify_fills` call above).
           "tags": ["orders", "alert"], "extra": records[0]}
    agent = SimpleNamespace(slug=event_agents.FILL_AGENT["slug"], **{k: event_agents.FILL_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # No agent_id on `row` -> no agent_name resolved -> origin defaults to
    # "manual" (2026-10-17 fix, see test_fill_format_golden). Account
    # masked (2026-10 audit fix) — ZG0790 -> ZG####. 2026-10 audit fix:
    # origin ("Agent: manual") is now the FIRST line, its own dual-tz
    # timestamp the second — no more separate "Source:"/"Time:" lines.
    #
    # 2026-10-23 chase robustness (fix #4) — a fill is FYI, never
    # action-required: `notify_fills` now tags its log record
    # `action_required: False`, and `_apply_action_required_ntfy_gate`
    # skips the ntfy channel entirely for it, independent of whatever
    # FILL_AGENT's own stored `events` list says. Telegram is untouched.
    assert sent["ntfy"] == []
    # 2026-11-05 live fix — `_render_fill` now supplies `tg=body`
    # explicitly (out[2]), so Telegram sends `body` alone, not the old
    # `f"{title}\n{body}"` fallback that pushed the real "Agent: ..."
    # origin line down to line 2 behind a redundant "Order filled: ..."
    # title line. `format_fill_message` now puts that same title text
    # back INSIDE `body` (right after the origin header) so "filled"
    # still reaches the alert text, just one line lower than before.
    # 2026-10-31 — trailing tag: action agent "manual" (no agent_id on
    # `row`), alert agent "fill-alert" (FILL_AGENT's slug, tag-token'd),
    # on its own line (2026-11-05 amendment — see _send_channel).
    assert sent["tg"] == [
        f"Agent: manual\n{_FIXED_TS}\nOrder filled: SELL 1\nAccount: ZG####\nSELL 1 CRUDEOIL26OCTFUT (MCX) @ 8750.00\nProduct: NRML\nOrder id: 7"
        "\n#manual #fill_alert"]


def _chase_rec():
    from datetime import timezone as _tz
    # 2026-10-25 tag-unification: CHASE_CANCEL_AGENT's condition now
    # matches on "alert" — the real producer (`chase.py`) tags
    # ["chase", "alert"].
    return {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": "CRITICAL",
            "logger": "backend.api.algo.chase", "message": "Chase NIFTY: ...", "tags": ["chase", "alert"],
            "extra": {"tags": ["chase", "alert"], "alert_event": "cancel_unconfirmed", "transaction_type": "BUY",
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
                            **{k: event_agents.CHASE_CANCEL_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([_chase_rec()], [agent])
    # 2026-10 audit fix: the old "after attempt 2/40 filled." wording
    # read like a fraction (attempt N of M attempts) when the
    # denominator was actually "contracts already filled" — unrelated
    # numbers. Now spelled out unambiguously.
    #
    # 2026-10 audit fix (origin header): leads with "Agent: manual" (no
    # `agent_id` in this record's extra) + the shared dual-tz timestamp,
    # replacing the old standalone trailing timestamp line.
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # CHASE_CANCEL_AGENT's own slug "chase-cancel-alert" (also this
    # record's own `ALERT_EVENT_DISPATCH["cancel_unconfirmed"]
    # ["source_slug"]`, tag-token'd).
    assert sent == [(
        "Chase cancel unconfirmed — possible resting duplicate order",
        f"Agent: manual\n{_FIXED_TS}\n"
        "BUY NIFTY26OCTFUT — cancel of order O1 on ZG#### could not be confirmed after attempt "
        "2 (40 contracts filled so far). "
        "The chase has been ABORTED without placing a replacement order. "
        "Manually verify the broker's order book — the old order may still be live."
        "\n#manual #chase_cancel_alert",
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
    # 2026-10-25 tag-unification: PARTIAL_GTT_AGENT's condition now
    # matches on "alert" — the real producer (`orders_place.py`) tags
    # ["orders", "gtt", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": "CRITICAL",
           "logger": "backend.api.routes.orders_place", "message": "PARTIAL GTT", "tags": ["gtt", "alert"],
           "extra": {"tags": ["orders", "gtt", "alert"], "alert_event": "partial_gtt", "parent_row_id": 1088,
                     "parent_symbol": "CRUDEOIL26OCTFUT", "planned": 3, "placed": 1,
                     "errors": ["rate limit", "invalid price"]}}
    agent = SimpleNamespace(slug="partial-gtt-alert",
                            **{k: event_agents.PARTIAL_GTT_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # 2026-10 audit fix: origin header ("Agent: manual" — no `agent_id`
    # in this record's extra) leads, replacing the old standalone
    # trailing timestamp line.
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # "partial-gtt-alert" (tag-token'd).
    assert sent == [("Partial GTT placement",
                     f"Agent: manual\n{_FIXED_TS}\nparent #1088 CRUDEOIL26OCTFUT: 1/3 GTTs placed. Errors: rate limit; invalid price"
                     "\n#manual #partial_gtt_alert",
                     "urgent")]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_key, event, level, extra, expected", [
    # 2026-10 audit fix: these 4 now lead with the origin header
    # ("Agent: manual" — no `agent_id` in any of these fixtures — plus
    # the dual-tz timestamp), replacing the old trailing bare timestamp.
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # TEMPLATE_ATTACH_URGENT_AGENT's own `ALERT_EVENT_DISPATCH` source
    # slug "template-attach-urgent" (tag-token'd), for both events.
    ("TEMPLATE_ATTACH_URGENT_AGENT", "wing_unprotected", "WARNING",
     {"gtt_ids_text": "['G1', 'G2']", "reason": "no candidate", "parent_order_id": 7,
      "symbol": "NIFTY", "exchange": "NFO"},
     ("Unprotected SELL position",
      f"Agent: manual\n{_FIXED_TS}\nGTTs placed (ids: ['G1', 'G2']) but wing failed: no candidate | order #7 NIFTY NFO\n#manual #template_attach_urgent", "urgent")),
    ("TEMPLATE_ATTACH_URGENT_AGENT", "wing_hard_reject", "CRITICAL",
     {"reason": "premium too high", "symbol": "NIFTY", "exchange": "NFO", "target_premium": 12.5},
     ("Wing scan hard-rejected", f"Agent: manual\n{_FIXED_TS}\npremium too high | NIFTY NFO target ₹12.50\n#manual #template_attach_urgent", "urgent")),
    # 2026-10-28 noise tightening removed ntfy from TEMPLATE_ATTACH_HIGH_AGENT
    # entirely (an FYI "leg skipped" notice, not action-required) — its
    # wing_skip/wing_offset_skip rows are gone from this ntfy-golden-text
    # parametrize list, since there is no longer any ntfy send to pin here.
    # See test_alert_routing.py's oracle entry for the channel-set change,
    # and test_alert_channel_collapse.py for the telegram-only text pin.
])
async def test_template_attach_golden_text_and_priority(monkeypatch, agent_key, event, level, extra, expected):
    from datetime import timezone as _tz
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent.append((t, b, priority))))
    # 2026-10-25 tag-unification: TEMPLATE_ATTACH_URGENT_AGENT/
    # TEMPLATE_ATTACH_HIGH_AGENT's conditions now match on "alert" — the
    # real producer (`template_attach.py`) tags ["orders", "gtt", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_tz.utc), "level": level,
           "logger": "backend.api.algo.template_attach", "message": "x", "tags": ["gtt", "alert"],
           "extra": {"tags": ["orders", "gtt", "alert"], "alert_event": event, **extra}}
    spec = getattr(event_agents, agent_key)
    agent = SimpleNamespace(slug=spec["slug"], **{k: spec.get(k, []) for k in ("conditions", "events", "actions")})
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
    # 2026-10-25 tag-unification: ORDER_FAILURE_AGENT's condition now
    # matches on "alert" — the real producer (`alert_utils.py`) tags
    # ["orders", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "logger": "backend.shared.helpers.alert_utils", "message": "x", "tags": ["orders", "alert"],
           "extra": {"tags": ["orders", "alert"], "alert_event": "order_failure", "masked": "ZG####",
                     "symbol": "NIFTY26OCTFUT", "exchange": "NFO", "side": "BUY", "qty": 75,
                     "mode": "live", "source": "ticket", "error": "Insufficient funds <x>",
                     "suppressed_count": 2, "ist_disp": "10:15:30 IST", "branch": "main"}}
    agent = SimpleNamespace(slug="order-failure-alert",
                            **{k: event_agents.ORDER_FAILURE_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # 2026-10 fix: every order-failure alert is now prefixed with its
    # origin label (Manual/Manual Bracket/Agent/Agent Bracket) ahead of
    # the mode tag — this record's `extra` has no "agent_id" key (the
    # shape persisted logs had before that fix), so it correctly
    # defaults to "Manual" (see alert_utils._classify_order_origin_label).
    # 2026-10 audit fix: the shared origin header ("Agent: manual" + the
    # dual-tz timestamp passed straight through via `ist_display=`) now
    # leads the message as its own first two lines — moved from line 2
    # (after the "Order rejected" title) to line 1.
    #
    # 2026-10 plain-text reversal: no `<b>`/`<code>`/entity-escaping —
    # telegram and ntfy now carry the BYTE-IDENTICAL plain `tg_body`
    # (order_failure_messages() builds one plain string, used directly
    # for both slots; `_render_order_failure` no longer calls
    # `_html_to_plain`).
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # "order-failure-alert" (tag-token'd); email body SHA re-derived
    # (now includes the trailing pill row).
    assert sent["tg"] == ['Agent: manual\n10:15:30 IST\n❌ Order rejected  [Manual]  [LIVE]  (+2 suppressed)\nZG####  BUY  75  NIFTY26OCTFUT  (NFO)\nsource: ticket\nInsufficient funds <x>\n#manual #order_failure_alert']
    assert sent["ntfy"] == [("Order Rejected: NIFTY26OCTFUT BUY",
                             "Agent: manual\n10:15:30 IST\n❌ Order rejected  [Manual]  [LIVE]  (+2 suppressed)\nZG####  BUY  75  NIFTY26OCTFUT  (NFO)\nsource: ticket\nInsufficient funds <x>\n#manual #order_failure_alert",
                             "urgent")]
    assert len(sent["mail"]) == 1
    _, addr, subj, body = sent["mail"][0]
    assert addr == "a@x.com"
    assert subj == "[Manual] RamboQuant Order Rejected: NIFTY26OCTFUT BUY (live)"
    assert hashlib.sha256(body.encode()).hexdigest() == "ab35cac9e1cf6a3816f71fb25b4def548fa1d4ea4c58f716c9b1972a0dc64492"


@pytest.mark.asyncio
async def test_template_guard_golden_text_and_channels(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.mail_utils as mu
    sent = {"tg": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append(tg)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    # 2026-10-25 tag-unification: TEMPLATE_GUARD_AGENT's condition now
    # matches on "alert" — the real producer (`template_attach.py`) tags
    # ["orders", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.algo.template_attach", "message": "x", "tags": ["info", "orders", "alert"],
           "extra": {"tags": ["orders", "alert"], "alert_event": "template_guard", "template_slug": "default-bull",
                     "applies_to": "sell_option", "reason": "qty < lot_size", "parent_order_id": 1088,
                     "parent_side": "SELL", "parent_qty": 75, "parent_symbol": "NIFTY26OCTFUT",
                     "parent_fill_price": 112.5, "parent_account": "ZG0790",
                     "ist_label": "Tue, Oct 06 2026, 14:30 IST"}}
    agent = SimpleNamespace(slug="template-guard-alert",
                            **{k: event_agents.TEMPLATE_GUARD_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # Account masked (2026-10 audit fix) — ZG0790 -> ZG####.
    # 2026-10 audit fix: origin header ("Agent: manual" — no agent_id in
    # this record's extra) now leads every template-guard alert.
    # 2026-10 plain-text reversal — no `<b>`/`<code>` tags or escaping;
    # telegram and ntfy carry the byte-identical plain `tg`.
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # "template-guard-alert" (tag-token'd).
    expected_tg = ("Agent: manual\n⚠ Template guard fired — Tue, Oct 06 2026, 14:30 IST\n\norder #1088\nSELL 75 NIFTY26OCTFUT\n"
                   "@ ₹112.50  (ZG####)\n\ntemplate:    default-bull\napplies_to:  sell_option\nreason:      qty < lot_size\n\n"
                   "Parent order FILLED. Exits NOT attached.\nArm exits manually if needed.\n\n"
                   "Fix: at /admin/templates, change this template's 'applies_to' to 'both' or 'buy_option'."
                   "\n#manual #template_guard_alert")
    assert sent["tg"] == [expected_tg]
    assert sent["ntfy"] == [("Template guard fired", expected_tg, "high")]
    # 2026-10 audit fix (item 10): TEMPLATE_GUARD_AGENT now also delivers
    # to email — "durable record for end-of-day review" per
    # template_attach.py:_fire_guard_alert's own docstring.
    assert len(sent["mail"]) == 1
    _, addr, subj, body = sent["mail"][0]
    assert addr == "a@x.com"
    assert "Template guard fired" in subj
    assert "ZG####" in body and "ZG0790" not in body


@pytest.mark.asyncio
async def test_template_attach_fail_golden_text_and_channels(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.mail_utils as mu
    sent = {"tg": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append(tg)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    # 2026-10-25 tag-unification: TEMPLATE_ATTACH_FAIL_AGENT's condition
    # now matches on "alert" — the real producer (`template_attach.py`)
    # tags ["orders", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, 0, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "logger": "backend.api.algo.template_attach", "message": "x", "tags": ["warning", "orders", "alert"],
           "extra": {"tags": ["orders", "alert"], "alert_event": "template_attach_fail", "order_id": 1088,
                     "symbol": "NIFTY26OCTFUT", "account": "ZG0790",
                     "err_summary": "qty < lot_size; rate limit", "ist_label": "Tue, Oct 06 2026, 14:30 IST"}}
    agent = SimpleNamespace(slug="template-attach-fail-alert",
                            **{k: event_agents.TEMPLATE_ATTACH_FAIL_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # Account masked (2026-10 audit fix) — ZG0790 -> ZG####.
    # 2026-10 audit fix: origin header ("Agent: manual" — no agent_id in
    # this record's extra) now leads every template-attach-fail alert.
    # 2026-10 plain-text reversal — no `<b>`/`<code>` tags or escaping.
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # "template-attach-fail-alert" (tag-token'd).
    expected_tg = ("Agent: manual\n⚠ Template attach failed — Tue, Oct 06 2026, 14:30 IST\n\norder #1088\nsymbol:   NIFTY26OCTFUT\naccount:  ZG####\n\n"
                   "errors:   qty < lot_size; rate limit\n\nParent order FILLED. Exits NOT attached.\nArm exits manually if needed."
                   "\n#manual #template_attach_fail_alert")
    assert sent["tg"] == [expected_tg]
    assert sent["ntfy"] == [("Template attach failed", expected_tg, "urgent")]
    # 2026-10 audit fix (item 10): TEMPLATE_ATTACH_FAIL_AGENT now also
    # delivers to email — same missing backstop as TEMPLATE_GUARD_AGENT.
    assert len(sent["mail"]) == 1
    _, addr, subj, body = sent["mail"][0]
    assert addr == "a@x.com"
    assert "Template attach failed" in subj
    assert "ZG####" in body and "ZG0790" not in body


@pytest.mark.asyncio
async def test_mcp_ping_sends_recorded_html_to_telegram_only(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = []
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent.append(tg)))
    tg = "<b>MCP CANCEL [LIVE]</b> order_id=<code>O1</code>\nacct=ZG####"
    # 2026-10-25 tag-unification: MCP_PING_AGENT's condition now matches
    # on "alert" — the real producer (`routes/lab.py`) tags
    # ["mcp", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.routes.lab", "message": "MCP ping", "tags": ["info", "mcp", "alert"],
           "extra": {"tags": ["mcp", "alert"], "alert_event": "mcp_ping", "tg": tg}}
    agent = SimpleNamespace(slug="mcp-ping-alert",
                            **{k: event_agents.MCP_PING_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # 2026-10-31 — trailing tag appended to `tg` (non-empty here):
    # action agent "manual", alert agent "mcp-ping-alert" (tag-token'd).
    assert sent == [tg + "\n#manual #mcp_ping_alert"]


@pytest.mark.asyncio
async def test_user_registered_agent_sends_telegram_and_ntfy_high(monkeypatch):
    """2026-10 operator-reported gap: new-user self-registration never
    reached Telegram/ntfy because `routes/auth.py`'s log line carried no
    alert-tag metadata at all (not a condition-matching failure — there
    was nothing for any event agent to match against). Confirms the
    full round trip: the real tagged record `auth.py` now emits ->
    `USER_REGISTERED_AGENT`'s condition matches -> the shared
    `generic_notice` renderer -> telegram (HTML) + ntfy (priority
    'high', not 'urgent' — not time-critical).

    Builds `extra` from the REAL producer helper
    (`routes.auth._registration_alert_extra`), not a hand-typed copy —
    a typo in the real `alert_event`/`tags` string would make this test
    fail instead of silently passing against a stale duplicate."""
    from backend.api.algo import event_agents
    from backend.api.routes.auth import _registration_alert_extra
    import backend.shared.helpers.utils as u
    sent = {"tg": [], "ntfy": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["tg"].append(tg or b)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    extra = _registration_alert_extra("newtrader", "newtrader@example.com")
    body = "newtrader (newtrader@example.com) registered and is pending admin approval."
    assert extra["body"] == body, "fixture sanity: no escaping needed for this plain username/email"
    # 2026-10-25 tag-unification: USER_REGISTERED_AGENT's condition now
    # matches on "alert" — the real producer (`_registration_alert_extra`,
    # captured into `extra` above) tags ["auth", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.routes.auth", "message": "x", "tags": ["info", "auth", "alert"],
           "extra": extra}
    agent = SimpleNamespace(slug="user-registered-alert",
                            **{k: event_agents.USER_REGISTERED_AGENT.get(k, [])
                               for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # "user-registered-alert" (tag-token'd).
    tagged_body = body + "\n#manual #user_registered_alert"
    assert sent["tg"] == [tagged_body]
    assert sent["ntfy"] == [("New user registered", tagged_body, "high")]


def test_registration_alert_extra_does_not_escape_username_and_email():
    """2026-10 plain-text reversal — inverts the prior escaping fix.
    `generic_notice`'s `body` is plain text now (no HTML parse mode, no
    `_html_to_plain` pass downstream), so escaping would show literal
    `&lt;`/`&amp;` text in the notification instead of the real
    username/email. A literal `<`/`>`/`&` in user-supplied input
    displays safely and verbatim in plain text."""
    from backend.api.routes.auth import _registration_alert_extra
    extra = _registration_alert_extra("bad<script>", "x@y.com&z")
    assert "bad<script>" in extra["body"]
    assert "x@y.com&z" in extra["body"]
    assert "&lt;" not in extra["body"]
    assert "&amp;" not in extra["body"]


@pytest.mark.asyncio
async def test_deploy_sync_reaches_only_the_dedicated_telegram_deploy_channel(monkeypatch):
    """2026-10 operator correction: a deploy-out-of-sync notice must
    reach "the telegram code deployment alerts channel alone" — a
    DEDICATED Telegram bot/chat via the new `telegram_deploy` channel
    (`event_agents.py:_send_telegram_deploy_html`), never the generic
    `telegram` channel, never ntfy, never email. Supersedes the prior
    "zero configured channels" design (this file's previous
    `test_deploy_sync_no_longer_sends_to_ntfy`) — ntfy/email are still
    correctly absent, but the agent is no longer silent.

    2026-10 routing-tree plan: `DEPLOY_SYNC_AGENT`'s own dict carries no
    `events` key at all (channel selection lives entirely in
    `alert_routing.py`'s per-slug override table) — the real invariant
    checked here is "the RESOLVED channel list for this agent's slug is
    exactly `[telegram_deploy]`", via the same `alert_routing.
    resolve_channels()` call `_dispatch_agent` itself makes."""
    from backend.api.algo import alert_routing, event_agents
    import backend.shared.helpers.utils as u
    sent = {"telegram_deploy": [], "telegram": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram_deploy",
                        ("telegram_deploy", lambda t, b, tg=None, **k: sent["telegram_deploy"].append(tg or f"{t}\n{b}")))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["telegram"].append(tg)))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append((t, b, priority))))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    # 2026-10-25 tag-unification: DEPLOY_SYNC_AGENT's condition now
    # matches on "alert" — the real producer (`background.py`) tags
    # ["deploy", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "logger": "backend.api.background", "message": "x", "tags": ["warning", "deploy", "alert"],
           "extra": {"tags": ["deploy", "alert"], "alert_event": "deploy_out_of_sync",
                     "title": "Deploy out of sync — main", "body": "Local HEAD abc12345 != origin/main"}}
    agent = SimpleNamespace(slug="deploy-sync-alert",
                            **{k: event_agents.DEPLOY_SYNC_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    resolved = alert_routing.resolve_channels(
        name="deploy-sync-alert", priority=event_agents.DEPLOY_SYNC_AGENT.get("priority"),
        agent_events=event_agents.DEPLOY_SYNC_AGENT.get("events", []),
    )
    assert resolved == [{"channel": "telegram_deploy", "enabled": True, "gate": False}]
    sent_count = await event_agents.dispatch([rec], [agent])
    # `_render_deploy_sync` always sets its 3rd tuple element (`tg`) to
    # the raw `body` (pre-existing behavior, unchanged by this fix) —
    # `_send_telegram_deploy_html`'s `tg or f"{title}\n{body}"` fallback
    # therefore uses `tg` directly here, so the title is not duplicated
    # into the message.
    # 2026-10-31 — trailing tag appended to `tg`: action agent "manual",
    # alert agent "deploy-sync-alert" (tag-token'd).
    assert sent["telegram_deploy"] == ["Local HEAD abc12345 != origin/main\n#manual #deploy_sync_alert"]
    assert sent["telegram"] == []
    assert sent["ntfy"] == []
    assert sent["mail"] == []
    assert sent_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name, sim, mode_tag, expected_title, expected_subject, expected_sha, expected_tg_head", [
    # 2026-10 plain-text reversal: no `<b>` wrap on the Telegram header line.
    # 2026-10-31 — trailing action-agent/alert-agent tag/pill (operator:
    # "add action agent name, and alert agent name in the alert") changes
    # the email body for every row below -> SHAs re-derived. Action agent
    # "manual" (no agent_id/agent_name in this fixture's extra); alert
    # agent "agent-alert-rich" (RICH_ALERT_AGENT's own slug — "rich_alert"
    # has no `ALERT_EVENT_DISPATCH` entry). live/paper share one SHA
    # (mode_tag affects only subject/tg, not body) as they did before;
    # sim's differs (its own SIMULATOR banner), as it did before.
    #
    # 2026-11-04 — "Alert" -> "Agent" email-body relabel reversion
    # (operator alert-format audit: "some have alert label. some agent
    # label. some no label" — standardized back to "Agent:" everywhere,
    # see `_relabel_agent_label_to_alert()`'s own docstring). This
    # fixture is agent-LESS (no agent_name/agent_id in extra), so
    # Telegram title/body and the email SUBJECT are unaffected
    # (unchanged "Agent — ..." text below — this fixture's relabel
    # scope never touched them even during the brief 2026-11-02 "Alert"
    # period, confirmed by actually running the real fixed code) — only
    # the email BODY's bold prefix+timestamp paragraph round-trips back
    # to its original pre-2026-11-02 "Agent — ..." text, so only
    # `expected_sha` changes here, re-derived by actually running the
    # real fixed dispatch, not hand-computed.
    ("live", False, "", "Agent — 10:15:30 IST", "RamboQuant Agent: ZG0790 summary",
     "19d539e6f8c9733db178dacd5215746a659add4ec494be59fdfb436a8c324811", "Agent — 10:15:30 IST"),
    ("paper", False, "[PAPER]", "Agent — 10:15:30 IST", "RamboQuant Agent:  [PAPER]ZG0790 summary",
     "19d539e6f8c9733db178dacd5215746a659add4ec494be59fdfb436a8c324811", "Agent [PAPER] — 10:15:30 IST"),
    # 2026-10-31 fix (operator: "the alerts should show paper and
    # simulation mode explicitly ... wire it") — dispatch_payload() now
    # auto-derives the [SIM] bracket tag from sim_mode=True whenever the
    # caller passes an empty mode_tag (this fixture's "" is exactly that
    # case), instead of leaving it blank as before. Subject and TG head
    # updated to match.
    ("sim", True, "", "SIMULATOR Agent — 10:15:30 IST", "SIMULATOR RamboQuant Agent:  [SIM]ZG0790 summary",
     "52e04e178c4aabe195f2df865d4035e18a648e25407191b44884ff2f5da2f266", "SIMULATOR Agent [SIM] — 10:15:30 IST"),
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
                            **{k: event_agents.RICH_ALERT_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # 2026-10-16 fix — RICH_ALERT_AGENT's ntfy channel no longer
    # hardcodes priority="urgent" (that hardcode forced every single
    # alert through send_ntfy_alert's 3x-repeat-send behavior
    # regardless of the real triggering agent's tier — live incident:
    # a routine market-open-nse notification sent 3 ntfy pushes). This
    # fixture's `rec["extra"]` deliberately carries no `tier` (the
    # "pre-migration" shape this golden test locks in) and the
    # SimpleNamespace `agent` fixture here has no `.tier` attribute
    # either, so priority correctly falls through to None (no override
    # — send_ntfy_alert's own clock-based default would apply in
    # production; this test mocks that call, so None is exactly what's
    # recorded). See test_rich_alert_priority_resolved_from_tracked_tier
    # below for the fixed path WITH a tier present.
    assert sent["ntfy"][0][0] == expected_title and sent["ntfy"][0][2] is None
    assert sent["tg"][0].startswith(expected_tg_head)
    subj, body = sent["mail"][0][2], sent["mail"][0][3]
    assert subj == expected_subject
    assert hashlib.sha256(body.encode()).hexdigest() == expected_sha


@pytest.mark.asyncio
@pytest.mark.parametrize("tier, expected_priority", [
    ("critical", "urgent"),
    ("high", "high"),
    ("medium", "default"),
    ("low", "low"),
    ("info", "low"),
])
async def test_rich_alert_priority_resolved_from_tracked_tier(monkeypatch, tier, expected_priority):
    """2026-10-16 fix — a rich_alert record carrying the real triggering
    agent's `tier` (via _v2_send_rich_alert's logged extras) resolves
    ntfy priority from THAT tier, not RICH_ALERT_AGENT's own (always
    "medium") and not a hardcoded "urgent". Confirms the live incident
    (market-open-nse, tier="info", sent 3 ntfy pushes) is fixed:
    "info" now resolves to "low" — a single, non-urgent send."""
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    sent = {"ntfy": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: None))
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None: sent["ntfy"].append(priority)))
    monkeypatch.setattr(event_agents, "_channel_enabled", lambda cap: True)
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.algo.agent_engine", "message": "x", "tags": ["info", "agent"],
           "extra": {"tags": ["agent"], "alert_event": "rich_alert", "agent_slug": "market-open-nse",
                     "ist_display": "10:15:30 IST", "tg_table": "x", "email_table_html": "x",
                     "subject_detail": "x", "sim_mode": False, "mode_tag": "", "tier": tier}}
    agent = SimpleNamespace(slug="agent-alert-rich", tier="medium",
                            **{k: event_agents.RICH_ALERT_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    assert sent["ntfy"][0] == expected_priority, (
        f"tier={tier!r} should resolve ntfy priority to {expected_priority!r}, "
        f"got {sent['ntfy'][0]!r} (RICH_ALERT_AGENT's own tier='medium' must "
        f"never be used when a real tier is tracked on the record)")


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type, title, subject, sha", [
    # 2026-10-31 — trailing action-agent/alert-agent tag/pill changes the
    # `info` text and email body -> re-derived. Action agent "manual"
    # (no agent_id/agent_name in this fixture's extra); alert agent
    # SUMMARY_AGENT's own slug "market-summary" (tag-token'd
    # "market_summary" — "summary" has no `ALERT_EVENT_DISPATCH` entry).
    ("open", "Open Summary — 10:15:30 IST", "RamboQuant Open Summary: Summary — 10:15:30 IST",
     "239826c41d5b5156b18d5f4d59207aa54e4324708beb13ac33ec27b969a1b058"),
    ("close", "Close Summary — 10:15:30 IST", "RamboQuant Close Summary: Summary — 10:15:30 IST",
     "02452bbd76386cd0fc95799937d9d92c436e14be1ca07d20c1cd26af0eb99297"),
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
    # 2026-10-25 tag-unification: SUMMARY_AGENT's condition now matches
    # on "alert" — the real producer (`alert_utils.py`) tags
    # ["summary", "alert"].
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.shared.helpers.alert_utils", "message": "x", "tags": ["info", "summary", "alert"],
           "extra": {"tags": ["summary", "alert"], "alert_event": "summary", "msg_type": msg_type,
                     "ist_display": "10:15:30 IST", "tg_table": "Holdings  ZG####  ₹1,20,000\nPositions  ZG####  -₹300",
                     "email_table_html": "<table><tr><td>Holdings</td><td>120000</td></tr></table>",
                     "subject_detail": "Summary — 10:15:30 IST"}}
    spec = event_agents.SUMMARY_AGENT
    agent = SimpleNamespace(slug=spec["slug"], **{k: spec.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [agent])
    # 2026-10 plain-text reversal — no `<b>`/`<code>` tags.
    assert sent["info"] == [f"{title}\n\nHoldings  ZG####  ₹1,20,000\nPositions  ZG####  -₹300\n#manual #market_summary"]
    _, addr, subj, body = sent["mail"][0]
    assert subj == subject
    assert hashlib.sha256(body.encode()).hexdigest() == sha


# ═══════════════════════════════════════════════════════════════════════════
#  Sprint 6 — full round trip: events.dispatch() -> (grouped record(s)) ->
#  event_agents.dispatch() via BREACH_AGENT -> final telegram/ntfy/email
#  delivery. Golden-pinned with the module's frozen _FIXED_TS.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_breach_dispatch_default_group_golden_round_trip(monkeypatch):
    """No channel carries a custom `message` — events.dispatch() must emit
    exactly one grouped record, and the final telegram/ntfy/email delivery
    through BREACH_AGENT must be byte-identical to the pre-Sprint-6
    (ungrouped) shape. This covers the majority of production traffic —
    every existing threshold/loss-* agent."""
    from unittest.mock import AsyncMock
    from backend.api.algo import events, event_agents
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.mail_utils as mu

    monkeypatch.setattr(events, "config", {"deploy_branch": "main"})
    monkeypatch.setattr(events, "is_enabled", lambda cap: True)
    monkeypatch.setattr(events, "_log_event", AsyncMock())
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    sent = {"tg": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent["tg"].append(msg))
    monkeypatch.setattr(au, "send_ntfy_alert",
                        lambda title, message, priority=None: sent["ntfy"].append((title, message, priority)))

    firing_agent = SimpleNamespace(
        id=42, name="Account funds gone negative", slug="loss-funds", tier="critical",
        events=[{"channel": "telegram", "enabled": True},
                {"channel": "ntfy", "enabled": True},
                {"channel": "email", "enabled": True}],
    )
    eval_result = events.EvalResult(triggered=True, condition_text="cash < 0", detail={})

    info_calls = []
    monkeypatch.setattr(events.logger, "info", lambda msg, **kw: info_calls.append(kw.get("extra")))

    await events.dispatch(firing_agent, eval_result, sim_mode=False)

    assert len(info_calls) == 1, "messageless channels must share exactly one record"
    extra = info_calls[0]

    # Replay through the real BREACH_AGENT, the same way log_store.py's
    # flush_once() -> event_agents.dispatch_rows() would in production.
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "logger": "backend.api.algo.events", "message": "x", "tags": ["info", "agent"],
           "extra": extra}
    breach_agent = SimpleNamespace(slug=event_agents.BREACH_AGENT["slug"],
                                   **{k: event_agents.BREACH_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    await event_agents.dispatch([rec], [breach_agent])

    # 2026-10-31 — trailing tag: action agent "Account funds gone
    # negative" (the real firing agent's name, tag-token'd), alert agent
    # BREACH_AGENT's own slug "cycle-breach-alert" (tag-token'd) — a bare
    # breach record's `alert_event` has no `ALERT_EVENT_DISPATCH` entry.
    expected_body = (
        f"Alert — Account funds gone negative\n"
        f"Agent: Account funds gone negative (#42)\n{_FIXED_TS}\n"
        f"Condition: cash < 0"
        f"\n#account_funds_gone_negative #cycle_breach_alert"
    )
    assert sent["tg"] == [expected_body]
    assert sent["ntfy"] == [("Account funds gone negative", expected_body, "urgent")]
    assert len(sent["mail"]) == 1
    _, addr, subj, body = sent["mail"][0]
    assert addr == "a@x.com"
    assert subj == "RamboQuant Agent: Account funds gone negative"
    assert "<p><b>Condition:</b> cash < 0</p>" in body


@pytest.mark.asyncio
async def test_breach_dispatch_custom_message_channel_golden_round_trip(monkeypatch):
    """One channel (ntfy) carries a custom `message` — it must deliver
    THAT text, not condition_text, while telegram/email (messageless)
    still deliver the shared condition_text body."""
    from unittest.mock import AsyncMock
    from backend.api.algo import events, event_agents
    import backend.shared.helpers.utils as u
    import backend.shared.helpers.alert_utils as au
    import backend.shared.helpers.mail_utils as mu

    monkeypatch.setattr(events, "config", {"deploy_branch": "main"})
    monkeypatch.setattr(events, "is_enabled", lambda cap: True)
    monkeypatch.setattr(events, "_log_event", AsyncMock())
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(au, "get_alert_recipients", lambda: ["a@x.com"])
    sent = {"tg": [], "ntfy": [], "mail": []}
    monkeypatch.setattr(mu, "send_email", lambda *a, **k: sent["mail"].append(a))
    monkeypatch.setattr(au, "_send_telegram", lambda msg, **k: sent["tg"].append(msg))
    monkeypatch.setattr(au, "send_ntfy_alert",
                        lambda title, message, priority=None: sent["ntfy"].append((title, message, priority)))

    firing_agent = SimpleNamespace(
        id=42, name="Account funds gone negative", slug="loss-funds", tier="critical",
        events=[{"channel": "telegram", "enabled": True},
                {"channel": "email", "enabled": True},
                {"channel": "ntfy", "enabled": True, "message": "Cash is deeply negative — act now"}],
    )
    eval_result = events.EvalResult(triggered=True, condition_text="cash < 0", detail={})

    info_calls = []
    monkeypatch.setattr(events.logger, "info", lambda msg, **kw: info_calls.append(kw.get("extra")))

    await events.dispatch(firing_agent, eval_result, sim_mode=False)

    assert len(info_calls) == 2
    breach_agent = SimpleNamespace(slug=event_agents.BREACH_AGENT["slug"],
                                   **{k: event_agents.BREACH_AGENT.get(k, []) for k in ("conditions", "events", "actions")})
    for extra in info_calls:
        rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
               "logger": "backend.api.algo.events", "message": "x", "tags": ["info", "agent"],
               "extra": extra}
        await event_agents.dispatch([rec], [breach_agent])

    # 2026-10-31 — same trailing tag as the default-group test above,
    # appended to both the shared and the custom-message body.
    _tag = "\n#account_funds_gone_negative #cycle_breach_alert"
    shared_body = (
        f"Alert — Account funds gone negative\n"
        f"Agent: Account funds gone negative (#42)\n{_FIXED_TS}\n"
        f"Condition: cash < 0"
        f"{_tag}"
    )
    custom_body = (
        f"Alert — Account funds gone negative\n"
        f"Agent: Account funds gone negative (#42)\n{_FIXED_TS}\n"
        f"Condition: Cash is deeply negative — act now"
        f"{_tag}"
    )
    assert sent["tg"] == [shared_body]
    assert sent["ntfy"] == [("Account funds gone negative", custom_body, "urgent")]
    assert len(sent["mail"]) == 1
    assert sent["mail"][0][2] == "RamboQuant Agent: Account funds gone negative"


# ═══════════════════════════════════════════════════════════════════════════
#  2026-11-02 — dispatch_payload() one-line agent-header merge (operator:
#  the two-line "Agent: {name} (#{id})" / "Agent — {ts}" header read as a
#  redundant duplicate — the word "Agent" literally repeated). The two
#  lines are merged into ONE pipe-separated line for the Telegram/ntfy
#  body (`telegram_msg`, shared verbatim by both channels via
#  `_render_rich_alert`/`_render_summary`'s `out[1] is out[2]`), AND the
#  email SUBJECT is reshaped the same way (operator, follow-up ask).
#
#  Word relabel, SCOPED to the merged line/subject AND the email BODY's
#  two header paragraphs (operator, second AND third follow-up asks):
#  the label reads "Alert:", not "Agent:", in the merged Telegram/ntfy
#  header line, the email subject, AND both paragraphs of the email
#  body (`agent_line_html` and the bold `tg_prefix_full` + ts
#  paragraph — see `TestEmailMessageSubjectReshapeAndBodyRelabel`
#  below for that coverage). `format_notification_header()` itself is
#  untouched, and every OTHER caller of it (order display's
#  `agent_origin` chip, fill/failure/chase-cancel renderers, etc.)
#  still says "Agent:" — a deliberate, separate origin-tracking SSOT
#  ("who placed this order: manual or which agent") that must never
#  say "Alert:".
#
#  Email body LAYOUT/STRUCTURE (two separate paragraphs, not merged
#  into one) is untouched — only the label WORD inside those two
#  existing paragraphs changes. See
#  `TestEmailMessageSubjectReshapeAndBodyRelabel` for the exact
#  before/after body text.
# ═══════════════════════════════════════════════════════════════════════════

class TestDispatchPayloadAgentHeaderOneLineMerge:
    """Class name kept for history — the header is TWO lines (since
    2026-11-03, operator: the one-line merge read too cramped) and the
    label word is "Agent:" (since 2026-11-04, operator alert-format
    audit — the brief 2026-11-02 "Agent" -> "Alert" relabel on this
    header was reverted so every renderer in the system agrees on one
    label; see `_relabel_agent_label_to_alert()`). Still exactly one
    agent mention and one timestamp, just split onto two lines for
    readability — not a reversion to the original duplicated
    "Agent: X" / "Agent — ts" shape (which repeated the word "Agent"
    on both lines)."""
    def test_known_agent_splits_identity_and_timestamp_onto_two_lines(self):
        """Confirmed target shape (reproduced by actually running the
        real fixed dispatch_payload() — not hand-computed): line 1 is
        'Agent: {name} (#{id})', line 2 is the bare dual-tz
        `ist_display` with no repeated label word."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(alert_utils, "config", {"deploy_branch": "main"})
            p = alert_utils.dispatch_payload(
                "alert",
                "Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST",
                "MCX pre-close — MCX closes in 30 minutes",
                "<p>MCX pre-close — MCX closes in 30 minutes</p>",
                "MCX pre-close (#93)",
                agent_name="MCX pre-close", agent_id=93,
            )
        assert p["telegram_msg"] == (
            "Agent: MCX pre-close (#93)\n"
            "Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST"
            "\n\nMCX pre-close — MCX closes in 30 minutes"
        )
        # Exactly one label token across the two header lines, not two,
        # and it is always the literal "Agent:" (2026-11-04 — no other
        # label word exists anywhere in the system any more).
        lines = p["telegram_msg"].splitlines()
        header = lines[0] + "\n" + lines[1]
        assert header.count("Agent") == 1
        assert "Alert" not in header
        assert "\nAgent —" not in p["telegram_msg"]
        # Email subject uses the same label — still the one-line
        # "Agent: {subject_detail} | {ist_display}" shape.
        assert p["email_subject"] == (
            "Agent: MCX pre-close (#93) | Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST"
        )

    def test_known_agent_sim_mode_keeps_bracket_tag_and_banner_on_two_lines(self):
        """The SIMULATOR/REPLAY *word* prefix is dropped from the header
        (pure redundancy once a real agent name is shown — see the
        comment in dispatch_payload), but the fact itself is not lost:
        the [SIM] bracket tag moves to the timestamp line, and the
        warning banner still renders right after it."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(alert_utils, "config", {"deploy_branch": "main"})
            p = alert_utils.dispatch_payload(
                "alert", "10:15:30 IST", "tg_table", "<p>x</p>", "subject",
                sim_mode=True, agent_name="loss-margin-low", agent_id=42,
            )
        assert p["telegram_msg"].startswith(
            "Agent: loss-margin-low (#42)\n[SIM] 10:15:30 IST"
        )
        assert "SIMULATOR RUN" in p["telegram_msg"]
        assert "\nAgent —" not in p["telegram_msg"]
        # Title construction (untouched by this fix) still carries the
        # SIMULATOR word and still says "SIMULATOR {name}" — only the
        # merged telegram/ntfy body line and the email subject get the
        # label/merge treatment; title is a separate, unrelated field
        # this fix does not touch.
        assert "SIMULATOR" in p["title"]
        # Email subject also carries [SIM] — see the dedicated PAPER-mode
        # regression test below for why this is asserted explicitly.
        assert p["email_subject"] == "Agent: subject [SIM] | 10:15:30 IST"

    def test_known_agent_paper_mode_subject_still_carries_mode_tag(self):
        """Regression guard: the known-agent email subject reshape must
        thread `mode_tag`/`branch_tag` through, exactly like the
        Telegram/ntfy merged line does. PAPER mode is the case that
        would otherwise disappear entirely from the email — unlike
        SIM/REPLAY, PAPER has no warning banner in the body, and the
        body's bold paragraph never carried `mode_tag` either, so the
        subject was PAPER's only surface anywhere in the email. Caught
        by actually running the real code and comparing before/after
        this fix — initial subject reshape dropped `mode_tag` outright,
        silently losing [PAPER] from the email."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(alert_utils, "config", {"deploy_branch": "main"})
            p = alert_utils.dispatch_payload(
                "alert", "10:15:30 IST", "tg_table", "<p>x</p>", "loss-margin-low (#42)",
                mode_tag="[PAPER]", agent_name="loss-margin-low", agent_id=42,
            )
        assert p["email_subject"] == "Agent: loss-margin-low (#42) [PAPER] | 10:15:30 IST"

    def test_known_agent_non_main_branch_subject_carries_branch_tag(self):
        """Same regression class as the PAPER-mode test above, for the
        non-main-branch `branch_tag` instead of `mode_tag`."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(alert_utils, "config", {"deploy_branch": "workshop"})
            p = alert_utils.dispatch_payload(
                "alert", "10:15:30 IST", "tg_table", "<p>x</p>", "loss-margin-low (#42)",
                agent_name="loss-margin-low", agent_id=42,
            )
        assert p["email_subject"] == "Agent: loss-margin-low (#42) [workshop] | 10:15:30 IST"

    def test_agentless_summary_branch_is_byte_identical_to_before(self):
        """No agent identity known (open/close summaries and any other
        agent-less dispatch) -> `telegram_msg` AND `email_subject` are
        completely unchanged by this fix: still the single
        generic-prefix shapes they always were, with no merge/relabel
        logic applied."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(alert_utils, "config", {"deploy_branch": "main"})
            p = alert_utils.dispatch_payload(
                "open", "09:14:22 IST", "tg_table", "<html></html>", "Summary — 09:14:22 IST",
            )
        assert p["telegram_msg"] == "Open Summary — 09:14:22 IST\n\ntg_table"
        assert p["email_subject"] == "RamboQuant Open Summary: Summary — 09:14:22 IST"

    def test_ntfy_body_is_the_same_string_as_telegram_msg(self):
        """`_render_rich_alert`'s `out[1]`/`out[2]` share one string —
        confirming this fix covers ntfy and Telegram identically with no
        separate ntfy-specific change required."""
        from backend.api.algo.event_agents import _render_rich_alert
        rec = {"extra": {
            "ist_display": "Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST",
            "tg_table": "MCX pre-close — MCX closes in 30 minutes",
            "email_table_html": "<p>x</p>", "subject_detail": "MCX pre-close (#93)",
            "agent_name": "MCX pre-close", "agent_id": 93,
        }}
        title, ntfy_body, tg_body, (email_subject, email_html) = _render_rich_alert(rec)
        assert ntfy_body == tg_body
        assert ntfy_body == (
            "Agent: MCX pre-close (#93)\n"
            "Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST"
            "\n\nMCX pre-close — MCX closes in 30 minutes"
        )
        assert email_subject == (
            "Agent: MCX pre-close (#93) | Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST"
        )


class TestEmailMessageSubjectReshapeAndBodyRelabel:
    """Focused coverage for the email SUBJECT reshape (2026-11-02,
    unchanged by the later reversion) AND the header-paragraph label
    word, which is "Agent:" as of 2026-11-04 (operator alert-format
    audit reverted the brief 2026-11-02 "Agent" -> "Alert" relabel so
    every renderer agrees). Layout/structure (two separate paragraphs,
    not merged) is unchanged throughout — only the subject's shape
    changed, and the label word is back to its original "Agent:".
    Verified by actually running the real `_email_message()`, not
    hand-computed."""

    def test_known_agent_subject_and_body_say_agent(self):
        from backend.shared.helpers.alert_utils import _email_message

        common = dict(
            branch_tag="", mode_tag="", sim_mode=False, branch="main",
            tg_prefix_full="Agent", ist_display="Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST",
            email_table_html="<p>x</p>",
        )
        subj_known, body_known = _email_message(
            email_prefix_full="RamboQuant Agent: ", subject_detail="MCX pre-close (#93)",
            agent_line="Agent: MCX pre-close (#93)", **common,
        )
        assert subj_known == (
            "Agent: MCX pre-close (#93) | Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST"
        )
        # Both header paragraphs say "Agent" — exact real-output body,
        # confirmed by running the real function.
        assert body_known == (
            "<html><body style='font-family:sans-serif;background-color:#0c1830;"
            "color:#c8d8f0;margin:0;padding:18px'><div style='max-width:760px;"
            "margin:0 auto'><p style='font-size:13px;color:#a3b9d0;margin:0 0 10px 0'>"
            "Agent: MCX pre-close (#93)</p><p style='font-size:14px;color:#fbbf24;"
            "letter-spacing:0.04em;margin:0 0 14px 0'><b>Agent — Mon 02 Nov 22:59 IST "
            "| Mon 02 Nov 12:29 EST</b></p><p>x</p></div></body></html>"
        )
        assert "Alert" not in body_known
        # Still two SEPARATE paragraphs — structure is not merged.
        assert body_known.count("<p") >= 3  # agent_line_html + bold ts + table

    def test_agentless_case_subject_unchanged_body_says_agent(self):
        """Agent-less (summary-shaped) case: subject keeps its prior
        shape exactly (operator's explicit scope boundary for the
        subject reshape), and the body's bold prefix+timestamp
        paragraph still says "Agent" (the relabel that used to change
        this word is now a no-op — see `_relabel_agent_label_to_alert`)."""
        from backend.shared.helpers.alert_utils import _email_message

        common = dict(
            branch_tag="", mode_tag="", sim_mode=False, branch="main",
            tg_prefix_full="Agent", ist_display="Mon 02 Nov 22:59 IST | Mon 02 Nov 12:29 EST",
            email_table_html="<p>x</p>",
        )
        subj_unknown, body_unknown = _email_message(
            email_prefix_full="RamboQuant Agent: ", subject_detail="MCX pre-close (#93)",
            agent_line="", **common,
        )
        assert subj_unknown == "RamboQuant Agent: MCX pre-close (#93)"
        assert body_unknown == (
            "<html><body style='font-family:sans-serif;background-color:#0c1830;"
            "color:#c8d8f0;margin:0;padding:18px'><div style='max-width:760px;"
            "margin:0 auto'><p style='font-size:14px;color:#fbbf24;"
            "letter-spacing:0.04em;margin:0 0 14px 0'><b>Agent — Mon 02 Nov 22:59 IST "
            "| Mon 02 Nov 12:29 EST</b></p><p>x</p></div></body></html>"
        )
        assert "Alert" not in body_unknown

    def test_summary_msg_type_prefix_never_contains_agent_word_unaffected(self):
        """Open/close summary prefixes ("Open Summary", "SIMULATOR
        Open Summary", ...) never contain the word "Agent" — the
        relabel helper (now a permanent no-op) was always a correct
        no-op for them either way, confirmed with the real
        summary-shaped `tg_prefix_full`."""
        from backend.shared.helpers.alert_utils import _email_message

        subj, body = _email_message(
            email_prefix_full="RamboQuant Open Summary: ", branch_tag="", mode_tag="",
            subject_detail="Summary — 09:14:22 IST", sim_mode=False, branch="main",
            tg_prefix_full="Open Summary", ist_display="09:14:22 IST",
            email_table_html="<p>x</p>", agent_line="",
        )
        assert subj == "RamboQuant Open Summary: Summary — 09:14:22 IST"
        assert "<b>Open Summary — 09:14:22 IST</b>" in body
        assert "Alert" not in body


class TestEmailAlertBodyTableOverflowFix:
    """2026-11-02 — operator-reported email table text overflowing its
    grid. `_email_alert_body()`'s data cells (`td`) had
    `white-space:nowrap` combined with the table's own `width:100%`,
    so any cell content longer than its column's available width
    pushed the table past its container. Headers (`th`) stay
    `nowrap` — short, fixed labels. Data cells now wrap."""

    def test_td_style_no_longer_nowrap_th_still_does(self):
        import re
        from backend.shared.helpers.alert_utils import _email_alert_body

        row = {
            'section': 'Positions', 'scope': 'ZG0790', 'kind': 'static_pct',
            'pnl': -1200.0, 'pct': -1.2, 'rate_val': None,
            'threshold': '-1.0%', 'underlyings_breakdown': [],
        }
        html = _email_alert_body([row])
        assert "<th style='" in html and "white-space:nowrap" in html.split("<td")[0]
        # Every <td ...> style attribute must NOT contain nowrap, and
        # must carry both wrap-safety properties.
        td_styles = re.findall(r"<td style='([^']*)'", html)
        assert td_styles, "expected at least one <td> in the rendered table"
        for style in td_styles:
            assert "white-space:nowrap" not in style
            assert "word-break:break-word" in style
            assert "overflow-wrap:break-word" in style
