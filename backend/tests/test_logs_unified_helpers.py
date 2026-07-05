"""
Unit tests for the logs.py helper decomposition.

Covers the pure helpers extracted from LogsController.unified_log during the
cyclomatic-complexity refactor (Jul 2026). Behaviour-parity tests only —
no HTTP-layer or DB fixtures required.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.api.routes.logs import (
    _agent_row_survives_filters,
    _build_agent_row,
    _build_order_row,
    _extract_agent_account,
    _identity,
    _mask_account,
    _mask_payload,
    _order_matches_account,
    _parse_csv_set,
    _parse_since,
    _parse_sim_filter,
)
from litestar.exceptions import HTTPException


# ---------------------------------------------------------------------------
# Query-param parsers
# ---------------------------------------------------------------------------

def test_parse_csv_set_empty_returns_empty():
    assert _parse_csv_set("") == set()


def test_parse_csv_set_strips_and_drops_empties():
    assert _parse_csv_set("a, b,,  c ,") == {"a", "b", "c"}


@pytest.mark.parametrize("raw,expected", [
    ("", None), ("TRUE", True), ("true", True),
    ("FALSE", False), ("false", False), ("other", None),
])
def test_parse_sim_filter(raw, expected):
    assert _parse_sim_filter(raw) is expected


def test_parse_since_empty_returns_none():
    assert _parse_since("") is None


def test_parse_since_zulu_normalised_to_utc():
    dt = _parse_since("2026-01-02T03:04:05Z")
    assert dt == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_parse_since_naive_gets_utc_tzinfo():
    dt = _parse_since("2026-01-02T03:04:05")
    assert dt.tzinfo == timezone.utc


def test_parse_since_bad_format_raises_400():
    with pytest.raises(HTTPException) as ei:
        _parse_since("not-a-date")
    assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# Mask helpers + identity
# ---------------------------------------------------------------------------

def test_mask_account_masks_kite_code():
    assert _mask_account("ZG0790") == "ZG####"


def test_mask_account_none_passthrough():
    assert _mask_account(None) is None


def test_mask_payload_masks_kite_code_embedded():
    assert _mask_payload('{"account":"DH6847"}') == '{"account":"DH####"}'


def test_mask_payload_none_passthrough():
    assert _mask_payload(None) is None


def test_identity_returns_input():
    assert _identity("x") == "x"
    assert _identity(None) is None
    assert _identity(42) == 42


# ---------------------------------------------------------------------------
# Order-row helpers
# ---------------------------------------------------------------------------

def test_order_matches_account_no_filter_always_true():
    assert _order_matches_account("ZG0790", None, set()) is True
    assert _order_matches_account(None, None, set()) is True


def test_order_matches_account_hit_on_account_col():
    assert _order_matches_account("ZG0790", None, {"ZG0790"}) is True


def test_order_matches_account_hit_on_payload():
    assert _order_matches_account("OTHER", '{"a":"DH6847"}', {"DH6847"}) is True


def test_order_matches_account_miss():
    assert _order_matches_account("ZJ6294", None, {"ZG0790"}) is False


def test_build_order_row_shapes_all_fields():
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    oe = SimpleNamespace(
        id=99, ts=ts, kind="fill", message="hi",
        order_id=42, payload_json='{"account":"ZG0790"}',
    )
    row = _build_order_row(oe, "ZG0790", mask=_mask_account, mask_p=_mask_payload)
    assert row.id == 99
    assert row.source == "order"
    assert row.ts == ts.isoformat()
    assert row.kind == "fill"
    assert row.message == "hi"
    assert row.order_id == 42
    assert row.agent_slug is None
    assert row.account == "ZG####"
    assert row.payload_json == '{"account":"ZG####"}'
    assert row.sim_mode is False


def test_build_order_row_null_ts_yields_empty_string():
    oe = SimpleNamespace(
        id=1, ts=None, kind="k", message=None, order_id=None, payload_json=None,
    )
    row = _build_order_row(oe, None, mask=_identity, mask_p=_identity)
    assert row.ts == ""
    assert row.message == ""


# ---------------------------------------------------------------------------
# Agent-row helpers
# ---------------------------------------------------------------------------

def test_extract_agent_account_finds_kite_code():
    ae = SimpleNamespace(detail="fired for ZG0790", trigger_condition=None)
    assert _extract_agent_account(ae) == "ZG0790"


def test_extract_agent_account_falls_back_to_trigger():
    ae = SimpleNamespace(detail=None, trigger_condition="acct=DH6847")
    assert _extract_agent_account(ae) == "DH6847"


def test_extract_agent_account_none_when_absent():
    ae = SimpleNamespace(detail="nothing here", trigger_condition=None)
    assert _extract_agent_account(ae) is None


def test_build_agent_row_defaults_missing_kind():
    ts = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)
    ae = SimpleNamespace(
        id=7, timestamp=ts, event_type=None,
        detail="fired ZG0790", trigger_condition=None, sim_mode=False,
    )
    row = _build_agent_row(ae, "loss-firm", mask=_mask_account)
    assert row.kind == "agent_fire"
    assert row.agent_slug == "loss-firm"
    assert row.account == "ZG####"
    assert row.sim_mode is False


def test_build_agent_row_sim_mode_flag():
    ae = SimpleNamespace(
        id=1, timestamp=None, event_type="agent_match",
        detail=None, trigger_condition=None, sim_mode=True,
    )
    row = _build_agent_row(ae, "sim-agent", mask=_identity)
    assert row.sim_mode is True
    assert row.ts == ""


def test_agent_row_kind_filter_pushes_through():
    ae = SimpleNamespace(event_type="agent_fire", detail=None, trigger_condition=None)
    assert _agent_row_survives_filters(ae, kind_set=set(), acct_set=set()) is True
    assert _agent_row_survives_filters(ae, kind_set={"agent_fire"}, acct_set=set()) is True
    assert _agent_row_survives_filters(ae, kind_set={"other"}, acct_set=set()) is False


def test_agent_row_account_filter_matches_via_regex():
    ae = SimpleNamespace(event_type="x", detail="ZG0790 hit", trigger_condition=None)
    assert _agent_row_survives_filters(ae, kind_set=set(), acct_set={"ZG0790"}) is True


def test_agent_row_account_filter_rejects_mismatch():
    ae = SimpleNamespace(event_type="x", detail="ZJ6294 hit", trigger_condition=None)
    assert _agent_row_survives_filters(ae, kind_set=set(), acct_set={"ZG0790"}) is False


def test_agent_row_account_filter_ignored_when_no_account_extractable():
    ae = SimpleNamespace(event_type="x", detail="no code", trigger_condition=None)
    # When there's no extractable account, filter is skipped — matches
    # legacy behavior in the original if branch.
    assert _agent_row_survives_filters(ae, kind_set=set(), acct_set={"ZG0790"}) is True
