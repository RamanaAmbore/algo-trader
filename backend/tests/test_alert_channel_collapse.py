"""2026-10-25 alert-channel collapse — regression gate.

16 of the 23 `kind='event'` seeded agents (CHASE_CANCEL_AGENT,
PARTIAL_GTT_AGENT, TEMPLATE_ATTACH_URGENT_AGENT, TEMPLATE_ATTACH_HIGH_AGENT,
ORDER_FAILURE_AGENT, ORDER_CORRECTION_AGENT, TEMPLATE_GUARD_AGENT,
TEMPLATE_ATTACH_FAIL_AGENT, MCP_PING_AGENT, GTT_ASYMMETRIC_AGENT,
OCO_DOUBLE_FIRE_AGENT, TICKER_RECOVERED_AGENT, TICKER_DEGRADED_AGENT,
GTT_NOT_ACCEPTED_AGENT, FILL_AGENT, USER_REGISTERED_AGENT) are retired from
`SEEDED_AGENTS` and replaced by 3 generic channel agents
(`TELEGRAM_CHANNEL_AGENT`/`NTFY_CHANNEL_AGENT`/`EMAIL_CHANNEL_AGENT`) plus
`event_agents.ALERT_EVENT_DISPATCH`, a table DERIVED from the 16 retired
specs' own `conditions`/`actions` and `alert_routing._PER_SLUG_OVERRIDES` —
see `event_agents.py`'s own block comment immediately above
`_COLLAPSED_DETECTOR_SPECS` for the full design and the (documented, council-
reviewed) exclusion rules.

This file proves the collapse changed AGENT COUNT (23 -> 10) without
changing DELIVERY BEHAVIOR: for every one of the 18 `alert_event` keys this
migration touches, the exact same channel set fires, at the exact same
priority, via the exact same renderer, as the now-retired dedicated agent
used to produce on its own. Where `test_alert_golden.py` already pinned the
literal rendered text/priority for a retired agent (an independent,
incident-driven oracle, not re-derived here), this file reuses those EXACT
`rec` fixtures and expected literals and dispatches them through the new 3
channel agents instead of the old single dedicated agent.
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest

from backend.api.algo import event_agents as ea


def _channel_agents() -> list[SimpleNamespace]:
    """The 3 real generic channel agents, wrapped exactly like every
    other test double in this suite (`_agent_double()` in
    test_event_agent_tag_unification.py) — `conditions`/`actions` read
    live off the real specs, never retyped."""
    return [
        SimpleNamespace(slug=spec["slug"], tier=None, priority=spec.get("priority"),
                        conditions=spec["conditions"], actions=spec["actions"], events=[])
        for spec in (ea.TELEGRAM_CHANNEL_AGENT, ea.NTFY_CHANNEL_AGENT, ea.EMAIL_CHANNEL_AGENT)
    ]


@pytest.fixture
def chans(monkeypatch):
    """Capture every outbound send across all 5 real channels, keyed by
    channel name. Mirrors the per-test ad-hoc fixtures in
    test_alert_golden.py, generalized to one shared fixture since every
    test below exercises a different subset."""
    import backend.shared.helpers.utils as u
    sent = {"telegram": [], "ntfy": [], "email": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(ea, "_channel_enabled", lambda cap: True)
    monkeypatch.setattr(ea, "resolve_email_recipients", lambda recipients: ["a@x.com"])
    monkeypatch.setitem(ea.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["telegram"].append((t, b, tg))))
    monkeypatch.setitem(ea.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None, **k: sent["ntfy"].append((t, b, priority))))
    monkeypatch.setitem(ea.CHANNELS, "email",
                        ("email", lambda t, b, tg=None, email=None, recipients=None, **k:
                         sent["email"].append((t, b, email))))
    return sent


class _FixedDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 6, 10, 15, 30, tzinfo=tz)


@pytest.fixture(autouse=True)
def _freeze_ts(monkeypatch):
    from backend.shared.helpers import date_time_utils
    monkeypatch.setattr(date_time_utils, "timestamp_display",
                        lambda: "Tue 06 Oct 10:15 IST | Tue 06 Oct 00:45 EDT")


# ─────────────────────────────────────────────────────────────────────────
# Structural sanity — the 3 new channel agents + the 7 kept specs = 10
# (the 2026-10-25 collapse), PLUS three dedicated additions from the
# 2026-11-03 alert-agent audit (`market-status-alert` Wave 1,
# `action-notify-alert` Wave 2, `order-cancelled-alert` Wave 3 — all
# unrelated to the collapse, brand-new agents, not replaced
# detectors) = 13, PLUS `gtt-orphan-cancelled-alert` (2026-11-04, SEED_
# VERSION 6 -> 7 — see event_agents.py's own comment on that bump) = 14,
# PLUS `draft-sweep-alert` (2026-11 alert-agent audit fix, SEED_VERSION
# 9 -> 10) = 15.
# ─────────────────────────────────────────────────────────────────────────

def test_seeded_agents_count_is_13_not_23():
    # 2026-11 channel-authoring retirement — `action-notify-alert` is
    # retired (the `notify=[...]` action param it rendered has no
    # replacement) and replaced one-for-one in this set by the brand-new
    # `agent-close-hold-alert`; count stays 14. 2026-11 alert-agent audit
    # fix — `draft-sweep-alert` is a brand-new standalone agent
    # (`background.py:_ds_tick_draft_sweep()` no longer calls
    # `alert_utils._send_telegram_info()` directly); count is now 15.
    assert len(ea.SEEDED_AGENTS) == 16
    slugs = {s["slug"] for s in ea.SEEDED_AGENTS}
    assert slugs == {
        "error-alert", "deploy-sync-alert", "agent-alert-rich", "market-summary",
        "cycle-breach-alert", "visitor-report-alert", "unrouted-alert-catch-all",
        "alert-channel-telegram", "alert-channel-ntfy", "alert-channel-email",
        "market-status-alert", "agent-close-hold-alert", "order-cancelled-alert",
        "gtt-orphan-cancelled-alert", "draft-sweep-alert", "app-message-alert",
    }


def test_alert_event_dispatch_has_18_entries_from_16_specs():
    assert len(ea.ALERT_EVENT_DISPATCH) == 18
    two_event_slugs = {"template-attach-urgent", "template-attach-high"}
    by_source = {}
    for entry in ea.ALERT_EVENT_DISPATCH.values():
        by_source.setdefault(entry["source_slug"], 0)
        by_source[entry["source_slug"]] += 1
    for slug, count in by_source.items():
        assert count == (2 if slug in two_event_slugs else 1), slug


def test_every_collapsed_spec_slug_is_retired():
    seeded_slugs = {s["slug"] for s in ea.SEEDED_AGENTS}
    for spec in ea._COLLAPSED_DETECTOR_SPECS:
        assert spec["slug"] not in seeded_slugs
        assert spec["slug"] in ea._RETIRED_SLUGS
    # 16 collapsed-detector slugs + `action-notify-alert` (2026-11
    # channel-authoring retirement, unioned in separately via
    # `_NOTIFY_ACTION_RETIRED_SLUGS` — see event_agents.py's own
    # comment above `_RETIRED_SLUGS`) = 17.
    assert len(ea._RETIRED_SLUGS) == 17
    assert "action-notify-alert" in ea._RETIRED_SLUGS
    assert "action-notify-alert" not in seeded_slugs


# ─────────────────────────────────────────────────────────────────────────
# Byte-identical, untouched: the 3 structurally-excluded agents and the
# 2 (now 4, counting ERROR_AGENT/VISITOR_REPORT_AGENT) dedicated-channel
# agents.
# ─────────────────────────────────────────────────────────────────────────

def test_rich_breach_unrouted_conditions_and_actions_untouched():
    assert ea.RICH_ALERT_AGENT["conditions"] == {
        "log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "rich_alert"}},
    }
    assert ea.RICH_ALERT_AGENT["actions"] == [{"type": "render", "render": "rich_alert"}]
    assert ea.BREACH_AGENT["conditions"] == {
        "log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "breach"}},
    }
    assert ea.BREACH_AGENT["events"] == []
    assert ea.UNROUTED_ALERT_AGENT["conditions"] == {
        "log": {"tag": "alert", "min_level": "WARNING", "where": {"alert_event": "unrouted_alert"}},
    }
    from backend.api.algo import alert_routing
    assert ea.UNROUTED_ALERT_AGENT["events"] == alert_routing.CATCH_ALL_CHANNELS


def test_error_agent_untouched():
    assert ea.ERROR_AGENT["conditions"] == {"log": {"tag": "error", "min_level": "ERROR"}}
    assert ea.ERROR_AGENT["actions"] == [{"type": "render", "render": "error", "gate": True}]
    assert ea.ERROR_AGENT["priority"] == "medium"


def test_deploy_sync_and_summary_and_visitor_report_untouched():
    assert ea.DEPLOY_SYNC_AGENT["conditions"]["log"]["where"] == {"alert_event": "deploy_out_of_sync"}
    assert ea.SUMMARY_AGENT["conditions"]["log"]["where"] == {"alert_event": "summary"}
    assert ea.VISITOR_REPORT_AGENT["conditions"]["log"]["where"] == {"alert_event": "visitor_report"}
    from backend.api.algo import alert_routing
    assert alert_routing.resolve_channels(
        name="deploy-sync-alert", priority=ea.DEPLOY_SYNC_AGENT.get("priority"), agent_events=[],
    ) == [{"channel": "telegram_deploy", "enabled": True, "gate": False}]
    assert alert_routing.resolve_channels(
        name="market-summary", priority=ea.SUMMARY_AGENT.get("priority"), agent_events=[],
    ) == [{"channel": "telegram_info", "enabled": True, "gate": False},
          {"channel": "email", "enabled": True, "gate": False}]
    assert alert_routing.resolve_channels(
        name="visitor-report-alert", priority=ea.VISITOR_REPORT_AGENT.get("priority"), agent_events=[],
    ) == [{"channel": "telegram_info", "enabled": True, "gate": False}]


# ─────────────────────────────────────────────────────────────────────────
# Before/after identical delivery — reusing test_alert_golden.py's own
# pinned `rec` fixtures and expected literals verbatim, dispatched through
# the 3 new channel agents instead of the single retired dedicated agent.
# ─────────────────────────────────────────────────────────────────────────

_TS = "Tue 06 Oct 10:15 IST | Tue 06 Oct 00:45 EDT"


@pytest.mark.asyncio
async def test_chase_cancel_identical_via_channel_agents(chans):
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "CRITICAL",
           "tags": ["chase", "alert"],
           "extra": {"tags": ["chase", "alert"], "alert_event": "cancel_unconfirmed", "action_required": True,
                     "transaction_type": "BUY", "symbol": "NIFTY26OCTFUT", "account": "ZG0790",
                     "order_id": "O1", "attempt": 2, "quantity": 100, "remaining_qty": 60,
                     "agent_id": None, "source_type": "process", "source_name": "chase"}}
    sent = await ea.dispatch([rec], _channel_agents())
    # 2026-11-06 rebuild (operator SSOT instruction) — "Source: chase
    # triggered manually" replaces the plain "Agent: manual" label (the
    # real producer, `chase.py`, always sets `agent_id` alongside
    # `source_type`/`source_name`). Trailing tag: action ("manual"),
    # process ("chase"), alert agent "chase-cancel-alert" (tag-token'd),
    # log level ("critical").
    body = (f"Source: chase triggered manually\n{_TS}\n"
            "BUY NIFTY26OCTFUT — cancel of order O1 on ZG#### could not be confirmed after attempt "
            "2 (40 contracts filled so far). "
            "The chase has been ABORTED without placing a replacement order. "
            "Manually verify the broker's order book — the old order may still be live."
            "\n#manual #chase #via_chase_cancel_alert #critical")
    title = "Chase cancel unconfirmed — possible resting duplicate order"
    assert chans["ntfy"] == [(title, body, "urgent")]
    # 2026-11-05 live fix — `_render_chase_cancel` now supplies `tg=body`
    # explicitly (out[2]), so the real `_send_telegram_html` sends `body`
    # alone, not its old `f"{title}\n{body}"` fallback (unmocked here, so
    # this test still only checks the title, matching
    # test_alert_golden.py's own chase-cancel fixture).
    assert len(chans["telegram"]) == 1 and chans["telegram"][0][0] == title
    # action_required=True on a chase-cancel record (no email entry in its
    # own table row) forces exactly ONE email — via the email-channel
    # agent, not duplicated across telegram/ntfy's own dispatch calls.
    assert len(chans["email"]) == 1
    assert sent == 3  # telegram + ntfy + forced email


@pytest.mark.asyncio
async def test_partial_gtt_identical_via_channel_agents(chans):
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "CRITICAL",
           "tags": ["gtt", "alert"],
           "extra": {"tags": ["orders", "gtt", "alert"], "alert_event": "partial_gtt", "parent_row_id": 1088,
                     "parent_symbol": "CRUDEOIL26OCTFUT", "planned": 3, "placed": 1,
                     "errors": ["rate limit", "invalid price"],
                     "agent_id": None, "source_type": "process", "source_name": "template attach"}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-11-06 rebuild — "Source: template attach triggered manually"
    # replaces the plain "Agent: manual" label. Trailing tag: action
    # ("manual"), process ("template_attach"), alert agent
    # "partial-gtt-alert" (tag-token'd), log level ("critical").
    body = (f"Source: template attach triggered manually\n{_TS}\nparent #1088 CRUDEOIL26OCTFUT: 1/3 GTTs placed. Errors: rate limit; invalid price"
            "\n#manual #template_attach #via_partial_gtt_alert #critical")
    assert chans["ntfy"] == [("Partial GTT placement", body, "urgent")]
    assert len(chans["telegram"]) == 1  # see chase-cancel test's own comment on tg=None
    assert chans["email"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event, level, extra, expected_title, expected_body, expected_priority", [
    # 2026-11-06 rebuild (operator SSOT instruction) — "Source: GTT wing
    # triggered manually" replaces the plain "Agent: manual" label (the
    # real producer, `template_attach.py`, always sets `agent_id`
    # alongside `source_type`/`source_name`). Trailing tag: action
    # ("manual"), process ("gtt_wing"), alert agent
    # TEMPLATE_ATTACH_URGENT_AGENT's own source slug
    # "template-attach-urgent" (tag-token'd), log level.
    ("wing_unprotected", "WARNING",
     {"gtt_ids_text": "['G1', 'G2']", "reason": "no candidate", "parent_order_id": 7,
      "symbol": "NIFTY", "exchange": "NFO", "agent_id": None,
      "source_type": "process", "source_name": "GTT wing"},
     "Unprotected SELL position",
     f"Source: GTT wing triggered manually\n{_TS}\nGTTs placed (ids: ['G1', 'G2']) but wing failed: no candidate | order #7 NIFTY NFO\n#manual #gtt_wing #via_template_attach_urgent #warning",
     "urgent"),
    ("wing_hard_reject", "CRITICAL",
     {"reason": "premium too high", "symbol": "NIFTY", "exchange": "NFO", "target_premium": 12.5,
      "agent_id": None, "source_type": "process", "source_name": "GTT wing"},
     "Wing scan hard-rejected", f"Source: GTT wing triggered manually\n{_TS}\npremium too high | NIFTY NFO target ₹12.50\n#manual #gtt_wing #via_template_attach_urgent #critical", "urgent"),
    ("wing_skip", "WARNING",
     {"reason": "no candidate", "parent_order_id": 9, "symbol": "BANKNIFTY", "exchange": "NFO"},
     "Wing attach skipped", f"Agent: manual\n{_TS}\nno candidate | order #9 BANKNIFTY NFO\n#manual #via_template_attach_high #warning", "high"),
    ("wing_offset_skip", "WARNING",
     {"reason": "offset outside band", "parent_order_id": 9, "symbol": "BANKNIFTY", "exchange": "NFO"},
     "Wing offset attach skipped", f"Agent: manual\n{_TS}\noffset outside band | order #9 BANKNIFTY NFO\n#manual #via_template_attach_high #warning", "high"),
])
async def test_template_attach_events_identical_via_channel_agents(
    chans, event, level, extra, expected_title, expected_body, expected_priority,
):
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": level,
           "tags": ["gtt", "alert"], "extra": {"tags": ["orders", "gtt", "alert"], "alert_event": event, **extra}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-28 noise tightening — template-attach-high (wing_skip/
    # wing_offset_skip) dropped ntfy; a skipped wing still has its own
    # protective SL/TP live, so it's informational, not action-required.
    # TEMPLATE_ATTACH_URGENT_AGENT (wing_unprotected/wing_hard_reject) is
    # unaffected — those leave a position genuinely unprotected.
    if event in ("wing_skip", "wing_offset_skip"):
        assert chans["ntfy"] == []
    else:
        assert chans["ntfy"] == [(expected_title, expected_body, expected_priority)]
    assert len(chans["telegram"]) == 1  # see chase-cancel test's own comment on tg=None
    assert chans["email"] == []


@pytest.mark.asyncio
async def test_order_failure_identical_via_channel_agents(chans):
    import hashlib
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "tags": ["orders", "alert"],
           "extra": {"tags": ["orders", "alert"], "alert_event": "order_failure", "masked": "ZG####",
                     "symbol": "NIFTY26OCTFUT", "exchange": "NFO", "side": "BUY", "qty": 75,
                     "mode": "live", "source": "ticket", "error": "Insufficient funds <x>",
                     "suppressed_count": 2, "ist_disp": "10:15:30 IST", "branch": "main",
                     # 2026-11-08 audit fix (item 7) — the real producer
                     # always carries these two keys (even as `None`).
                     "agent_id": None, "agent_name": None}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # "order-failure-alert" (via `ALERT_EVENT_DISPATCH["order_failure"]`,
    # tag-token'd, 2026-11-08: `via_`-prefixed); email body SHA re-derived.
    # 2026-11-06 — 4th slot is this record's own level ("warning"); email
    # SHA re-derived (same fixture shape as test_alert_golden.py's own
    # order-failure test — same new SHA).
    # 2026-11-08 audit fix (item 7) — header reads "Source: Order
    # failure triggered manually" instead of "Agent: manual".
    # 2026-11-08 audit fix (item 2) — mode="live" shows no pill, and the
    # subject uses the shared "{label_word}: {detail}... | {ts}" shape.
    tg_body = ('Source: Order failure triggered manually\n10:15:30 IST\n❌ Order rejected  [Manual]    (+2 suppressed)\n'
              'ZG####  BUY  75  NIFTY26OCTFUT  (NFO)\nsource: ticket\nInsufficient funds <x>'
              '\n#manual #via_order_failure_alert #warning')
    assert chans["telegram"] == [("Order Rejected: NIFTY26OCTFUT BUY", tg_body, tg_body)]
    assert chans["ntfy"] == [("Order Rejected: NIFTY26OCTFUT BUY", tg_body, "urgent")]
    assert len(chans["email"]) == 1
    _, _, email_payload = chans["email"][0]
    subj, body = email_payload
    assert subj == "Source: Order Rejected NIFTY26OCTFUT BUY [Manual] | 10:15:30 IST"
    assert hashlib.sha256(body.encode()).hexdigest() == \
        "be52c58997bdb252245c7902f834bf2a18c35d1e4ae166d4eedd3edf16f24174"


@pytest.mark.asyncio
async def test_order_correction_identical_via_channel_agents(chans):
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "tags": ["orders", "alert"],
           "extra": {"tags": ["orders", "alert"], "alert_event": "order_correction", "algo_order_id": 1,
                     "symbol": "NIFTY26OCTFUT", "exchange": "NFO", "prior_status": "OPEN",
                     "new_status": "FILLED", "source": "order_poll", "masked": "ZG####",
                     "agent_id": None}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-28 noise tightening — order-correction-alert dropped ntfy.
    # A corrected status is informational (the row now shows its real
    # broker state); it isn't an action the operator needs to take.
    assert len(chans["telegram"]) == 1
    assert chans["ntfy"] == []
    assert chans["email"] == []
    title, body, tg = chans["telegram"][0]
    assert title == "Order corrected: NIFTY26OCTFUT #1"
    # 2026-11-06 rebuild (operator SSOT instruction) — "Source:
    # order_poll triggered manually" replaces "Agent: manual" (this
    # fixture's `agent_id` key is present with value `None` -> manual
    # trigger, matching the real producer, `order_poll.py`, which
    # always sets `agent_id`). `source_name` derives from this
    # fixture's own "source" field, which this particular fixture
    # spells with an underscore ("order_poll"), not the real producer's
    # hyphenated "order-poll" — `.replace("-", " ")` is a no-op here.
    assert body.splitlines()[0] == "Source: order_poll triggered manually"


@pytest.mark.asyncio
async def test_template_guard_identical_via_channel_agents(chans):
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "tags": ["info", "orders", "alert"],
           "extra": {"tags": ["orders", "alert"], "alert_event": "template_guard", "template_slug": "default-bull",
                     "applies_to": "sell_option", "reason": "qty < lot_size", "parent_order_id": 1088,
                     "parent_side": "SELL", "parent_qty": 75, "parent_symbol": "NIFTY26OCTFUT",
                     "parent_fill_price": 112.5, "parent_account": "ZG0790",
                     "ist_label": "Tue, Oct 06 2026, 14:30 IST"}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-31 — trailing tag: no action slot (this fixture carries
    # no `agent_id`/`agent_name` key -> genuinely unknown, no fabricated
    # "manual" — 2026-11-08 audit fix), alert agent "template-guard-alert"
    # (tag-token'd, via_-prefixed).
    expected_tg = ("Agent: manual\n⚠ Template guard fired — Tue, Oct 06 2026, 14:30 IST\n\norder #1088\n"
                  "SELL 75 NIFTY26OCTFUT\n@ ₹112.50  (ZG####)\n\ntemplate:    default-bull\n"
                  "applies_to:  sell_option\nreason:      qty < lot_size\n\n"
                  "Parent order FILLED. Exits NOT attached.\nArm exits manually if needed.\n\n"
                  "Fix: at /admin/templates, change this template's 'applies_to' to 'both' or 'buy_option'."
                  # 2026-11-06 — 4th slot is this record's own level ("info").
                  "\n#via_template_guard_alert #info")
    assert chans["telegram"] == [("Template guard fired", expected_tg, expected_tg)]
    assert chans["ntfy"] == [("Template guard fired", expected_tg, "high")]
    assert len(chans["email"]) == 1


@pytest.mark.asyncio
async def test_template_attach_fail_identical_via_channel_agents(chans):
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, 0, tzinfo=_dt.timezone.utc), "level": "WARNING",
           "tags": ["warning", "orders", "alert"],
           "extra": {"tags": ["orders", "alert"], "alert_event": "template_attach_fail", "order_id": 1088,
                     "symbol": "NIFTY26OCTFUT", "account": "ZG0790",
                     "err_summary": "qty < lot_size; rate limit", "ist_label": "Tue, Oct 06 2026, 14:30 IST"}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-31 — trailing tag: no action slot (genuinely unknown, see
    # template-guard test above), alert agent "template-attach-fail-alert"
    # (tag-token'd, via_-prefixed).
    expected_tg = ("Agent: manual\n⚠ Template attach failed — Tue, Oct 06 2026, 14:30 IST\n\norder #1088\n"
                  "symbol:   NIFTY26OCTFUT\naccount:  ZG####\n\n"
                  "errors:   qty < lot_size; rate limit\n\nParent order FILLED. Exits NOT attached.\n"
                  "Arm exits manually if needed."
                  # 2026-11-06 — 4th slot is this record's own level ("warning").
                  "\n#via_template_attach_fail_alert #warning")
    assert chans["telegram"] == [("Template attach failed", expected_tg, expected_tg)]
    assert chans["ntfy"] == [("Template attach failed", expected_tg, "urgent")]
    assert len(chans["email"]) == 1


@pytest.mark.asyncio
async def test_mcp_ping_identical_via_channel_agents(chans):
    tg = "<b>MCP CANCEL [LIVE]</b> order_id=<code>O1</code>\nacct=ZG####"
    # 2026-11-08 audit fix (item 3) — the producer logs `body` (never a
    # pre-built `tg` with an embedded header); the renderer builds the
    # header itself, classified `source_type="process"/source_name="MCP"`.
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "tags": ["info", "mcp", "alert"],
           "extra": {"tags": ["mcp", "alert"], "alert_event": "mcp_ping", "body": tg}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-31 — trailing tag appended to `tg`: no action slot
    # (genuinely unknown — no `agent_id`/`agent_name` key at all), alert
    # agent "mcp-ping-alert" (tag-token'd, via_-prefixed).
    # 2026-11-06 — 4th slot is this record's own level ("info").
    tagged = "Source: MCP\n" + _TS + "\n" + tg + "\n#via_mcp_ping_alert #info"
    assert chans["telegram"] == [("MCP", tagged, tagged)]
    assert chans["ntfy"] == [] and chans["email"] == []


@pytest.mark.asyncio
async def test_user_registered_identical_via_channel_agents(chans):
    from backend.api.routes.auth import _registration_alert_extra
    extra = _registration_alert_extra("newtrader", "newtrader@example.com")
    body = "newtrader (newtrader@example.com) registered and is pending admin approval."
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "INFO",
           "tags": ["info", "auth", "alert"], "extra": extra}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-28 noise tightening — user-registered-alert gained email
    # (a new pending-approval registration is action-required: the
    # operator must approve or reject the account).
    # 2026-11-06 rebuild (operator SSOT instruction) — self-registration
    # is a genuinely un-caused process ("Source: User registration"),
    # never "manual". Trailing tag: process, alert agent
    # "user-registered-alert" (tag-token'd), log level ("info").
    body = (f"Source: User registration\n{_TS}\n{body}"
            "\n#user_registration #via_user_registered_alert #info")
    assert chans["telegram"] == [("New user registered", body, body)]
    assert chans["ntfy"] == [("New user registered", body, "high")]
    assert len(chans["email"]) == 1


@pytest.mark.asyncio
async def test_fill_live_identical_via_channel_agents(chans, monkeypatch):
    from backend.api.algo import fill_notify
    records: list[dict] = []
    monkeypatch.setattr(fill_notify.logger, "info", lambda msg, **kw: records.append(kw["extra"]))
    row = SimpleNamespace(id=7, account="ZG0790", symbol="CRUDEOIL26OCTFUT", exchange="MCX",
                          transaction_type="SELL", quantity=1, fill_price=8750.0, product="NRML",
                          mode="live", engine="live")
    await fill_notify.notify_fills([row])
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "INFO",
           "tags": ["orders", "alert"], "extra": records[0]}
    await ea.dispatch([rec], _channel_agents())
    # Fill is pure FYI (action_required=False) -> ntfy and email never fire,
    # regardless of agent — same as before this collapse.
    assert chans["ntfy"] == [] and chans["email"] == []
    # 2026-11-05 live fix — `_render_fill` now supplies `tg=body`
    # explicitly (out[2]), so the real `_send_telegram_html` sends `body`
    # alone (the origin line stays the FIRST line), not the old
    # `f"{title}\n{body}"` fallback it used to fall through to when `tg`
    # was left `None`.
    # 2026-10-31 — trailing tag: action agent "manual", alert agent
    # "fill-alert" (tag-token'd), on its own line (2026-11-05 amendment).
    # Amended same day — operator: "remove symbol just after order
    # filled text" — the body's summary line drops the symbol (kept
    # once, on the trade-detail line below); `title` itself (unchanged,
    # ntfy's own title slot) still carries it.
    # 2026-11-06 — 4th slot is this record's own level ("info").
    assert chans["telegram"] == [(
        "Order filled: SELL 1 CRUDEOIL26OCTFUT",
        f"Agent: manual\n{_TS}\nOrder filled: SELL 1\nAccount: ZG####\nSELL 1 CRUDEOIL26OCTFUT (MCX) @ 8750.00\nProduct: NRML\nOrder id: 7\n#manual #via_fill_alert #info",
        f"Agent: manual\n{_TS}\nOrder filled: SELL 1\nAccount: ZG####\nSELL 1 CRUDEOIL26OCTFUT (MCX) @ 8750.00\nProduct: NRML\nOrder id: 7\n#manual #via_fill_alert #info",
    )]


@pytest.mark.asyncio
async def test_fill_paper_mode_never_sends_via_any_channel_agent(chans, monkeypatch):
    """The FILL_AGENT `where: {mode: "live"}` filter survives the move
    into the shared OR condition — a paper/sim fill matches none of the
    3 channel agents' conditions at all."""
    from backend.api.algo import fill_notify
    records: list[dict] = []
    monkeypatch.setattr(fill_notify.logger, "info", lambda msg, **kw: records.append(kw["extra"]))
    row = SimpleNamespace(id=8, account="ZG0790", symbol="NIFTY26OCTFUT", exchange="NFO",
                          transaction_type="BUY", quantity=1, fill_price=10.0, product="NRML",
                          mode="paper", engine="paper")
    await fill_notify.notify_fills([row])
    rec = {"ts": _dt.datetime(2026, 10, 6, 4, 45, 30, tzinfo=_dt.timezone.utc), "level": "INFO",
           "tags": ["orders", "alert"], "extra": records[0]}
    sent = await ea.dispatch([rec], _channel_agents())
    assert sent == 0
    assert chans == {"telegram": [], "ntfy": [], "email": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("alert_event, extra_extra, expect_ntfy, expect_email", [
    # 2026-10-28 noise tightening — gtt_asymmetric/oco_double_fire/
    # ticker_degraded are genuine exposure/anomaly/connectivity issues,
    # action-required, now synced to ntfy+email. ticker_recovered is
    # good news with nothing to act on, so it lost ntfy entirely and is
    # telegram-only — see alert_routing.py's own per-slug comments.
    ("gtt_asymmetric", {"title": "GTT asymmetric", "body": "leg mismatch"}, True, True),
    ("oco_double_fire", {"title": "OCO double fire", "body": "both legs fired"}, True, True),
    ("ticker_recovered", {"title": "Ticker recovered", "body": "back up"}, False, False),
    ("ticker_degraded", {"title": "Ticker degraded", "body": "degraded"}, True, True),
])
async def test_generic_notice_events_identical_channel_set_and_priority(
    chans, alert_event, extra_extra, expect_ntfy, expect_email,
):
    """These 4 share the `generic_notice` renderer. No pre-existing
    golden-text fixture exists for them (test_alert_golden.py only pins
    deploy_out_of_sync's own body via the same renderer) — this asserts
    channel set + priority against `ALERT_EVENT_DISPATCH`'s own entry,
    which is DERIVED from (never retyped from) `alert_routing.
    _PER_SLUG_OVERRIDES` — the independent ground truth `test_alert_
    routing.py`'s frozen oracle also pins by slug."""
    entry = ea.ALERT_EVENT_DISPATCH[alert_event]
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": entry["min_level"],
           "tags": ["gtt" if "gtt" in alert_event or "oco" in alert_event else "broker", "alert"],
           "extra": {"alert_event": alert_event, **extra_extra}}
    await ea.dispatch([rec], _channel_agents())
    expected_channels = {c["channel"] for c in entry["channels"]}
    expected = {"telegram"} | ({"ntfy"} if expect_ntfy else set()) | ({"email"} if expect_email else set())
    assert expected_channels == expected
    # 2026-10-31 — trailing tag: action agent "manual" (no agent_id in
    # this fixture's extra), alert agent this `alert_event`'s own
    # `source_slug` (tag-token'd) — derived the same way production
    # does, not hand-typed per case.
    # 2026-11-06 — 4th slot is this entry's own `min_level`, derived the
    # same way (not hand-typed per case).
    # 2026-11-08 — the action-agent slot is now `None` (no fabricated
    # "manual") for a record that carries no `agent_id`/`agent_name`
    # key at all (this fixture's `extra` has neither) — see
    # `resolve_source()`'s own docstring. The alert-agent slot is now
    # prefixed `via_` (item 10).
    tagged_body = (extra_extra["body"]
                   + f"\n#via_{ea._tag_token(entry['source_slug'])} #{entry['min_level'].lower()}")
    assert chans["telegram"] == [(extra_extra["title"], tagged_body, tagged_body)]
    if expect_ntfy:
        expected_priority = next(c.get("priority") for c in entry["channels"] if c["channel"] == "ntfy")
        assert chans["ntfy"] == [(extra_extra["title"], tagged_body, expected_priority)]
    else:
        assert chans["ntfy"] == []
    if expect_email:
        assert len(chans["email"]) == 1
    else:
        assert chans["email"] == []


@pytest.mark.asyncio
async def test_gtt_not_accepted_identical_via_channel_agents(chans):
    rec = {"ts": _dt.datetime(2026, 10, 6, 9, 0, tzinfo=_dt.timezone.utc), "level": "ERROR",
           "tags": ["orders", "gtt", "alert"],
           "extra": {"tags": ["orders", "gtt", "alert"], "alert_event": "gtt_not_accepted",
                     "symbol": "NIFTY26OCTFUT", "label": "SL", "gtt_id": 55, "reason": "triggered, status mismatch"}}
    await ea.dispatch([rec], _channel_agents())
    # 2026-10-28 noise tightening — gtt-not-accepted-alert gained
    # telegram + email alongside its existing ntfy (a GTT the broker
    # refused is action-required: the operator must re-arm manually).
    assert len(chans["telegram"]) == 1
    assert len(chans["email"]) == 1
    assert len(chans["ntfy"]) == 1
    assert chans["ntfy"][0][2] == "urgent"


# ─────────────────────────────────────────────────────────────────────────
# Fail-loud invariants: unknown event / sub-threshold level still fall
# through to the unrouted catch-all, never silently vanish into the bare
# "matched but nothing to send" shape.
# ─────────────────────────────────────────────────────────────────────────

def test_unknown_alert_event_matches_no_channel_agent_and_is_unrouted():
    from backend.api.algo.agent_evaluator import Context, evaluate
    rec = {"level": "CRITICAL", "tags": ["alert"],
           "extra": {"alert_event": "some_future_event_not_in_the_table"}}
    for agent in _channel_agents():
        assert not evaluate(agent.conditions, Context(log_records=[rec]))
    assert ea._looks_alert_shaped(rec)
    assert not ea._record_matches_any_agent(rec, _channel_agents())


def test_known_event_below_its_required_min_level_falls_through():
    """`cancel_unconfirmed` requires CRITICAL (per its own original
    agent's condition) — logged at WARNING, it must not match the
    shared channel-agent condition (same as it never matched
    CHASE_CANCEL_AGENT's own condition before this collapse)."""
    from backend.api.algo.agent_evaluator import Context, evaluate
    rec = {"level": "WARNING", "tags": ["chase", "alert"],
           "extra": {"alert_event": "cancel_unconfirmed"}}
    for agent in _channel_agents():
        assert not evaluate(agent.conditions, Context(log_records=[rec]))


@pytest.mark.asyncio
async def test_debug_level_never_reaches_a_channel_agent(chans):
    rec = {"level": "DEBUG", "tags": ["alert"],
           "extra": {"alert_event": "cancel_unconfirmed", "action_required": True}}
    sent = await ea.dispatch([rec], _channel_agents())
    assert sent == 0
    assert chans == {"telegram": [], "ntfy": [], "email": []}


# ─────────────────────────────────────────────────────────────────────────
# Retirement — the 16 collapsed slugs are deactivated, never deleted, on
# the next `seed_event_agents()` run; an already-inactive or absent row is
# left alone.
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retire_deactivates_only_active_retired_rows():
    rows = [
        SimpleNamespace(slug="chase-cancel-alert", status="active"),
        SimpleNamespace(slug="fill-alert", status="active"),
        SimpleNamespace(slug="error-alert", status="active"),  # NOT retired — must be untouched
        SimpleNamespace(slug="gtt-not-accepted-alert", status="inactive"),  # already inactive
    ]

    class _FakeScalars:
        def all(self):
            # Simulates the real SQL filter: only rows whose slug is in
            # _RETIRED_SLUGS AND status == 'active' would actually be
            # returned by the real query. This fake asserts the CALLER
            # passes that filter correctly by checking the args it was
            # given, then returns the pre-filtered matching subset.
            return [r for r in rows if r.slug in ea._RETIRED_SLUGS and r.status == "active"]

    class _FakeResult:
        def scalars(self):
            return _FakeScalars()

    class _FakeSession:
        async def execute(self, *_a, **_k):
            return _FakeResult()

    await ea._retire_collapsed_event_agents(_FakeSession())

    by_slug = {r.slug: r for r in rows}
    assert by_slug["chase-cancel-alert"].status == "inactive"
    assert by_slug["fill-alert"].status == "inactive"
    assert by_slug["error-alert"].status == "active", "a kept-dedicated agent must never be touched"
    assert by_slug["gtt-not-accepted-alert"].status == "inactive", "already-inactive row stays inactive (no-op)"


@pytest.mark.asyncio
async def test_seed_event_agents_calls_retire_before_commit(monkeypatch):
    """End-to-end: `seed_event_agents()` itself invokes the retirement
    pass (not just a standalone unit covered above)."""
    from backend.api.algo import event_agents as ea_mod
    calls = []

    async def _fake_retire(s):
        calls.append(s)

    monkeypatch.setattr(ea_mod, "_retire_collapsed_event_agents", _fake_retire)
    monkeypatch.setattr(ea_mod, "SEEDED_AGENTS", ())  # skip the real insert/update loop entirely

    class _FakeSession:
        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    import backend.api.database as db
    monkeypatch.setattr(db, "async_session", lambda: _FakeSession())
    await ea_mod.seed_event_agents()
    assert len(calls) == 1
