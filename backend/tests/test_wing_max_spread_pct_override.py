"""
Tests for the new per-template/per-order `wing_max_spread_pct` field —
the Chain-tab Spread% threshold shown alongside TP%/SL% (operator
brief: "there should be a threshold for spread ... shown along tp%,
sl%, with some default value which can be changed").

Covers the override-threading chain, mirroring the existing tp_pct/
sl_pct override-resolution test pattern in test_resolve_template_plan.py:

  OrderTemplate.wing_max_spread_pct (DB column) → OrderTemplateOut
      → TemplateBar shows it as the Spread% field's default/placeholder
  TicketOrderRequest/TicketPreviewRequest.wing_max_spread_pct_override
      → _ticket_overrides_dict (operator override, persisted for
        retry/replay via _build_overrides_json)
  resolve_max_spread_pct(template, overrides) → override > template > setting
      — the live Chain-tab gate (GET /api/orders/spread-check) calls
      this with template=None and the query param as the override tier
      (the frontend already resolves template-default-vs-operator-
      override client-side before calling that endpoint); this
      resolver's template tier remains available for any future
      server-side caller (e.g. a declarative-agent-grammar metric) that
      does have a loaded template dict in hand.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.api.algo.spread_check import resolve_max_spread_pct
from backend.api.routes.orders_helpers import _ticket_overrides_dict, _build_overrides_json
from backend.api.routes.templates import _to_out
from backend.api.schemas import TicketOrderRequest, TicketPreviewRequest


# ── OrderTemplateOut — template default surfaces to the frontend ──────────

class _FakeOrderTemplateRow:
    """Minimal stand-in for the OrderTemplate ORM row — only the
    attributes templates.py's _to_out reads."""
    def __init__(self, wing_max_spread_pct=None):
        self.id = 1
        self.slug = "test-template"
        self.name = "Test Template"
        self.description = ""
        self.applies_to = "both"
        self.tp_pct = 10.0
        self.sl_pct = 5.0
        self.wing_premium_pct = None
        self.wing_strike_offset = None
        self.wing_max_spread_pct = wing_max_spread_pct
        self.tp_order_type = "LIMIT"
        self.tp_scales_json = None
        self.sl_trail_pct = None
        self.is_default = False
        self.is_system = False
        self.is_active = True


class TestOrderTemplateOutCarriesSpreadDefault:
    def test_row_value_surfaced_to_schema(self):
        row = _FakeOrderTemplateRow(wing_max_spread_pct=7.5)
        out = _to_out(row)
        assert out.wing_max_spread_pct == 7.5

    def test_row_none_surfaced_as_none_not_dropped(self):
        row = _FakeOrderTemplateRow(wing_max_spread_pct=None)
        out = _to_out(row)
        assert out.wing_max_spread_pct is None


# ── _ticket_overrides_dict — operator override extracted from request ────

class TestTicketOverridesDictCarriesSpreadOverride:
    def test_ticket_order_request_override_extracted(self):
        data = TicketOrderRequest(
            mode="paper", side="BUY", tradingsymbol="RELIANCE",
            quantity=1, account="ACC1",
            wing_max_spread_pct_override=4.0,
        )
        overrides = _ticket_overrides_dict(data)
        assert overrides["wing_max_spread_pct"] == 4.0

    def test_ticket_preview_request_override_extracted(self):
        data = TicketPreviewRequest(
            mode="paper", side="BUY", tradingsymbol="RELIANCE",
            quantity=1, account="ACC1",
            wing_max_spread_pct_override=6.5,
        )
        overrides = _ticket_overrides_dict(data)
        assert overrides["wing_max_spread_pct"] == 6.5

    def test_absent_override_is_none_not_dropped(self):
        data = TicketOrderRequest(
            mode="paper", side="BUY", tradingsymbol="RELIANCE",
            quantity=1, account="ACC1",
        )
        overrides = _ticket_overrides_dict(data)
        assert overrides["wing_max_spread_pct"] is None

    def test_other_override_struct_without_the_field_does_not_raise(self):
        """_ticket_overrides_dict is also called by callers whose request
        struct predates this field (getattr-guarded)."""
        data = SimpleNamespace(
            tp_pct_override=None, sl_pct_override=None,
            wing_premium_pct_override=None, wing_strike_offset_override=None,
        )
        overrides = _ticket_overrides_dict(data)
        assert overrides["wing_max_spread_pct"] is None


# ── _build_overrides_json — persisted for retry/replay ────────────────────

class TestBuildOverridesJsonCarriesSpreadOverride:
    def test_override_serialized_into_json(self):
        leg = SimpleNamespace(
            tp_pct_override=None, sl_pct_override=None,
            wing_premium_pct_override=None, wing_strike_offset_override=None,
            wing_max_spread_pct_override=8.0,
            sl_trail_pct_override=None, tp_scales_json_override=None,
        )
        payload_json = _build_overrides_json(leg)
        assert payload_json is not None
        import json as _json
        payload = _json.loads(payload_json)
        assert payload["wing_max_spread_pct"] == 8.0

    def test_absent_override_not_included_in_json(self):
        leg = SimpleNamespace(
            tp_pct_override=30.0, sl_pct_override=None,
            wing_premium_pct_override=None, wing_strike_offset_override=None,
            wing_max_spread_pct_override=None,
            sl_trail_pct_override=None, tp_scales_json_override=None,
        )
        payload_json = _build_overrides_json(leg)
        import json as _json
        payload = _json.loads(payload_json)
        assert "wing_max_spread_pct" not in payload
        assert payload["tp_pct"] == 30.0

    def test_all_fields_none_returns_none(self):
        leg = SimpleNamespace(
            tp_pct_override=None, sl_pct_override=None,
            wing_premium_pct_override=None, wing_strike_offset_override=None,
            wing_max_spread_pct_override=None,
            sl_trail_pct_override=None, tp_scales_json_override=None,
        )
        assert _build_overrides_json(leg) is None


# ── resolve_max_spread_pct — override > template > setting ────────────────
# (generic resolver test; the live GET /api/orders/spread-check route only
# ever exercises the override/setting tiers — see test_orders_spread_check.py
# — but the template tier stays correct for any future server-side caller.)

class TestResolveMaxSpreadPctChain:
    def test_operator_override_from_ticket_request_wins(self):
        template = {"wing_max_spread_pct": 7.0}
        data = TicketPreviewRequest(
            mode="paper", side="BUY", tradingsymbol="RELIANCE",
            quantity=1, account="ACC1",
            wing_max_spread_pct_override=3.0,
        )
        overrides = _ticket_overrides_dict(data)
        value, source = resolve_max_spread_pct(template, overrides)
        assert value == 3.0
        assert source == "override"

    def test_saved_template_default_used_when_operator_leaves_field_blank(self):
        template = {"wing_max_spread_pct": 7.0}
        data = TicketPreviewRequest(
            mode="paper", side="BUY", tradingsymbol="RELIANCE",
            quantity=1, account="ACC1",
        )
        overrides = _ticket_overrides_dict(data)
        value, source = resolve_max_spread_pct(template, overrides)
        assert value == 7.0
        assert source == "template"

    def test_global_admin_setting_used_when_template_has_no_value_either(self):
        """Operator's brief: 'the global setting's current value as the
        DEFAULT' — a template never saved with this field, no override
        supplied, must resolve to the admin setting (10.0 by default)."""
        row = _FakeOrderTemplateRow(wing_max_spread_pct=None)
        template_out = _to_out(row)
        template = {"wing_max_spread_pct": template_out.wing_max_spread_pct}
        data = TicketPreviewRequest(
            mode="paper", side="BUY", tradingsymbol="RELIANCE",
            quantity=1, account="ACC1",
        )
        overrides = _ticket_overrides_dict(data)
        with patch(
            "backend.shared.helpers.settings.get_float", return_value=10.0,
        ):
            value, source = resolve_max_spread_pct(template, overrides)
        assert value == 10.0
        assert source == "setting"
