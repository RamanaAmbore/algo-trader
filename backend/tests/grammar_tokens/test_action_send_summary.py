"""Tests for grammar_tokens/action_send_summary.py — `send_summary`
action resolver.

Fresh coverage, written as part of Wave 1 (DSL token-unification plan,
Part A — actions). The resolver's dispatch-mechanism coverage (patching
`_action_send_summary` as a mock to prove `_al_run_noop_handler`/
`execute`'s asymmetric un-wrapped exception propagation) stays in
`test_actions_coverage.py`, unchanged — it resolves via `actions_live.py`'s
backward-compatible re-export.
"""

from unittest.mock import patch

import pytest

from backend.api.algo.grammar_tokens.action_send_summary import _action_send_summary


@pytest.mark.asyncio
async def test_noop_when_sum_holdings_missing():
    """`sum_holdings is None` (context never populated — e.g. a dry-run)
    must no-op rather than raise."""
    with patch("backend.shared.helpers.alert_utils.send_summary") as mock_send:
        await _action_send_summary({}, {})
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_calls_send_summary_once_per_segment():
    context = {
        "sum_holdings": {"total": 1},
        "sum_positions": {"total": 2},
        "df_margins": None,
        "ist_display": "2026-10-25 10:00 IST",
    }
    with patch("backend.shared.helpers.alert_utils.send_summary") as mock_send:
        await _action_send_summary(context, {"segments": ["equity", "commodity"], "summary_type": "open"})
    assert mock_send.call_count == 2
    labels = {call.kwargs.get("label") for call in mock_send.call_args_list}
    assert labels == {"Equity", "Commodity"}


@pytest.mark.asyncio
async def test_defaults_to_both_segments_and_open_summary():
    context = {
        "sum_holdings": {"total": 1}, "sum_positions": None,
        "df_margins": None, "ist_display": "",
    }
    with patch("backend.shared.helpers.alert_utils.send_summary") as mock_send:
        await _action_send_summary(context, {})
    assert mock_send.call_count == 2
    for call in mock_send.call_args_list:
        assert call.args[3] == "open"
