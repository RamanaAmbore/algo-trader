"""
Regression test for the task_rows index mis-mapping in
_action_live_chase_close_positions.

Bug: `chase_tasks` has fewer entries than `rows` whenever any position is
preflight-blocked. The error-results loop used `rows[i]` (index into the
unfiltered list) instead of `task_rows[i]` (index into the tasks-only list).
On any run where >=1 position is blocked, the failure alert and log reference
the wrong account/symbol/quantity.

Fix: build a parallel `task_rows` list alongside `chase_tasks` and use
`task_rows[i]` in the error loop.

Test structure:
  - 3 positions: pos1 (blocked), pos2 (raises), pos3 (succeeds)
  - run_preflight patched: returns ok=False for pos1, ok=True for pos2+pos3
  - chase_order patched: raises RuntimeError for the first task (pos2),
    returns normally for the second task (pos3)
  - Assert the failure alert references pos2's symbol — NOT pos1's symbol
    (the mis-mapped index would produce pos1's symbol with the bug).
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call
import pandas as pd


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_agent_close_guards.py conventions)
# ---------------------------------------------------------------------------

def _make_agent(slug: str = "test-agent") -> MagicMock:
    agent = MagicMock()
    agent.slug = slug
    agent.id   = 1
    return agent


def _make_broker_stub() -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [{"initial": {"total": 10_000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500_000.0},
        "commodity": {"enabled": True, "net": 500_000.0},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _make_positions_df() -> pd.DataFrame:
    """
    Three rows:
      - pos1: CRUDEOILAUG25FUT / account=ACC1  — will be preflight-blocked
      - pos2: GOLDAUG25FUT     / account=ACC2  — task raises RuntimeError
      - pos3: SILVERAUG25FUT   / account=ACC3  — task succeeds
    """
    return pd.DataFrame([
        {
            "account":       "ACC1",
            "tradingsymbol": "CRUDEOILAUG25FUT",
            "exchange":      "MCX",
            "quantity":      100,
            "last_price":    7500.0,
            "close_price":   7450.0,
        },
        {
            "account":       "ACC2",
            "tradingsymbol": "GOLDAUG25FUT",
            "exchange":      "MCX",
            "quantity":      100,
            "last_price":    72000.0,
            "close_price":   71900.0,
        },
        {
            "account":       "ACC3",
            "tradingsymbol": "SILVERAUG25FUT",
            "exchange":      "MCX",
            "quantity":      100,
            "last_price":    88000.0,
            "close_price":   87500.0,
        },
    ])


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chase_close_error_loop_uses_task_rows_not_rows():
    """
    With pos1 blocked, chase_tasks has 2 entries (pos2, pos3).
    chase_order raises for pos2 (the FIRST task = index 0 in results).

    BUG: results[0] matched against rows[0] → alert fires for pos1 (ACC1/CRUDEOIL).
    FIX: results[0] matched against task_rows[0] → alert fires for pos2 (ACC2/GOLD).

    The test asserts the failure alert references ACC2/GOLDAUG25FUT.
    """
    from backend.api.algo.actions import _action_live_chase_close_positions

    agent   = _make_agent()
    broker  = _make_broker_stub()
    df      = _make_positions_df()
    context = {"df_positions": df}
    params  = {}

    # preflight: block only the first position (pos1 = CRUDEOILAUG25FUT/ACC1)
    _pf_call_count = 0

    async def mock_preflight(account, order_dict):
        nonlocal _pf_call_count
        _pf_call_count += 1
        if _pf_call_count == 1:
            # First call → blocked
            return {
                "ok": False,
                "blocked": [{"code": "G1_LOT_MULTIPLE", "reason": "sub-lot test block"}],
            }
        # Subsequent calls → pass
        return {"ok": True, "blocked": []}

    # chase_order: raises for the first task (pos2), succeeds for the second (pos3)
    _chase_call_count = 0

    async def mock_chase(*, account, symbol, transaction_type, quantity, cfg):
        nonlocal _chase_call_count
        _chase_call_count += 1
        if _chase_call_count == 1:
            raise RuntimeError("broker timeout — intentional test failure")
        return "FILLED"

    alerted_with: list[dict] = []

    def mock_send_alert(**kwargs):
        alerted_with.append(dict(kwargs))

    with patch("backend.api.algo.actions.run_preflight",
               new=mock_preflight), \
         patch("backend.api.algo.chase.chase_order",
               new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.brokers.registry.get_broker",
               return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",
               return_value=False), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
               new=mock_send_alert):

        await _action_live_chase_close_positions(agent, context, params)

    # Exactly one preflight-blocked alert (pos1) and one chase-failure alert (pos2).
    # The total alert count is 2: one block alert (from the preflight path) +
    # one failure alert (from the gather error loop).
    assert len(alerted_with) == 2, (
        f"expected 2 alerts total (1 blocked + 1 task failure), got {len(alerted_with)}: "
        f"{alerted_with}"
    )

    # The gather-error alert (second one) must reference pos2 — GOLDAUG25FUT / ACC2.
    failure_alert = alerted_with[1]
    assert failure_alert.get("symbol") == "GOLDAUG25FUT", (
        f"failure alert should reference GOLDAUG25FUT (pos2), "
        f"got symbol={failure_alert.get('symbol')!r}. "
        f"This indicates rows[i] is still used instead of task_rows[i]."
    )
    assert failure_alert.get("account") == "ACC2", (
        f"failure alert should reference ACC2 (pos2), "
        f"got account={failure_alert.get('account')!r}."
    )

    # Negative assertion: the failure alert must NOT reference pos1's symbol/account.
    assert failure_alert.get("symbol") != "CRUDEOILAUG25FUT", (
        "failure alert incorrectly references pos1 (CRUDEOILAUG25FUT) — "
        "the rows[i] vs task_rows[i] index bug is present."
    )
    assert failure_alert.get("account") != "ACC1", (
        "failure alert incorrectly references pos1 (ACC1) — "
        "the rows[i] vs task_rows[i] index bug is present."
    )
