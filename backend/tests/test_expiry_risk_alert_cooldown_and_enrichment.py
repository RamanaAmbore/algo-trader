"""Tests for the 2026-11-26 expiry-risk-alert volume reduction +
enrichment fix (operator: "too many alerts on the previous day of
expiry of mcx. need to reduce it. make it informative.").

Two parts, covered separately:
  1. `_EXPIRY_RISK_AGENT_DEFAULTS["cooldown_minutes"]` raised 60 -> 240
     for both `expiry-nfo-risk-alert` and `expiry-mcx-risk-alert` — the
     shared dict both agents pull their default cooldown from (NOT the
     `positions.expiring_today` 1.5-day lookahead window itself, which
     is deliberately left unchanged — see that scope's own docstring).
  2. `_v2_expiry_risk_alert_enrichment()` — a new per-leg enrichment
     block, slug-gated and appended (never replacing) the generic
     kind/threshold table in `_v2_send_rich_alert`, mirroring the
     existing `exit-tp-short-option-90` enrichment precedent exactly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from backend.api.algo.agent_engine import (
    BUILTIN_AGENTS,
    _v2_expiry_risk_alert_enrichment,
    _v2_send_rich_alert,
)
from backend.api.algo.events import EvalResult


def _by_slug(slug: str) -> dict:
    return next(a for a in BUILTIN_AGENTS if a["slug"] == slug)


def _now():
    return datetime(2026, 11, 25, 6, 0, 0, tzinfo=timezone.utc)  # ~11:30 IST


# ═══════════════════════════════════════════════════════════════════════
#  Part 1 — cooldown raised 60 -> 240 for both agents
# ═══════════════════════════════════════════════════════════════════════

class TestCooldownRaisedTo240:
    def test_nfo_cooldown_is_240(self):
        agent = _by_slug("expiry-nfo-risk-alert")
        assert agent["cooldown_minutes"] == 240

    def test_mcx_cooldown_is_240(self):
        agent = _by_slug("expiry-mcx-risk-alert")
        assert agent["cooldown_minutes"] == 240

    def test_both_agents_share_the_same_defaults_dict(self):
        """A single shared dict is the only lever changed for volume —
        confirms editing `_EXPIRY_RISK_AGENT_DEFAULTS` once reaches
        both agents, rather than each carrying its own duplicated
        literal that could drift."""
        nfo = _by_slug("expiry-nfo-risk-alert")
        mcx = _by_slug("expiry-mcx-risk-alert")
        assert nfo["cooldown_minutes"] == mcx["cooldown_minutes"] == 240

    def test_lookahead_window_in_scope_resolver_is_unchanged(self):
        """The volume fix must come ONLY from the cooldown, never from
        narrowing `positions.expiring_today`'s 1.5-day lookahead ceiling
        — narrowing that would reduce the early-warning behaviour other
        callers of the same scope share, which is out of scope here."""
        import inspect
        from backend.api.algo.grammar_tokens import scope_positions_expiring_today as mod
        src = inspect.getsource(mod._scope_positions_expiring_today)
        assert "d <= 1.5" in src


# ═══════════════════════════════════════════════════════════════════════
#  Part 2 — _v2_expiry_risk_alert_enrichment()
# ═══════════════════════════════════════════════════════════════════════

_MCX_ROW = {
    "account": "ZG0790", "tradingsymbol": "GOLDM26DECFUT", "exchange": "MCX",
    "quantity": -1,
}
_MCX_ROW_2 = {
    "account": "ZJ6294", "tradingsymbol": "CRUDEOIL26DEC5500CE", "exchange": "MCX",
    "quantity": 2,
}
_NFO_ROW = {
    "account": "ZG0790", "tradingsymbol": "NIFTY26DEC24000CE", "exchange": "NFO",
    "quantity": -50,
}


def _match(row: dict) -> dict:
    return {"metric": "pnl", "scope": "positions.expiring_today.mcx_unhedged",
            "op": ">=", "value": 0.0, "threshold": -999999999, "row": row,
            "account": row["account"]}


class _Agent:
    def __init__(self, slug: str):
        self.id = 1
        self.slug = slug
        self.name = slug
        self.tier = "high"
        self.fire_at_time = None
        self.events = []
        self.actions = []


class TestEnrichmentContent:
    def test_lists_symbol_masked_account_and_dte(self):
        agent = _Agent("expiry-mcx-risk-alert")
        text = _v2_expiry_risk_alert_enrichment(
            agent, [_match(_MCX_ROW)], {"now": _now(), "position_rows": [_MCX_ROW]},
        )
        assert text is not None
        assert "GOLDM26DECFUT" in text
        assert "ZG0790" not in text, "raw account must never leak unmasked"
        assert ", in " in text and "d)" in text, (
            "expiry date + day-count belongs once in the group header, "
            "not repeated per leg row"
        )

    def test_multiple_legs_all_listed(self):
        agent = _Agent("expiry-mcx-risk-alert")
        text = _v2_expiry_risk_alert_enrichment(
            agent, [_match(_MCX_ROW), _match(_MCX_ROW_2)],
            {"now": _now(), "position_rows": [_MCX_ROW, _MCX_ROW_2]},
        )
        assert "GOLDM26DECFUT" in text
        assert "CRUDEOIL26DEC5500CE" in text

    def test_legs_sharing_one_expiry_date_render_under_one_header_not_repeated(self):
        """2026-11-26 operator follow-up: 'no need to repeat expiry date
        for each symbol row'. Two legs on the same underlying/month (so
        they share the exact same real expiry date) must render under
        ONE header carrying the date once — not once per leg row."""
        agent = _Agent("expiry-nfo-risk-alert")
        row_a = {"account": "ZG0790", "tradingsymbol": "NIFTY26DEC24000CE", "exchange": "NFO"}
        row_b = {"account": "ZJ6294", "tradingsymbol": "NIFTY26DEC25000PE", "exchange": "NFO"}
        text = _v2_expiry_risk_alert_enrichment(
            agent, [_match(row_a), _match(row_b)],
            {"now": _now(), "position_rows": [row_a, row_b]},
        )
        assert text.count("in ") == 1 and text.count("d)") == 1, (
            "same-expiry legs must share one date header, not one per row"
        )
        assert "NIFTY26DEC24000CE" in text and "NIFTY26DEC25000PE" in text
        # Only one header line (the one ending in ":") for both legs.
        header_lines = [ln for ln in text.splitlines() if ln.endswith(":")]
        assert len(header_lines) == 1

    def test_duplicate_leg_from_a_second_unkeyed_leaf_is_not_repeated(self):
        """The condition's suppression leaf (a second, TOTAL-scoped leaf
        with no row of its own) can surface alongside the real leg's own
        match for the same firing cycle — the same `_eval_all` mechanism
        `_v2_reminder_only_line`'s own docstring describes. One expiring
        leg must render exactly once."""
        agent = _Agent("expiry-mcx-risk-alert")
        text = _v2_expiry_risk_alert_enrichment(
            agent, [_match(_MCX_ROW), _match(_MCX_ROW)],
            {"now": _now(), "position_rows": [_MCX_ROW]},
        )
        assert text.count("GOLDM26DECFUT") == 1

    def test_empty_matches_returns_none(self):
        agent = _Agent("expiry-mcx-risk-alert")
        assert _v2_expiry_risk_alert_enrichment(agent, [], {"now": _now()}) is None

    def test_mcx_header_says_unhedged(self):
        agent = _Agent("expiry-mcx-risk-alert")
        text = _v2_expiry_risk_alert_enrichment(
            agent, [_match(_MCX_ROW)], {"now": _now(), "position_rows": [_MCX_ROW]},
        )
        assert text.startswith("Unhedged legs (")

    def test_nfo_header_does_not_say_unhedged(self):
        """NFO fires on ITM options or open futures — not a netting/
        hedge concept at all — so its header must never borrow MCX's
        'unhedged' phrasing (would misstate why the alert fired)."""
        agent = _Agent("expiry-nfo-risk-alert")
        text = _v2_expiry_risk_alert_enrichment(
            agent, [_match(_NFO_ROW)], {"now": _now(), "position_rows": [_NFO_ROW]},
        )
        assert not text.lower().startswith("unhedged")
        assert "NIFTY26DEC24000CE" in text

    def test_malformed_symbol_still_renders_without_dte_phrase(self):
        """A DTE-formatting failure (unparseable symbol, no expiry) must
        never drop the leg from the list — only the trailing phrase is
        omitted."""
        row = {"account": "ZG0790", "tradingsymbol": "NOTAREALSYMBOL", "exchange": "MCX"}
        agent = _Agent("expiry-mcx-risk-alert")
        text = _v2_expiry_risk_alert_enrichment(
            agent, [_match(row)], {"now": _now(), "position_rows": [row]},
        )
        assert "NOTAREALSYMBOL" in text


# ═══════════════════════════════════════════════════════════════════════
#  Part 3 — dispatch-branch wiring inside _v2_send_rich_alert
# ═══════════════════════════════════════════════════════════════════════

async def _capture_rich_alert(agent, matches, context):
    from backend.api.algo import agent_engine as ae_mod
    calls = []
    with patch.object(ae_mod.logger, "log",
                       side_effect=lambda level, msg, **kw: calls.append(kw.get("extra"))):
        sent = await _v2_send_rich_alert(
            agent, matches, _now(), sim_mode=False, context=context,
            eval_result=EvalResult(triggered=True, condition_text="x", detail={}),
        )
    assert sent is True
    assert len(calls) == 1
    return calls[0]


class TestEnrichmentAppendedAtDispatch:
    @pytest.mark.asyncio
    async def test_mcx_agent_gets_enrichment_appended_to_generic_table(self):
        agent = _Agent("expiry-mcx-risk-alert")
        extra = await _capture_rich_alert(
            agent, [_match(_MCX_ROW)], {"now": _now(), "position_rows": [_MCX_ROW]},
        )
        assert "GOLDM26DECFUT" in extra["tg_table"]
        assert "GOLDM26DECFUT" in extra["email_table_html"]

    @pytest.mark.asyncio
    async def test_nfo_agent_gets_enrichment_appended_to_generic_table(self):
        agent = _Agent("expiry-nfo-risk-alert")
        extra = await _capture_rich_alert(
            agent, [_match(_NFO_ROW)], {"now": _now(), "position_rows": [_NFO_ROW]},
        )
        assert "NIFTY26DEC24000CE" in extra["tg_table"]

    @pytest.mark.asyncio
    async def test_unrelated_agent_gets_no_enrichment(self):
        """The generic kind/threshold table itself may legitimately
        mention the symbol for an expiring-scope match (its own,
        separate per-row rendering) — what must NOT appear for an
        unrelated agent is THIS enrichment's own distinguishing header
        text, which only the two gated slugs ever produce."""
        agent = _Agent("some-other-agent")
        extra = await _capture_rich_alert(
            agent, [_match(_MCX_ROW)], {"now": _now(), "position_rows": [_MCX_ROW]},
        )
        assert "Unhedged legs:" not in extra["tg_table"]
        assert "ITM options / open futures needing action:" not in extra["tg_table"]

    def test_call_site_gates_both_slugs(self):
        """Structural guard mirroring the existing exit-tp precedent's
        own source-inspection test — confirms the gate actually names
        both slugs at the real call site, not just the helper being
        independently callable."""
        import inspect
        from backend.api.algo import agent_engine as ae_mod
        src = inspect.getsource(ae_mod._v2_send_rich_alert)
        assert "'expiry-nfo-risk-alert'" in src
        assert "'expiry-mcx-risk-alert'" in src
