"""2026-10-25 tag-unification regression gate.

Every `kind='event'` seeded agent except `ERROR_AGENT` / `RICH_ALERT_AGENT` /
`BREACH_AGENT` (explicitly excluded — see `event_agents.SEED_VERSION`'s own
comment, or each excluded agent's own dict) now matches on the single,
additive "alert" tag instead of its own category tag ("orders"/"gtt"/
"chase"/"mcp"/"deploy"/"summary"/"broker"/"visitor"/"auth"/"agent") —
`where.alert_event` already uniquely discriminated every one of these
agents, so the category tag in the condition was redundant.

We JUST fixed a ~19-day silent alert-delivery outage in this exact area
(commit 492fd988), caused by a condition/tag mismatch that silently stopped
an agent from matching its real triggering record. This file exists so that
class of regression cannot recur here: for every unified agent it builds the
REAL triggering log record — matching the actual `tags`/`extra` shape its
real producer constructs, confirmed against source at
`backend/api/algo/*.py`, `backend/api/routes/*.py`, `backend/api/background.py`,
`backend/shared/helpers/alert_utils.py` — and proves, via the REAL
evaluation path (`agent_evaluator.evaluate` / `log_store.row_for` /
`log_store.tags_for`), that the new condition still matches it. It also
proves the swap is real (not a silent no-op): the SAME record with only the
old category tag — "alert" removed — no longer matches.

For the two agents whose producer is a pure, callable, DB-free function —
`FILL_AGENT` (`fill_notify.notify_fills`), `USER_REGISTERED_AGENT`
(`routes.auth._registration_alert_extra`), and `UNROUTED_ALERT_AGENT`
(`event_agents._emit_unrouted_self_alert`) — the record is captured from a
REAL CALL to that producer, not a hand-typed copy, closing the gap a typo in
the real `tags`/`alert_event` string could otherwise hide.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.api.algo import event_agents as ea
from backend.api.algo.agent_evaluator import Context, evaluate
from backend.shared.helpers import log_store


def _row_from_real_pipeline(logger_name: str, level: str, message: str, extra: dict) -> dict:
    """Build the REAL row dict a genuine `logger.<level>(msg, extra=...)`
    call produces — real `logging.LogRecord` via `Logger.makeRecord`
    (exactly how a genuine call site builds one), through the real
    `log_store.row_for()` (which itself calls the real `tags_for()`).
    Same pattern `test_alert_routing.py::
    test_auth_internal_tagged_records_never_reach_the_catch_all` already
    uses for the identical reason: exercise the REAL tag-derivation
    pipeline, not a hand-built tags list."""
    lg = logging.getLogger(logger_name)
    level_no = logging.getLevelName(level)
    record = lg.makeRecord(lg.name, level_no, __file__, 0, message, None, None, extra=extra)
    return log_store.row_for(record)


def _strip_tag(row: dict, tag: str) -> dict:
    """A copy of `row` with `tag` removed from its top-level tags list —
    simulates "the swap never happened" so we can prove the new condition
    genuinely requires the new tag, not that it matches everything."""
    return {**row, "tags": [t for t in row.get("tags", []) if t != tag]}


def _agent_double(spec: dict) -> SimpleNamespace:
    return SimpleNamespace(slug=spec["slug"], tier="medium", priority=spec.get("priority"),
                           conditions=spec["conditions"], actions=spec["actions"],
                           events=spec.get("events") or [])


# ─────────────────────────────────────────────────────────────────────────
# Table of (spec, real producer logger, level, real producer `extra`,
# old category tag) for every unified agent whose producer we exercise via
# a hand-built-but-source-confirmed record (the producer call itself needs
# DB/broker/network mocking too heavy for a per-agent matching proof —
# the `extra`/`tags` shape below is copied verbatim from each producer's
# real source line, confirmed via grep immediately before this file was
# written).
# ─────────────────────────────────────────────────────────────────────────
_CASES = [
    pytest.param(
        ea.CHASE_CANCEL_AGENT, "backend.api.algo.chase", "CRITICAL",
        {"tags": ["chase", "alert"], "alert_event": "cancel_unconfirmed", "order_id": "O1"},
        "chase", id="chase-cancel-alert",
    ),
    pytest.param(
        ea.PARTIAL_GTT_AGENT, "backend.api.routes.orders_place", "CRITICAL",
        {"tags": ["orders", "gtt", "alert"], "alert_event": "partial_gtt", "parent_row_id": 1},
        "gtt", id="partial-gtt-alert",
    ),
    pytest.param(
        ea.TEMPLATE_ATTACH_URGENT_AGENT, "backend.api.algo.template_attach", "WARNING",
        {"tags": ["orders", "gtt", "alert"], "alert_event": "wing_unprotected", "parent_order_id": 1},
        "gtt", id="template-attach-urgent-wing_unprotected",
    ),
    pytest.param(
        ea.TEMPLATE_ATTACH_URGENT_AGENT, "backend.api.algo.template_attach", "CRITICAL",
        {"tags": ["orders", "gtt", "alert"], "alert_event": "wing_hard_reject", "symbol": "X"},
        "gtt", id="template-attach-urgent-wing_hard_reject",
    ),
    pytest.param(
        ea.TEMPLATE_ATTACH_HIGH_AGENT, "backend.api.algo.template_attach", "WARNING",
        {"tags": ["orders", "gtt", "alert"], "alert_event": "wing_skip", "parent_order_id": 1},
        "gtt", id="template-attach-high-wing_skip",
    ),
    pytest.param(
        ea.TEMPLATE_ATTACH_HIGH_AGENT, "backend.api.algo.template_attach", "WARNING",
        {"tags": ["orders", "gtt", "alert"], "alert_event": "wing_offset_skip", "parent_order_id": 1},
        "gtt", id="template-attach-high-wing_offset_skip",
    ),
    pytest.param(
        ea.ORDER_FAILURE_AGENT, "backend.api.algo.events", "WARNING",
        {"tags": ["orders", "alert"], "alert_event": "order_failure", "masked": "ZG####"},
        "orders", id="order-failure-alert",
    ),
    pytest.param(
        ea.ORDER_CORRECTION_AGENT, "backend.api.algo.order_poll", "WARNING",
        {"tags": ["orders", "alert"], "alert_event": "order_correction", "algo_order_id": 1},
        "orders", id="order-correction-alert",
    ),
    pytest.param(
        ea.TEMPLATE_GUARD_AGENT, "backend.api.algo.template_attach", "INFO",
        {"tags": ["orders", "alert"], "alert_event": "template_guard", "parent_order_id": 1},
        "orders", id="template-guard-alert",
    ),
    pytest.param(
        ea.TEMPLATE_ATTACH_FAIL_AGENT, "backend.api.algo.template_attach", "WARNING",
        {"tags": ["orders", "alert"], "alert_event": "template_attach_fail", "order_id": 1},
        "orders", id="template-attach-fail-alert",
    ),
    pytest.param(
        ea.MCP_PING_AGENT, "backend.api.routes.lab", "INFO",
        {"tags": ["mcp", "alert"], "alert_event": "mcp_ping", "tg": "x"},
        "mcp", id="mcp-ping-alert",
    ),
    pytest.param(
        ea.DEPLOY_SYNC_AGENT, "backend.api.background", "WARNING",
        {"tags": ["deploy", "alert"], "alert_event": "deploy_out_of_sync", "title": "x", "body": "y"},
        "deploy", id="deploy-sync-alert",
    ),
    pytest.param(
        ea.SUMMARY_AGENT, "backend.api.algo.events", "INFO",
        {"tags": ["summary", "alert"], "alert_event": "summary", "msg_type": "open",
         "ist_display": "x", "tg_table": "x", "email_table_html": "x", "subject_detail": "x"},
        "summary", id="market-summary",
    ),
    pytest.param(
        ea.GTT_NOT_ACCEPTED_AGENT, "backend.api.algo.template_attach", "ERROR",
        {"tags": ["orders", "gtt", "alert"], "alert_event": "gtt_not_accepted", "gtt_id": 1},
        "gtt", id="gtt-not-accepted-alert",
    ),
    pytest.param(
        ea.GTT_ASYMMETRIC_AGENT, "backend.api.background", "WARNING",
        {"tags": ["gtt", "alert"], "alert_event": "gtt_asymmetric", "title": "x", "body": "y"},
        "gtt", id="gtt-asymmetric-alert",
    ),
    pytest.param(
        ea.OCO_DOUBLE_FIRE_AGENT, "backend.api.background", "WARNING",
        {"tags": ["gtt", "alert"], "alert_event": "oco_double_fire", "title": "x", "body": "y"},
        "gtt", id="oco-double-fire-alert",
    ),
    pytest.param(
        ea.TICKER_RECOVERED_AGENT, "backend.api.background", "INFO",
        {"tags": ["broker", "alert"], "alert_event": "ticker_recovered", "title": "x", "body": "y"},
        "broker", id="ticker-recovered-alert",
    ),
    pytest.param(
        ea.TICKER_DEGRADED_AGENT, "backend.api.background", "WARNING",
        {"tags": ["broker", "alert"], "alert_event": "ticker_degraded", "title": "x", "body": "y"},
        "broker", id="ticker-degraded-alert",
    ),
    pytest.param(
        ea.VISITOR_REPORT_AGENT, "backend.api.background", "INFO",
        {"tags": ["visitor", "alert"], "alert_event": "visitor_report", "title": "x", "body": "y"},
        "visitor", id="visitor-report-alert",
    ),
]


@pytest.mark.parametrize("spec, logger_name, level, extra, old_tag", _CASES)
def test_unified_agent_matches_its_real_triggering_record(spec, logger_name, level, extra, old_tag):
    """The new condition (tag="alert") matches a record shaped exactly
    like the real producer's own `extra`/`tags`, via the real evaluation
    path (`agent_evaluator.evaluate` over a row built by the real
    `log_store.row_for()`/`tags_for()`)."""
    row = _row_from_real_pipeline(logger_name, level, "x", extra)
    assert "alert" in row["tags"], "fixture sanity: the real tags_for() pipeline must surface 'alert'"
    matches = evaluate(spec["conditions"], Context(log_records=[row]))
    assert matches, f"{spec['slug']}: new tag='alert' condition failed to match its own real producer shape"


@pytest.mark.parametrize("spec, logger_name, level, extra, old_tag", _CASES)
def test_unified_agent_no_longer_matches_the_old_category_tag_alone(spec, logger_name, level, extra, old_tag):
    """Proves the tag swap is REAL, not a no-op: the identical record,
    with ONLY 'alert' removed from its tags (old category tag still
    present), must no longer satisfy the new condition. If this ever
    passes with a non-empty match, the condition silently stopped
    requiring 'alert' — exactly the regression class this file guards."""
    row = _row_from_real_pipeline(logger_name, level, "x", extra)
    stripped = _strip_tag(row, "alert")
    assert old_tag in stripped["tags"], "fixture sanity: the category tag must survive the strip"
    matches = evaluate(spec["conditions"], Context(log_records=[stripped]))
    assert not matches, (
        f"{spec['slug']}: condition matched WITHOUT the 'alert' tag — "
        f"the tag swap to 'alert' is not actually enforced"
    )


# ─────────────────────────────────────────────────────────────────────────
# The 3 agents whose producer is a pure, DB-free, directly-callable
# function — captured from a REAL CALL, not a hand-typed copy.
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fill_agent_matches_the_real_notify_fills_producer(monkeypatch):
    from backend.api.algo import fill_notify

    captured: list[dict] = []
    monkeypatch.setattr(fill_notify.logger, "info", lambda msg, **kw: captured.append(kw["extra"]))
    row_obj = SimpleNamespace(id=1, account="ZG0790", symbol="NIFTY26OCTFUT", exchange="NFO",
                              transaction_type="BUY", quantity=1, fill_price=10.0, product="NRML",
                              mode="live", engine="live")
    await fill_notify.notify_fills([row_obj])
    assert captured, "fixture sanity: notify_fills must have logged something"
    real_extra = captured[0]
    assert "alert" in real_extra["tags"], "fixture sanity: fill_notify's own producer must still tag 'alert'"

    row = _row_from_real_pipeline("backend.api.algo.fill_notify", "INFO", "order filled", real_extra)
    assert evaluate(ea.FILL_AGENT["conditions"], Context(log_records=[row])), (
        "FILL_AGENT's tag='alert' condition failed to match the REAL notify_fills() producer output"
    )

    stripped = _strip_tag(row, "alert")
    assert not evaluate(ea.FILL_AGENT["conditions"], Context(log_records=[stripped])), (
        "FILL_AGENT matched without the 'alert' tag — the swap is not actually enforced"
    )


def test_user_registered_agent_matches_the_real_registration_alert_extra():
    """Not in the original unification brief's own enumeration — flagged
    explicitly in this pass's report; included under the brief's own
    stated rule ("every agent except the 3 excluded ones")."""
    from backend.api.routes.auth import _registration_alert_extra

    real_extra = _registration_alert_extra("newtrader", "newtrader@example.com")
    assert "alert" in real_extra["tags"], "fixture sanity: the real producer must tag 'alert'"

    row = _row_from_real_pipeline("backend.api.routes.auth", "INFO", "x", real_extra)
    assert evaluate(ea.USER_REGISTERED_AGENT["conditions"], Context(log_records=[row])), (
        "USER_REGISTERED_AGENT's tag='alert' condition failed to match the REAL "
        "_registration_alert_extra() producer output"
    )

    stripped = _strip_tag(row, "alert")
    assert not evaluate(ea.USER_REGISTERED_AGENT["conditions"], Context(log_records=[stripped])), (
        "USER_REGISTERED_AGENT matched without the 'alert' tag — the swap is not actually enforced"
    )


def test_unrouted_alert_agent_matches_the_real_self_alert_producer(monkeypatch):
    """Not in the original unification brief's own enumeration — flagged
    explicitly in this pass's report; included under the brief's own
    stated rule. Unlike RICH_ALERT_AGENT/BREACH_AGENT (also tag="agent",
    correctly excluded), this agent's producer is a log-tag event-agent
    producer like every other one in this file — see
    `UNROUTED_ALERT_AGENT`'s own dict comment for why it's safe to unify."""
    captured: list[tuple] = []
    monkeypatch.setattr(ea._EVENTS_LOGGER, "warning",
                        lambda msg, **kw: captured.append((msg, kw["extra"])))
    rec = {"tags": ["orders"], "level": "WARNING", "logger": "backend.x",
           "message": "boom", "extra": {}}
    ea._emit_unrouted_self_alert(rec)
    assert captured, "fixture sanity: _emit_unrouted_self_alert must have logged something"
    _, real_extra = captured[0]
    assert "alert" in real_extra["tags"], "fixture sanity: the real producer must tag 'alert'"
    assert "agent" in real_extra["tags"], "the pre-existing 'agent' tag must survive additively"

    row = _row_from_real_pipeline("backend.api.algo.events", "WARNING", "x", real_extra)
    assert evaluate(ea.UNROUTED_ALERT_AGENT["conditions"], Context(log_records=[row])), (
        "UNROUTED_ALERT_AGENT's tag='alert' condition failed to match the REAL "
        "_emit_unrouted_self_alert() producer output"
    )

    stripped = _strip_tag(row, "alert")
    assert not evaluate(ea.UNROUTED_ALERT_AGENT["conditions"], Context(log_records=[stripped])), (
        "UNROUTED_ALERT_AGENT matched without the 'alert' tag — the swap is not actually enforced"
    )


# ─────────────────────────────────────────────────────────────────────────
# The 3 explicitly excluded agents — byte-identical to before this pass.
# ─────────────────────────────────────────────────────────────────────────

def test_error_agent_condition_is_byte_identical_to_before_this_pass():
    """ERROR_AGENT is a blanket catch-all auto-tagged by `tags_for()` for
    EVERY ERROR+ (and, as of 2026-10-24, CRITICAL) record codebase-wide —
    changing it to tag="alert" would silently stop it matching almost
    everything it currently catches, since most ERROR lines don't carry
    an explicit "alert" tag. Deliberately left untouched."""
    assert ea.ERROR_AGENT["conditions"] == {"log": {"tag": "error", "min_level": "ERROR"}}


def test_rich_alert_agent_condition_is_byte_identical_to_before_this_pass():
    """RICH_ALERT_AGENT fires from the cycle-agent engine pipeline, which
    already writes a real AgentEvent DB row for every firing. Tagging its
    log line "alert" would double-count it in the Alerts tab's LogEvent
    merge (`routes/alerts.py`: `LogEvent.tags.overlap(["alert"])`).
    Deliberately left untouched, including its own producer call site."""
    assert ea.RICH_ALERT_AGENT["conditions"] == {
        "log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "rich_alert"}},
    }


def test_breach_agent_condition_is_byte_identical_to_before_this_pass():
    """Same rationale as RICH_ALERT_AGENT above — BREACH_AGENT also fires
    from the cycle-agent engine pipeline with its own real AgentEvent
    row. Deliberately left untouched, including its own producer call
    site."""
    assert ea.BREACH_AGENT["conditions"] == {
        "log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "breach"}},
    }


def test_all_23_seeded_agents_accounted_for():
    """Every SEEDED_AGENTS slug is either unified (tested above),
    explicitly excluded (tested above), or named here as net-new/
    special-case — guards against a future agent silently falling
    outside this file's coverage."""
    excluded = {ea.ERROR_AGENT["slug"], ea.RICH_ALERT_AGENT["slug"], ea.BREACH_AGENT["slug"]}
    unified_tested = {p.values[0]["slug"] for p in _CASES} | {
        ea.FILL_AGENT["slug"], ea.USER_REGISTERED_AGENT["slug"], ea.UNROUTED_ALERT_AGENT["slug"],
    }
    all_slugs = {spec["slug"] for spec in ea.SEEDED_AGENTS}
    assert all_slugs == excluded | unified_tested, (
        f"uncovered slugs: {all_slugs - excluded - unified_tested}; "
        f"stale coverage entries: {(excluded | unified_tested) - all_slugs}"
    )
