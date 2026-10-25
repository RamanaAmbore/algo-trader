"""Illustrative, end-to-end alert-agent DSL examples — TESTING/EDUCATIONAL ONLY.

Each test case below states the literal CLI text an operator would type into
the Console to create an event agent under the current (post event_stmt
dissolution, see `docs/specs/AGENTS_GRAMMAR_EBNF.md`) grammar:

    WHEN log(tag=..., min_level=..., where={...}) DO render(...), telegram, ntfy

then hand-builds the exact compiled `conditions`/`actions` shape that CLI
text compiles to (confirmed against REAL production agents in
`backend/api/algo/event_agents.py` — see each test's own docstring for which
real agent its shape mirrors), feeds a matching (and, where relevant, a
non-matching) log record through the REAL dispatch pipeline
(`event_agents.dispatch`), and asserts the exact message that would actually
be sent to each channel — mocked here, nothing is really sent.

Compiled-shape reference, all confirmed directly from `event_agents.py`:
  - `WHEN log(tag="error", min_level="ERROR") DO render(error), ...`
    -> `conditions = {"log": {"tag": "error", "min_level": "ERROR"}}`
    (this is `ERROR_AGENT`'s own literal `conditions`, byte-for-byte).
  - A `where={...}` clause narrows the match to log records whose `extra`
    dict carries those exact key/value pairs (see `ORDER_FAILURE_AGENT`,
    which conditions on `where: {"alert_event": "order_failure"}`).
  - `log(...) | log(...)` (the `event_stmt`-era `match=[...]`'s
    replacement) compiles to `{"any": [leaf1, leaf2]}` (see
    `TEMPLATE_ATTACH_URGENT_AGENT`, which ORs a "wing_unprotected" leaf
    with a "wing_hard_reject" leaf).
  - `DO render(error), telegram, ntfy` compiles to
    `actions = [{"type": "render", "render": "error"}]` plus
    `events = [{"channel": "telegram", "enabled": True},
               {"channel": "ntfy", "enabled": True}]`.

These are hand-authored test fixtures, not real seeded agents — they exist
purely to show how a DSL statement turns into a real, dispatchable alert.
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest


_FIXED_TS = "Sun 25 Oct 09:00 IST | Sat 24 Oct 23:30 EDT"


@pytest.fixture(autouse=True)
def _freeze_timestamp_display(monkeypatch):
    from backend.shared.helpers import date_time_utils
    monkeypatch.setattr(date_time_utils, "timestamp_display", lambda: _FIXED_TS)


@pytest.fixture
def sent(monkeypatch):
    """Capture every outbound channel send instead of actually sending it."""
    import backend.shared.helpers.utils as u
    from backend.shared.helpers import alert_utils
    calls = {"ntfy": [], "tg": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setattr(alert_utils, "send_ntfy_alert",
                         lambda t, b, **k: calls["ntfy"].append((t, b)))
    monkeypatch.setattr(alert_utils, "_send_telegram",
                         lambda m, **k: calls["tg"].append(m))
    return calls


def _rec(tag: str, level: str, message: str, extra: dict | None = None) -> dict:
    """Build a minimal log record shaped the way `log_store.py` hands one to
    `event_agents.dispatch_rows()` in production."""
    from datetime import timezone as _tz
    return {
        "ts": _dt.datetime(2026, 10, 25, 3, 30, 0, tzinfo=_tz.utc),
        "level": level, "logger": "backend.api.routes.orders",
        "message": message, "tags": [tag], "extra": extra or {},
    }


def _agent(conditions: dict, events: list[dict], render: str, slug: str) -> SimpleNamespace:
    """A hand-built event agent — exactly the shape a `WHEN log(...) DO
    render(...), <channels>` statement compiles to, minus the DB wrapper."""
    return SimpleNamespace(
        slug=slug, tier="medium", priority="medium",
        conditions=conditions,
        actions=[{"type": "render", "render": render}],
        events=events,
    )


_TELEGRAM_AND_NTFY = [
    {"channel": "telegram", "enabled": True},
    {"channel": "ntfy", "enabled": True},
]


@pytest.mark.asyncio
async def test_dsl_single_tag_alert_dispatches_to_telegram_and_ntfy(sent):
    """DSL:  WHEN log(tag="error", min_level="ERROR") DO render(error), telegram, ntfy

    The simplest possible event agent — match any ERROR+ log record tagged
    "error", render it via the `error` renderer, send to both channels.
    This is `ERROR_AGENT`'s own real `conditions` shape (byte-for-byte)."""
    agent = _agent(
        conditions={"log": {"tag": "error", "min_level": "ERROR"}},
        events=_TELEGRAM_AND_NTFY,
        render="error",
        slug="demo-error-alert",
    )
    from backend.api.algo import event_agents

    n_sent = await event_agents.dispatch(
        [_rec("error", "ERROR", "order placement failed: insufficient margin")],
        [agent],
    )

    assert n_sent == 2, "1 matching record x 2 channels (telegram + ntfy)"
    assert sent["ntfy"] == [
        ("RamboQuant error",
         f"Agent: manual\nbackend.api.routes.orders\norder placement failed: insufficient margin\n{_FIXED_TS}"),
    ]
    assert sent["tg"] == [
        f"Agent: manual\n<b>RamboQuant error</b>\n<code>backend.api.routes.orders</code>\n"
        f"order placement failed: insufficient margin\n{_FIXED_TS}",
    ]


@pytest.mark.asyncio
async def test_dsl_where_clause_narrows_match_to_one_alert_event(sent):
    """DSL:  WHEN log(tag="orders", min_level="WARNING",
                      where={alert_event="order_failure"})
             DO render(error), telegram, ntfy

    `where={...}` narrows the match beyond tag+level to specific `extra`
    key/value pairs — mirrors `ORDER_FAILURE_AGENT`'s real condition shape
    (`where: {"alert_event": "order_failure"}`), just rendered via the
    simpler `error` renderer here for a self-contained example.

    Two records are dispatched: one whose `extra.alert_event` matches
    exactly, one tagged/leveled identically but with a DIFFERENT
    `alert_event` — only the first may fire."""
    agent = _agent(
        conditions={"log": {"tag": "orders", "min_level": "WARNING",
                             "where": {"alert_event": "order_failure"}}},
        events=_TELEGRAM_AND_NTFY,
        render="error",
        slug="demo-order-failure-filtered",
    )
    from backend.api.algo import event_agents

    matching = _rec("orders", "WARNING", "order rejected by broker",
                     extra={"alert_event": "order_failure"})
    non_matching = _rec("orders", "WARNING", "order modified by operator",
                         extra={"alert_event": "order_modified"})

    n_sent = await event_agents.dispatch([matching, non_matching], [agent])

    assert n_sent == 2, "only the where-matching record may fire (x 2 channels)"
    assert len(sent["tg"]) == 1
    assert "order rejected by broker" in sent["tg"][0]
    assert "order modified by operator" not in sent["tg"][0]


@pytest.mark.asyncio
async def test_dsl_or_of_filters_fires_on_either_branch(sent):
    """DSL:  WHEN log(tag="gtt", min_level="WARNING",
                      where={alert_event="wing_unprotected"})
                | log(tag="gtt", min_level="CRITICAL",
                       where={alert_event="wing_hard_reject"})
             DO render(error), telegram, ntfy

    `|` between two `log(...)` conditions is the replacement for the old
    `event_stmt` grammar's `match=[...]` OR-of-filters — compiles to
    `{"any": [leaf1, leaf2]}`. Mirrors `TEMPLATE_ATTACH_URGENT_AGENT`'s
    real condition shape exactly (same two branches, same tags/levels).

    Two records are dispatched, each matching exactly one OR-branch —
    both must independently fire."""
    agent = _agent(
        conditions={"any": [
            {"log": {"tag": "gtt", "min_level": "WARNING",
                     "where": {"alert_event": "wing_unprotected"}}},
            {"log": {"tag": "gtt", "min_level": "CRITICAL",
                     "where": {"alert_event": "wing_hard_reject"}}},
        ]},
        events=_TELEGRAM_AND_NTFY,
        render="error",
        slug="demo-template-attach-urgent",
    )
    from backend.api.algo import event_agents

    branch_1 = _rec("gtt", "WARNING", "wing leg placed without protection",
                     extra={"alert_event": "wing_unprotected"})
    branch_2 = _rec("gtt", "CRITICAL", "wing leg hard-rejected by broker",
                     extra={"alert_event": "wing_hard_reject"})
    neither = _rec("gtt", "WARNING", "wing leg skipped (off-hours)",
                    extra={"alert_event": "wing_skip"})

    n_sent = await event_agents.dispatch([branch_1, branch_2, neither], [agent])

    assert n_sent == 4, "both OR-branches fire independently (x 2 channels each); the third record matches neither"
    assert len(sent["tg"]) == 2
    assert any("wing leg placed without protection" in m for m in sent["tg"])
    assert any("wing leg hard-rejected by broker" in m for m in sent["tg"])
    assert not any("wing leg skipped" in m for m in sent["tg"])
