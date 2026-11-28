"""Pins the "Fix 1" root-cause finding from the 2026-11-28 expiry-alert
volume investigation, at the real engine level — not just by reading the
source.

The task's original framing assumed `expiry-mcx-risk-alert` fired once
PER LEAF within a single evaluation tick, and asked for a new mechanism
to batch simultaneously-eligible leaves into one message. Reading the
real dispatch path (`_cycle_maybe_buffer_fire` → one `pending_dispatches`
entry per agent per tick, carrying the FULL `matches` list, not just the
newly-eligible subset) showed this batching already exists. The real
root cause (confirmed via prod `agent_events` + a missing cooldown
migration) is covered by `_ae_sync_expiry_risk_alert_cooldown`
(`agent_engine.py`) and `test_expiry_risk_alert_cooldown_migration_
2026_11_28.py`.

This file is the engine-level proof for that "already batched" claim,
requested explicitly because the claim had previously rested only on
reading the source:

  1. Four simultaneously-eligible matches, spanning 2 accounts, passed
     to ONE `_cycle_maybe_buffer_fire` call (= one evaluation tick) must
     produce exactly ONE `pending_dispatches` entry, carrying all 4
     matches (not 4 separate entries/calls).
  2. Two SEPARATE `_cycle_maybe_buffer_fire` calls (= two separate
     ticks) must never merge into one entry — each produces its own,
     independent entry. Batching must never become "wait and coalesce
     across cycles forever".

No source code is changed or needed by this file.
"""
from __future__ import annotations

from datetime import datetime

from backend.api.algo import agent_engine
from backend.api.algo.agent_engine import _cycle_maybe_buffer_fire, _latch_key


class _Agent:
    def __init__(self, id=1, slug="expiry-mcx-risk-alert", tier="high", topic="expiry_warning_mcx"):
        self.id = id
        self.slug = slug
        self.name = slug
        self.tier = tier
        self.topic = topic
        self.actions = []
        self.status = "active"
        self.trigger_count = 0
        self.fire_at_time = None
        self.cooldown_minutes = 240


def _leg_match(account: str, symbol: str, *, metric="pnl", scope="positions.expiring_today.mcx_unhedged"):
    """A per-leg match shaped like the real
    `positions.expiring_today.mcx_unhedged` scope — carries both
    'account' AND a 'row' with 'tradingsymbol', so `_latch_key` treats
    each (account, symbol) pair as an independent leaf (Sprint 1 fan-out
    fix for `positions.any_symbol`-style scopes)."""
    return {
        "metric": metric, "scope": scope, "account": account,
        "op": ">=", "threshold": -999999999, "value": 0.0,
        "row": {"account": account, "tradingsymbol": symbol, "exchange": "MCX"},
    }


def _now():
    return datetime(2026, 11, 27, 9, 4, 0)


class TestFourSimultaneousLegsProduceOneDispatchEntry:
    def setup_method(self):
        agent_engine._V2_LATCH.clear()

    def teardown_method(self):
        agent_engine._V2_LATCH.clear()

    def test_four_legs_two_accounts_one_tick_yields_one_pending_entry(self):
        agent = _Agent()
        matches = [
            _leg_match("ZG0790", "GOLD26NOV123000CE"),
            _leg_match("ZG0790", "GOLDM26NOV123000CE"),
            _leg_match("ZJ6294", "GOLD26NOV124000CE"),
            _leg_match("ZJ6294", "GOLD26NOV125000PE"),
        ]
        pending_dispatches: list = []

        fired = _cycle_maybe_buffer_fire(
            agent, matches,
            now=_now(),
            bypass_suppression=False,
            bypass_schedule=True,
            sim_mode=False,
            alert_state={},
            cfg={"cooldown_min": 30},
            broadcast_fn=None,
            debounce_min=0,
            pending_dispatches=pending_dispatches,
        )

        assert fired is True
        assert len(pending_dispatches) == 1, (
            "4 legs becoming eligible in the SAME tick must produce "
            f"exactly 1 pending_dispatches entry (one outbound message), "
            f"got {len(pending_dispatches)}"
        )
        entry = pending_dispatches[0]
        entry_symbols = {m["row"]["tradingsymbol"] for m in entry["matches"]}
        assert entry_symbols == {
            "GOLD26NOV123000CE", "GOLDM26NOV123000CE",
            "GOLD26NOV124000CE", "GOLD26NOV125000PE",
        }, "the single dispatched entry must carry all 4 legs, grouped into one message"
        entry_accounts = {m["account"] for m in entry["matches"]}
        assert entry_accounts == {"ZG0790", "ZJ6294"}

    def test_two_separate_ticks_never_merge_into_one_entry(self):
        """Legs becoming eligible on DIFFERENT ticks must send as
        separate messages — this must never become 'wait and batch
        across cycles'."""
        agent = _Agent()
        pending_dispatches: list = []

        fired1 = _cycle_maybe_buffer_fire(
            agent, [_leg_match("ZG0790", "GOLD26NOV123000CE")],
            now=_now(),
            bypass_suppression=False, bypass_schedule=True, sim_mode=False,
            alert_state={}, cfg={"cooldown_min": 30}, broadcast_fn=None,
            debounce_min=0, pending_dispatches=pending_dispatches,
        )
        assert fired1 is True
        assert len(pending_dispatches) == 1

        # A later, independent tick — a DIFFERENT leg becomes eligible.
        # Modeled as a fresh call (the real engine loops ticks and calls
        # this once per agent per tick); each call's own fire is
        # buffered as its own, separate entry — never merged with the
        # first tick's entry, which the test leaves untouched above.
        fired2 = _cycle_maybe_buffer_fire(
            agent, [_leg_match("ZJ6294", "GOLD26NOV126000PE")],
            now=_now(),
            bypass_suppression=False, bypass_schedule=True, sim_mode=False,
            alert_state={}, cfg={"cooldown_min": 30}, broadcast_fn=None,
            debounce_min=0, pending_dispatches=pending_dispatches,
        )
        assert fired2 is True
        assert len(pending_dispatches) == 2, (
            "a second, later tick's fire must append its OWN entry, "
            "never merge into the first tick's already-buffered entry"
        )
        first_symbols = {m["row"]["tradingsymbol"] for m in pending_dispatches[0]["matches"]}
        second_symbols = {m["row"]["tradingsymbol"] for m in pending_dispatches[1]["matches"]}
        assert first_symbols == {"GOLD26NOV123000CE"}
        assert second_symbols == {"GOLD26NOV126000PE"}
        assert first_symbols.isdisjoint(second_symbols)
