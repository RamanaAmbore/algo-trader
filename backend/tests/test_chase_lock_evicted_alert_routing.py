"""2026-11-28 alert-routing fix — `chase.py`'s own position-lock TTL
eviction housekeeping (`_get_chase_position_lock`) carried no
`alert_event` at all, so every single eviction fell through to
`unrouted-alert-catch-all` (Telegram + ntfy). A live trace confirmed all
22 `unrouted_alert` catch-all fires in the prior 14 days of prod
`agent_events`/`log_events` were this one, routine, self-resolving
message.

Fix, three pieces, all exercised below against the REAL code (not a
re-implementation):

  1. `chase.py:_get_chase_position_lock` now tags the eviction WARNING
     with `tags=["chase", "alert"]` and `alert_event="chase_lock_evicted"`.
  2. `event_agents.CHASE_LOCK_EVICTED_AGENT` is folded into
     `_COLLAPSED_DETECTOR_SPECS`, so the record now matches at least one
     active agent's conditions (the shared condition the 3 generic
     channel agents carry) — this is what actually stops
     `_emit_unrouted_self_alert()` from firing, independent of routing.
  3. `alert_routing._PER_SLUG_OVERRIDES["chase-lock-evicted-alert"] = []`
     routes the matched record to zero real channels — logged (visible
     in `/admin/alerts/history` via the `alert` tag), sent nowhere.

Each test below is end-to-end against the real pipeline, mirroring the
existing precedent in `test_event_agents.py`
(`test_unrouted_alert_shaped_record_reaches_a_real_channel` et al.) and
`test_alert_channel_collapse.py` (`_channel_agents()`), not a synthetic
shortcut.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.api.algo import chase
from backend.api.algo import event_agents as ea
from backend.shared.helpers import utils as u
from backend.shared.helpers.log_store import row_for


# ─────────────────────────────────────────────────────────────────────
# 1 — the real producer carries the right shape
# ─────────────────────────────────────────────────────────────────────

class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_chase_lock_eviction_log_record_carries_alert_event_and_tags(monkeypatch):
    """Drives the REAL `_get_chase_position_lock()` — not a hand-built
    fixture — with a pre-seeded stale entry, captures the real
    `logger.warning(...)` call via a real `logging.Handler` on
    `chase.logger`, and converts it through the real `log_store.row_for()`
    exactly like `LogStoreHandler` does on the next flush cycle."""
    handler = _Capture()
    chase.logger.addHandler(handler)
    # Isolate from any other test/async-task touching the module-level
    # registry — a fresh key, pre-seeded as already-expired.
    stale_key = ("Z0001", "NFO", "TESTSYM")
    monkeypatch.setattr(chase, "_CHASE_POSITION_LOCKS", {
        stale_key: (asyncio.Lock(), chase._time.monotonic() - chase._CHASE_POSITION_LOCK_TTL_S - 1),
    })
    try:
        await chase._get_chase_position_lock(("Z0001", "NFO", "OTHERSYM"))
    finally:
        chase.logger.removeHandler(handler)

    assert len(handler.records) == 1, "exactly one eviction WARNING for the one stale entry"
    row = row_for(handler.records[0])
    assert row["level"] == "WARNING"
    assert row["extra"]["alert_event"] == "chase_lock_evicted"
    assert row["extra"]["source_type"] == "process"
    assert row["extra"]["source_name"] == "chase"
    assert "chase" in row["tags"]
    assert "alert" in row["tags"], "must carry the 'alert' tag — the 3 generic channel agents' shared condition is hardcoded to tag='alert', not 'chase'"


# ─────────────────────────────────────────────────────────────────────
# 2 — the derived dispatch table resolves to zero channels
# ─────────────────────────────────────────────────────────────────────

def test_alert_event_dispatch_entry_exists_and_resolves_to_zero_channels():
    entry = ea.ALERT_EVENT_DISPATCH.get("chase_lock_evicted")
    assert entry is not None, "chase_lock_evicted must have a real ALERT_EVENT_DISPATCH entry for the 3 generic channel agents to recognise it at all"
    assert entry["channels"] == []
    assert entry["min_level"] == "WARNING"
    assert entry["where"] == {"alert_event": "chase_lock_evicted"}
    assert entry["source_slug"] == "chase-lock-evicted-alert"
    assert entry["render"] in ea.RENDERS


def test_chase_lock_evicted_agent_is_folded_not_standalone():
    """Folded into `_COLLAPSED_DETECTOR_SPECS` — never a standalone
    `SEEDED_AGENTS` row, so its slug never gets its own `Agent` DB row
    seeded (see event_agents.CHASE_LOCK_EVICTED_AGENT's own comment for
    why: an empty `[]` channel list has no structural reason to need a
    dedicated agent, unlike `draft-sweep-alert`/`pnl-reconciliation-
    alert`, which are pinned to `telegram_info`)."""
    seeded_slugs = {s["slug"] for s in ea.SEEDED_AGENTS}
    collapsed_slugs = {s["slug"] for s in ea._COLLAPSED_DETECTOR_SPECS}
    assert "chase-lock-evicted-alert" not in seeded_slugs
    assert "chase-lock-evicted-alert" in collapsed_slugs
    assert "chase-lock-evicted-alert" in ea._RETIRED_SLUGS


def test_per_slug_override_is_explicit_empty_list():
    from backend.api.algo import alert_routing
    assert alert_routing._PER_SLUG_OVERRIDES["chase-lock-evicted-alert"] == []


# ─────────────────────────────────────────────────────────────────────
# 3 — end-to-end: the real record reaches zero channels AND never
#     triggers the unrouted-alert catch-all (the regression this fix
#     exists to close)
# ─────────────────────────────────────────────────────────────────────

def _real_channel_agents() -> list[SimpleNamespace]:
    """The 3 real generic channel agents, exactly as production loads
    them — `conditions`/`actions` read live off the real specs, never
    retyped. Mirrors `test_alert_channel_collapse.py:_channel_agents()`."""
    return [
        SimpleNamespace(slug=spec["slug"], tier=None, priority=spec.get("priority"),
                        conditions=spec["conditions"], actions=spec["actions"], events=[])
        for spec in (ea.TELEGRAM_CHANNEL_AGENT, ea.NTFY_CHANNEL_AGENT, ea.EMAIL_CHANNEL_AGENT)
    ]


def _capture_channels(monkeypatch):
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


def _real_chase_lock_evicted_rec() -> dict:
    """The exact row shape the real `_get_chase_position_lock()` WARNING
    produces, built through the real `row_for()` over a hand-constructed
    `LogRecord` carrying the real `extra` dict (same literal values as
    the production call site in `chase.py`, kept in sync by test #1
    above driving the real producer directly)."""
    record = logging.LogRecord(
        name="backend.api.algo.chase", level=logging.WARNING, pathname=__file__,
        lineno=1, msg="[CHASE-LOCK] evicted 1 stale position lock(s) (TTL=1800s): keys=%s",
        args=(["x"],), exc_info=None,
    )
    record.tags = ["chase", "alert"]
    record.alert_event = "chase_lock_evicted"
    record.source_type = "process"
    record.source_name = "chase"
    return row_for(record)


@pytest.mark.asyncio
async def test_chase_lock_evicted_record_reaches_zero_channels(monkeypatch):
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    sent = _capture_channels(monkeypatch)

    async def load():
        return _real_channel_agents()

    monkeypatch.setattr(ea, "load_agents", load)
    rec = _real_chase_lock_evicted_rec()
    sent_count = 0
    for agent in _real_channel_agents():
        key = ea._channel_key_for_agent(agent)
        sent_count += await ea._dispatch_channel_agent(agent, [rec], key)
    assert sent_count == 0
    assert sent == {"telegram": [], "ntfy": [], "email": []}


@pytest.mark.asyncio
async def test_chase_lock_evicted_record_never_triggers_unrouted_catch_all(monkeypatch):
    """Full `dispatch_rows()` path — confirms the record is classified as
    ROUTED (matches a real agent's conditions), so
    `_emit_unrouted_self_alert()` is never called at all. This is the
    direct regression test for the confirmed root cause: before the fix,
    every one of these records reached this exact call."""
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    sent = _capture_channels(monkeypatch)
    self_alert_calls: list[dict] = []
    monkeypatch.setattr(ea, "_emit_unrouted_self_alert", lambda rec: self_alert_calls.append(rec))

    async def load():
        return _real_channel_agents()

    monkeypatch.setattr(ea, "load_agents", load)
    rec = _real_chase_lock_evicted_rec()
    await ea.dispatch_rows([rec])

    assert self_alert_calls == [], "a chase_lock_evicted record must never reach the unrouted-alert catch-all"
    assert sent == {"telegram": [], "ntfy": [], "email": []}, "and must send to zero real channels"


def test_chase_lock_evicted_rec_matches_a_real_agent_condition():
    """Direct unit check of the exact gate `_run_unrouted_catch_all()`
    uses — `_looks_alert_shaped()` is True (WARNING + a known alert tag,
    unavoidable) but `_record_matches_any_agent()` must now ALSO be
    True, which is what actually prevents the self-alert."""
    rec = _real_chase_lock_evicted_rec()
    assert ea._looks_alert_shaped(rec) is True
    assert ea._record_matches_any_agent(rec, _real_channel_agents()) is True


# ─────────────────────────────────────────────────────────────────────
# 4 — negative control: the catch-all's safety net is still intact for
#     a genuinely unmapped alert_event
# ─────────────────────────────────────────────────────────────────────

def test_genuinely_unmapped_alert_event_still_fails_the_match_check():
    """Same tags (`["chase", "alert"]`), same WARNING level, same real
    3-channel-agent condition set as the fixed case above — but an
    `alert_event` with NO `ALERT_EVENT_DISPATCH` entry. Must still fail
    `_record_matches_any_agent()`, proving this fix narrowly targets
    `chase_lock_evicted` and does not loosen the shared condition into a
    bare tag="alert" match (which would defeat the catch-all's whole
    purpose — see `_build_shared_channel_condition()`'s own docstring)."""
    assert "totally_unmapped_chase_event" not in ea.ALERT_EVENT_DISPATCH
    record = logging.LogRecord(
        name="backend.api.algo.chase", level=logging.WARNING, pathname=__file__,
        lineno=1, msg="some other chase warning", args=(), exc_info=None,
    )
    record.tags = ["chase", "alert"]
    record.alert_event = "totally_unmapped_chase_event"
    rec = row_for(record)

    assert ea._looks_alert_shaped(rec) is True, "still alert-shaped (WARNING + known tag)"
    assert ea._record_matches_any_agent(rec, _real_channel_agents()) is False, (
        "an unmapped alert_event must still fail to match any real agent's conditions"
    )


@pytest.mark.asyncio
async def test_genuinely_unmapped_alert_event_still_reaches_the_catch_all(monkeypatch):
    """Full `dispatch_rows()` path for the negative control — the
    catch-all's `_emit_unrouted_self_alert()` must still fire exactly
    once for a record this fix does not, and should not, cover."""
    monkeypatch.setattr(u, "config", {"deploy_branch": "main"})
    self_alert_calls: list[dict] = []
    monkeypatch.setattr(ea, "_emit_unrouted_self_alert", lambda rec: self_alert_calls.append(rec))

    async def load():
        return _real_channel_agents()

    monkeypatch.setattr(ea, "load_agents", load)
    record = logging.LogRecord(
        name="backend.api.algo.chase", level=logging.WARNING, pathname=__file__,
        lineno=1, msg="some other chase warning", args=(), exc_info=None,
    )
    record.tags = ["chase", "alert"]
    record.alert_event = "totally_unmapped_chase_event"
    rec = row_for(record)
    await ea.dispatch_rows([rec])

    assert len(self_alert_calls) == 1, "the catch-all's safety net must still fire for a genuinely unrouted event"
    assert self_alert_calls[0]["extra"]["alert_event"] == "totally_unmapped_chase_event"
