"""2026-11 alert-noise-reduction plan, Component A — consolidate duplicate
rich-alert bullets. Operator complaint: several leaves of the same agent
firing together for one (section, scope) in a single cycle rendered as N
near-identical blocks, repeating the section/scope header and (usually)
the same-looking pnl.

`agent_engine._ae_group_alert_rows()` groups by (section, scope), ONCE,
upstream of both `_tg_alert_body` (Telegram/ntfy) and `_email_alert_body`
(email) — see its own docstring and `_v2_send_rich_alert`'s call site.

IMPORTANT, empirically confirmed via a 36-fire replay of real
`loss-positions-total` prod history (14 days): the plan's original
premise — "every leaf that fires in one cycle for the same (section,
scope) carries an identical pnl/pct" — is FALSE for this exact agent.
`_v2_extract_pnl_fields` derives `pnl` differently per METRIC, not per
scope: `day_val` reads `day_change_val`, every other Positions metric
reads lifetime `pnl`. All 19 multi-leaf groups in the real history
disagreed on pnl; zero agreed. So the header shows a shared value ONLY
when every member's own value genuinely matches; otherwise it shows a
bare count and each subline carries its own value — no information is
ever dropped, it just moves from a (non-existent) shared header onto the
per-leaf line.
"""
from __future__ import annotations

from backend.api.algo.agent_engine import (
    _ae_group_alert_rows,
    _ae_sort_alert_rows,
    _v2_match_to_alertrow,
)
from backend.shared.helpers.alert_utils import _tg_alert_body, _email_alert_body


# The real 2026-10-30 11:25:07 UTC fire of loss-positions-total (prod
# agent_events, pulled read-only via `ssh ramboq` during this
# investigation) — day_val + day_pct + pnl_rate_abs all crossed their
# thresholds in the same cycle. day_pct had already latched (cooldown),
# but `_v2_send_rich_alert` is passed the FULL `matches` list (not just
# the latched survivors) — see `_cycle_dispatch_survivors`'s
# `entry['matches'] = matches` — so all 3 render.
_REAL_FIRE_MATCHES = [
    {"metric": "day_val", "scope": "positions.total", "op": "<=", "threshold": -50000,
     "value": -518600.0,
     "row": {"account": "TOTAL", "pnl": -436800.0, "day_change_val": -518600.0,
             "day_change_percentage": -37.57018147571268,
             "day_change_pct_margin": -3.065064408460549},
     "account": "TOTAL", "underlying": None},
    {"metric": "day_pct", "scope": "positions.total", "op": "<=", "threshold": -2.0,
     "value": -3.065064408460549,
     "row": {"account": "TOTAL", "pnl": -436800.0, "day_change_val": -518600.0,
             "day_change_percentage": -37.57018147571268,
             "day_change_pct_margin": -3.065064408460549},
     "account": "TOTAL", "underlying": None},
    {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "threshold": -6000,
     "value": -15150.37112018757,
     "row": {"account": "TOTAL", "pnl": -436800.0, "day_change_val": -518600.0,
             "day_change_percentage": -37.57018147571268,
             "day_change_pct_margin": -3.065064408460549},
     "account": "TOTAL", "underlying": None},
]


def _build_rows(matches, **kwargs):
    rows = [_v2_match_to_alertrow(m, **kwargs) for m in matches]
    _ae_sort_alert_rows(rows)
    return rows


class TestRealMixedPnlGroupGoldenRender:
    """The real, mixed-pnl 3-leaf fire — the dominant case in practice.

    `_tg_rule_line`/`_tg_alert_body` must never drop a number: since the
    header can't show one agreed value, each subline carries its own."""

    def test_group_metadata(self):
        rows = _build_rows(_REAL_FIRE_MATCHES)
        grouped = _ae_group_alert_rows(rows)
        assert len(grouped) == 3
        assert all(g["_grp_size"] == 3 for g in grouped)
        assert [g["_grp_is_first"] for g in grouped] == [True, False, False]
        assert [g["_grp_is_last"] for g in grouped] == [False, False, True]
        # The real pnl values genuinely disagree (day_val vs lifetime pnl).
        assert all(g["_grp_pnl_agrees"] is False for g in grouped)
        pnls = {round(g["pnl"], 2) for g in grouped}
        assert pnls == {-518600.0, -436800.0}

    def test_telegram_golden_render(self):
        rows = _build_rows(_REAL_FIRE_MATCHES)
        grouped = _ae_group_alert_rows(rows)
        tg = _tg_alert_body(grouped)
        assert tg == (
            "POS TOTAL  x3\n"
            "  Static ₹  -₹5.19L  floor -₹50,000\n"
            "  Static %  -₹4.37L  floor -2.00%\n"
            "  Rate ₹/min  -₹4.37L  now -₹15K/min  floor -₹6,000/min"
        )
        # Header carries no value (never an arbitrarily-picked one) —
        # exactly one "x3" marker, on line 1 only.
        assert tg.count("x3") == 1
        assert tg.splitlines()[0] == "POS TOTAL  x3"
        # Every leaf's own number still appears somewhere.
        assert "-₹5.19L" in tg  # day_val's day_change_val
        assert "-₹4.37L" in tg  # day_pct's / pnl_rate_abs's lifetime pnl
        assert "-₹15K/min" in tg  # pnl_rate_abs's own rate
        for glyph in ("▸", "·", "•"):
            assert glyph not in tg

    def test_email_renders_one_row_per_leaf_with_own_values(self):
        rows = _build_rows(_REAL_FIRE_MATCHES)
        grouped = _ae_group_alert_rows(rows)
        html = _email_alert_body(grouped)
        # One <tr> per leaf (each shows its own honest Current/Rate/
        # Threshold — NOT consolidated, unlike Telegram's header), plus
        # the <thead> header row.
        assert html.count("<tr>") == 4
        assert "Static &#8377;" in html or "Static ₹" in html
        assert "Rate &#8377;/min" in html or "Rate ₹/min" in html


class TestAgreeingGroupGetsSharedHeader:
    """Constructed case: two leaves for the same scope whose pnl/pct
    genuinely match (e.g. two non-day_val metrics, both reading
    lifetime pnl from the same row) — the shared-header design the
    plan originally described IS still used, when it's actually safe."""

    def _two_agreeing_rows(self):
        return [
            {"section": "Positions", "scope": "TOTAL", "kind": "static_abs",
             "pnl": -200000.0, "pct": None, "threshold": "-₹100,000",
             "rate_val": None,
             "underlyings_breakdown": [{"underlying": "NIFTY", "pnl": -120000.0},
                                       {"underlying": "GOLD", "pnl": -80000.0}]},
            {"section": "Positions", "scope": "TOTAL", "kind": "rate_abs",
             "pnl": -200000.0, "pct": None, "threshold": "-₹6,000/min",
             "rate_val": -9000.0,
             "underlyings_breakdown": [{"underlying": "NIFTY", "pnl": -120000.0},
                                       {"underlying": "GOLD", "pnl": -80000.0}]},
        ]

    def test_group_metadata_agrees(self):
        grouped = _ae_group_alert_rows(self._two_agreeing_rows())
        assert all(g["_grp_pnl_agrees"] is True for g in grouped)
        # rate_abs is present in the group — the static-alert "rate:"
        # enrichment line must be suppressed (the rate_abs leaf's own
        # subline already shows the rate).
        assert all(g["_grp_rate_for_enrichment"] is None for g in grouped)

    def test_telegram_shared_header_no_duplicate_pnl(self):
        grouped = _ae_group_alert_rows(self._two_agreeing_rows())
        tg = _tg_alert_body(grouped)
        assert tg == (
            "POS TOTAL  -₹2.00L  x2\n"
            "  Static ₹  floor -₹100,000\n"
            "  Rate ₹/min  now -₹9K/min  floor -₹6,000/min\n"
            "  by und: NIFTY -₹1.20L, GOLD -₹80.0K"
        )
        # The shared value appears exactly once (in the header) — not
        # repeated on every subline, unlike the disagreeing case above.
        assert tg.count("₹2.00L") == 1

    def test_email_underlying_breakdown_emitted_once_not_per_leaf(self):
        grouped = _ae_group_alert_rows(self._two_agreeing_rows())
        html = _email_alert_body(grouped)
        # Without the fix, 2 leaves each carrying the same breakdown
        # payload would print "By underlying:" twice (the duplication
        # bug) — it must appear exactly once per (section, scope) group.
        assert html.count("By underlying") == 1
        assert html.count("NIFTY") == 1  # the breakdown itself isn't duplicated either


class TestSingleLeafUnchanged:
    """The dominant real-world case (17/36 of the real history) — a
    group of exactly one member must render byte-identical to the
    pre-grouping code, with no 'x1' marker and no behavior change."""

    def test_single_leaf_no_marker_no_own_pnl_repeat(self):
        match = {
            "metric": "day_val", "scope": "positions.total", "op": "<=",
            "threshold": -100000, "value": -142450.0,
            "row": {"account": "TOTAL", "pnl": -140100.0, "day_change_val": -142450.0,
                    "day_change_percentage": -4.861774744027303},
            "account": "TOTAL", "underlying": None,
        }
        rows = _build_rows([match])
        grouped = _ae_group_alert_rows(rows)
        assert grouped[0]["_grp_size"] == 1
        assert grouped[0]["_grp_pnl_agrees"] is True
        tg = _tg_alert_body(grouped)
        assert "x1" not in tg
        assert tg == "POS TOTAL  -₹1.42L\n  Static ₹  floor -₹100,000"

    def test_bare_ungrouped_row_list_still_works_as_group_of_one(self):
        """A caller that skips `_ae_group_alert_rows` entirely (every
        pre-existing test, e.g. test_mcx_preclose_agent.py /
        test_rowset_aggregates.py / test_alert_golden.py) must render
        exactly as it did before this feature — `.get(key, default)`
        fallbacks inside the renderers cover this."""
        row = {
            "section": "Funds", "scope": "ACC1", "kind": "negative_margin",
            "pnl": -5000.0, "pct": None, "threshold": "-₹0", "rate_val": None,
        }
        tg = _tg_alert_body([row])
        assert tg == "FND ACC1  -₹5K\n  Margin < 0  -₹0"
        html = _email_alert_body([row])
        assert html.count("<tr>") == 2  # header + the one row, no crash
