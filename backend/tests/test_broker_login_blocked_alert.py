"""2026-11-20 fix (F3, consumer half — live ZG0790 CAPTCHA incident).

A broker login cooloff that escalates to its worst tier used to reach
ONLY Telegram, via `ERROR_AGENT`'s blanket catch-all (`error-alert`'s
own routing row is Telegram-only by deliberate design — see
`alert_routing.py`'s own comment on that slug). A multi-hour real
connection outage never escalated to ntfy or email.

This file proves the consumer half end-to-end against the REAL record
shape the producer (`backend/brokers/connections.py:KiteConnection.
_attempt_login_with_cooloff()`, the broker-layer agent's own change, a
parallel edit already landed in this working tree) actually emits — not
an idealised shape. Confirmed from the real (current) diff:

    _DISPATCH_LOGGER.error(
        "Kite login for %s has been blocked for %ds "
        "(max cooloff reached, streak=%d) — Zerodha error: %s",
        self.account, cooloff_s, self._login_fail_streak, _err_msg,
        extra={
            "tags": ["brokers", "alert"],
            "alert_event": "broker_login_blocked",
            "action_required": True,
            "self_alerted": True,
            "source_type": "process",
            "source_name": "Broker login",
            "account": mask_account(self.account),
            "cooloff_seconds": cooloff_s,
            "fail_streak": self._login_fail_streak,
            "error": _err_msg,
        },
    )

(This shape evolved during this fix's own review: an earlier producer
revision had no `self_alerted` flag and an unmasked `account`, which is
why an earlier version of this file's docstring and
`_error_agent_self_alerted()` both described a now-removed, dedicated
`alert_event`-specific branch. The producer fixed both at the source —
see `connections.py`'s own comment on the `self_alerted`/`mask_account`
lines — so the consumer side no longer needs special-casing.)

Three things this shape forces, each checked here:
  - level is ERROR, not WARNING — `_capture_row` below logs at ERROR.
  - there is NO `extra["title"]`/`extra["body"]` — the real Zerodha
    error text is baked into the formatted log MESSAGE, not a separate
    key. The renderer must read `rec["message"]`, not `extra["body"]"`
    (this is exactly why `generic_notice` was rejected in favor of a
    dedicated renderer — see `event_agents.py:_render_broker_login_
    blocked`'s own docstring).
  - `tags_for()`'s own level-based stamp still adds `"error"` to the
    row's tags (the record is ERROR level), so `ERROR_AGENT`'s blanket
    catch-all (`tag="error", min_level="ERROR"`) structurally ALSO
    matches this record — but the producer's explicit
    `extra["self_alerted"] = True` makes `_error_agent_self_alerted()`'s
    existing, generic check skip it, the SAME mechanism every other
    self-alerting producer in this codebase already relies on (chase's
    max-attempts exhaustion, `actions_live.py`'s close-failure path).
    `test_does_not_double_fire_via_error_agent_catch_all` pins this.

Pattern follows `test_app_message_critical_delivery.py` — a real
`LogStoreHandler` attached to the real (non-skip-listed) dispatch
logger, so the captured row is the exact shape `dispatch_rows()` would
hand to `event_agents.dispatch()` in production.
"""
import logging
from types import SimpleNamespace

import pytest

from backend.shared.helpers import log_store
from backend.shared.helpers.alert_utils import _DISPATCH_LOGGER
from backend.shared.helpers.utils import mask_account
from backend.api.algo import event_agents as ea
from backend.api.algo import alert_routing


def _all_seeded_agents() -> list:
    return [
        SimpleNamespace(
            id=None, slug=spec["slug"], name=spec.get("name"),
            priority=spec.get("priority"), tier=spec.get("tier"),
            **{k: spec.get(k, []) for k in ("conditions", "events", "actions")},
        )
        for spec in ea.SEEDED_AGENTS
    ]


_ZERODHA_ERROR_TEXT = "Invalid CAPTCHA values."


def _producer_log_message(account: str = "ZG0790", cooloff_s: int = 1800,
                           fail_streak: int = 11) -> str:
    """Byte-for-byte the real producer's format string, substituted —
    confirms the renderer's assertions below are checking the ACTUAL
    message shape, not an approximation of it."""
    return (
        f"Kite login for {account} has been blocked for {cooloff_s}s "
        f"(max cooloff reached, streak={fail_streak}) — "
        f"Zerodha error: {_ZERODHA_ERROR_TEXT}"
    )


async def _capture_row(level: str, message: str, **extra) -> dict:
    """Log one record through the real, non-skip-listed `_DISPATCH_
    LOGGER` (`backend.api.algo.events`) and return the row
    `LogStoreHandler` captured via the real `row_for()`/`tags_for()`
    pipeline."""
    handler = log_store.LogStoreHandler()
    logger = logging.getLogger("backend.api.algo.events")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        getattr(_DISPATCH_LOGGER, level)(message, extra=extra)
    finally:
        logger.removeHandler(handler)
    rows = handler.drain(10)
    assert len(rows) == 1, f"expected exactly one captured row, got {len(rows)}"
    return rows[0]


def _real_producer_extra(**overrides) -> dict:
    """Byte-for-byte the real (current) producer's `extra` dict — no
    `title`, no `body`; `self_alerted` and a masked `account` ARE
    present (the producer's own fix, see this module's docstring).
    Deliberately NOT idealised — synced to the real diff, not assumed."""
    base = {
        "tags": ["brokers", "alert"],
        "alert_event": "broker_login_blocked",
        "action_required": True,
        "self_alerted": True,
        "source_type": "process",
        "source_name": "Broker login",
        "account": mask_account("ZG0790"),
        "cooloff_seconds": 1800,
        "fail_streak": 11,
        "error": _ZERODHA_ERROR_TEXT,
    }
    base.update(overrides)
    return base


def _force_error_agent_gate_through(monkeypatch):
    """ERROR_AGENT is itself gated by `error_alerts.RepeatGate`
    (`ea._get_gate().decide(...)`), which returns `None` (suppress) for
    the FIRST occurrence of any never-before-seen message, delivering
    only after >3 occurrences within 900s (or an explicit `alert_now`).
    Without this stub, a double-fire test that dispatches a single,
    brand-new message would show `telegram == 1` regardless of whether
    `self_alerted` actually suppressed ERROR_AGENT — ERROR_AGENT's own
    gate would have suppressed it anyway, for an unrelated reason,
    making the test pass without testing anything. This stub makes
    ERROR_AGENT's gate deliver unconditionally (`decide()` always
    returns `0`), so only `self_alerted` decides whether it fires."""
    class _AlwaysDeliverGate:
        def decide(self, key, alert_now=False):
            return 0
    monkeypatch.setattr(ea, "_get_gate", lambda: _AlwaysDeliverGate())


@pytest.fixture
def chans(monkeypatch):
    """Capture every telegram/ntfy/email send without touching a real
    network — same pattern `test_app_message_critical_delivery.py`'s
    own `chans` fixture uses."""
    sent = {"telegram": [], "ntfy": [], "email": [], "telegram_info": []}
    monkeypatch.setattr(ea, "_channel_enabled", lambda cap: True)
    monkeypatch.setitem(ea.CHANNELS, "telegram",
                        ("telegram", lambda t, b, tg=None, **k: sent["telegram"].append((t, b, tg))))
    monkeypatch.setitem(ea.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, tg=None, priority=None, **k: sent["ntfy"].append((t, b, priority))))
    monkeypatch.setitem(ea.CHANNELS, "email",
                        ("mail", lambda t, b, tg=None, **k: sent["email"].append((t, b, k.get("email")))))
    monkeypatch.setitem(ea.CHANNELS, "telegram_info",
                        (None, lambda t, b, tg=None, **k: sent["telegram_info"].append((t, b, tg))))
    return sent


def test_broker_login_blocked_agent_is_seeded_critical_tier():
    spec = ea.BROKER_LOGIN_BLOCKED_AGENT
    assert spec["slug"] == "broker-login-blocked-alert"
    assert spec["priority"] == "critical"
    assert spec in ea.SEEDED_AGENTS
    assert spec["conditions"] == {
        "log": {"tag": "alert", "min_level": "WARNING",
                "where": {"alert_event": "broker_login_blocked"}},
    }
    assert spec["actions"] == [{"type": "render", "render": "broker_login_blocked"}]


def test_per_slug_override_routes_to_all_three_channels_gate_false():
    """F3 'done when': telegram + ntfy + email, explicitly, each
    `gate: False` so the alert reaches the operator regardless of
    capability-enabled toggles (same convention as `order-failure-alert`)."""
    resolved = alert_routing.resolve_channels(
        name="broker-login-blocked-alert", priority="critical", agent_events=[],
    )
    channels = {c["channel"] for c in resolved}
    assert channels == {"telegram", "ntfy", "email"}
    assert all(c.get("gate") is False for c in resolved)


@pytest.mark.asyncio
async def test_real_producer_shape_reaches_telegram_ntfy_and_email_exactly_once(chans, monkeypatch):
    """THE key test — the REAL producer record shape (ERROR level, no
    title/body, Zerodha text baked into the message), dispatched
    against the REAL full `SEEDED_AGENTS` table, reaches telegram + ntfy
    + email exactly once each — not just Telegram (the pre-fix
    `ERROR_AGENT`-only path), and not duplicated by `ERROR_AGENT`'s own
    catch-all firing alongside the dedicated agent. `_force_error_agent_
    gate_through` makes this a REAL double-fire guard — without it,
    ERROR_AGENT's own repeat-gate would suppress a first-time message
    regardless of `self_alerted`, and this assertion would pass for the
    wrong reason."""
    _force_error_agent_gate_through(monkeypatch)
    msg = _producer_log_message()
    row = await _capture_row("error", msg, **_real_producer_extra())
    assert row["level"] == "ERROR"
    assert row["extra"]["alert_event"] == "broker_login_blocked"
    assert row["extra"]["action_required"] is True
    assert "alert" in row["tags"]
    assert "error" in row["tags"], (
        "tags_for()'s own level-based stamp adds 'error' for any ERROR-"
        "level record, regardless of the producer's own explicit tags — "
        "this is exactly the condition that makes ERROR_AGENT's catch-all "
        "also structurally match, which this test's channel-count "
        "assertions below confirm is correctly suppressed"
    )
    # extra carries no title/body — the real producer's own shape.
    assert "title" not in row["extra"]
    assert "body" not in row["extra"]

    sent_count = await ea.dispatch([row], _all_seeded_agents())

    assert len(chans["telegram"]) == 1, (
        "must reach telegram exactly once — not twice (dedicated agent + "
        "ERROR_AGENT double-fire would be the regression)"
    )
    assert len(chans["ntfy"]) == 1, "must reach ntfy exactly once — this is the gap the fix closes"
    assert len(chans["email"]) == 1, "must reach email exactly once — this is the gap the fix closes"
    assert sent_count == 3


@pytest.mark.asyncio
async def test_real_zerodha_error_text_reaches_the_body_via_the_log_message(chans):
    """F2's captured Zerodha error text is baked into the log MESSAGE,
    not a separate `extra["body"]` key (the real producer sets none).
    The dedicated renderer must read `rec['message']`, and the rendered
    alert body must carry both the real error text and a real identity
    header — not an empty body under a generic title (what `generic_
    notice` would have produced)."""
    msg = _producer_log_message()
    row = await _capture_row("error", msg, **_real_producer_extra())

    await ea.dispatch([row], _all_seeded_agents())

    assert len(chans["telegram"]) == 1
    body = chans["telegram"][0][1]
    assert _ZERODHA_ERROR_TEXT in body, (
        f"expected the real Zerodha error text in the body, got: {body!r}"
    )
    assert "ZG0790" in body or "blocked" in body.lower()
    assert "Source: Broker login" in body
    assert body != "Source: Broker login\n", "body must not be empty after the header"


def _two_agent_list():
    return [
        SimpleNamespace(
            id=None, slug=spec["slug"], name=spec.get("name"),
            priority=spec.get("priority"), tier=spec.get("tier"),
            **{k: spec.get(k, []) for k in ("conditions", "events", "actions")},
        )
        for spec in (ea.BROKER_LOGIN_BLOCKED_AGENT, ea.ERROR_AGENT)
    ]


@pytest.mark.asyncio
async def test_does_not_double_fire_via_error_agent_catch_all(chans, monkeypatch):
    """Direct regression test for the double-fire the producer's own
    `extra["self_alerted"] = True` prevents, via `_error_agent_self_
    alerted()`'s existing, generic check (no dedicated-agent-specific
    branch needed — see this module's own docstring for why an earlier
    draft of the producer and this fix both needed, then dropped, one):
    dispatching against ONLY [BROKER_LOGIN_BLOCKED_AGENT, ERROR_AGENT]
    (isolating the two agents that structurally match this record) must
    still reach telegram exactly once, not twice.

    `_force_error_agent_gate_through` is load-bearing here, not
    decorative — without it, ERROR_AGENT's own repeat-gate suppresses a
    brand-new message's first occurrence regardless of `self_alerted`,
    so `telegram == 1` would hold even with the fix fully reverted (the
    counterfactual test below demonstrates exactly that gap)."""
    _force_error_agent_gate_through(monkeypatch)
    msg = _producer_log_message()
    row = await _capture_row("error", msg, **_real_producer_extra())

    await ea.dispatch([row], _two_agent_list())

    assert len(chans["telegram"]) == 1, (
        f"ERROR_AGENT double-fired alongside the dedicated agent — "
        f"got {chans['telegram']!r}"
    )


@pytest.mark.asyncio
async def test_counterfactual_without_self_alerted_error_agent_does_double_fire(chans, monkeypatch):
    """The counterfactual half of the double-fire proof above — with
    `self_alerted` explicitly absent (reproducing what the record would
    look like if the producer's own fix were reverted) and ERROR_AGENT's
    gate forced through, telegram must reach 2 (dedicated agent + ERROR_
    AGENT both firing). This is what actually proves the test above
    exercises the real suppression, rather than passing for an unrelated
    reason (ERROR_AGENT's own first-occurrence gate)."""
    _force_error_agent_gate_through(monkeypatch)
    msg = _producer_log_message()
    extra = _real_producer_extra()
    extra.pop("self_alerted")
    row = await _capture_row("error", msg, **extra)

    await ea.dispatch([row], _two_agent_list())

    assert len(chans["telegram"]) == 2, (
        f"expected BOTH the dedicated agent and ERROR_AGENT to fire once "
        f"`self_alerted` is absent and ERROR_AGENT's gate is forced through — "
        f"got {chans['telegram']!r}. If this is 1, the positive test above "
        f"isn't actually proving anything."
    )


def test_self_alerted_flag_unconditionally_skips_error_agent_same_as_every_other_producer():
    """Documents the CURRENT, real mechanism — the producer sets
    `extra["self_alerted"] = True` itself (see this module's own
    docstring for the review history: an earlier draft had the producer
    omit this flag, which required a dedicated, slug-specific branch in
    `_error_agent_self_alerted()` that was later removed once the
    producer was fixed at the source instead). This is the SAME
    unconditional, "trust the caller" mechanism every other
    self-alerting producer in this codebase already uses (chase's
    max-attempts exhaustion, `actions_live.py`'s close-failure path) —
    NOT a new, dedicated special case for this one `alert_event`. The
    2026-11-08 "no silent drop" invariant is scoped to the CRITICAL+
    `alert_event`-with-no-`self_alerted`-flag case specifically (see
    `_error_agent_self_alerted`'s own docstring); it does not apply here,
    because this record DOES carry an explicit `self_alerted` flag, same
    as every other producer that opts into this mechanism."""
    row = {
        "level": "ERROR", "logger": "backend.api.algo.events",
        "message": _producer_log_message(),
        "tags": ["error", "brokers", "alert"],
        "extra": _real_producer_extra(),
    }

    agent_ns = SimpleNamespace(
        id=None, slug=ea.ERROR_AGENT["slug"], name=ea.ERROR_AGENT.get("name"),
        priority=ea.ERROR_AGENT.get("priority"), tier=ea.ERROR_AGENT.get("tier"),
        **{k: ea.ERROR_AGENT.get(k, []) for k in ("conditions", "events", "actions")},
    )

    # Unconditional — true regardless of whether the dedicated consumer
    # is even in the agent list, exactly like every other self_alerted
    # producer's own records already behave.
    assert ea._error_agent_self_alerted(agent_ns, row, [agent_ns]) is True
    assert ea._error_agent_self_alerted(agent_ns, row, None) is True


@pytest.mark.asyncio
async def test_without_action_required_still_reaches_all_three_via_explicit_channel_list(chans):
    """The explicit per-slug channel list (telegram+ntfy+email) is the
    primary mechanism for this agent — an ABSENT (not explicitly
    `False`) `action_required` must not silently drop a channel, since
    `_apply_action_required_ntfy_gate`/`_apply_action_required_email_
    gate` both pass the channel list through unchanged when the flag is
    `None` rather than a real FYI-only `False`."""
    msg = _producer_log_message()
    row = await _capture_row("error", msg, **_real_producer_extra(action_required=None))

    await ea.dispatch([row], _all_seeded_agents())

    assert len(chans["telegram"]) == 1
    assert len(chans["ntfy"]) == 1
    assert len(chans["email"]) == 1


@pytest.mark.asyncio
async def test_explicit_action_required_false_drops_ntfy_per_binary_gate(chans):
    """Documents the real, intentional interaction rather than assuming
    the explicit channel list always wins unconditionally: an explicit
    `action_required=False` (a pure-FYI outcome) actively drops ntfy via
    `_apply_action_required_ntfy_gate`'s binary gate, even though this
    agent's own static per-slug override lists ntfy. The real producer
    always sets `action_required=True` for this event, so this case
    should never occur in production — this test exists to pin the
    gate's real behavior, not to recommend `False` for this agent."""
    msg = _producer_log_message()
    row = await _capture_row("error", msg, **_real_producer_extra(action_required=False))

    await ea.dispatch([row], _all_seeded_agents())

    assert len(chans["telegram"]) == 1
    assert chans["ntfy"] == []
    assert len(chans["email"]) == 1


@pytest.mark.asyncio
async def test_non_matching_record_does_not_fire_this_agent(chans):
    """A record that carries no `broker_login_blocked` alert_event (e.g.
    a routine retry log line the producer must NOT emit per-attempt)
    must not match this agent at all — confirms the condition is scoped
    to the one escalation-episode event, not every login-related log
    line. Also confirmed not to match ERROR_AGENT (level is INFO)."""
    row = await _capture_row("info", "routine retry attempt", tags=["broker"], alert_event=None)

    sent_count = await ea.dispatch([row], _all_seeded_agents())

    assert chans["telegram"] == []
    assert chans["ntfy"] == []
    assert chans["email"] == []
    assert sent_count == 0


@pytest.mark.asyncio
async def test_two_distinct_escalation_records_each_fire_once_no_cross_contamination(chans):
    """Simulates two DIFFERENT accounts each independently reaching max
    cooloff — each is its own log call (the producer's per-account flag
    is account-scoped), so each must independently reach all three
    channels; dispatching two records must not collapse into a single
    de-duplicated send (that would silently hide a second account's
    real outage) nor multiply beyond 2x each channel."""
    row_a = await _capture_row("error", _producer_log_message(account="ZG0790"),
                                **_real_producer_extra(account="ZG0790"))
    row_b = await _capture_row("error", _producer_log_message(account="DH6847"),
                                **_real_producer_extra(account="DH6847"))

    await ea.dispatch([row_a, row_b], _all_seeded_agents())

    assert len(chans["telegram"]) == 2
    assert len(chans["ntfy"]) == 2
    assert len(chans["email"]) == 2
