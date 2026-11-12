"""
2026-11 channel-authoring retirement — the `notify=[...]` action-call
parameter (Part B, 2026-10-12) is removed with NO replacement.

This file used to cover Part B's full safety-invariant suite (mode
gating, masking, success-only firing, per-tick throttle, notify-send
failure isolation, and an end-to-end real-log-handler round trip). All
of that coverage is gone along with the mechanism itself:
`actions.py:_al_maybe_notify_action`/`_al_notify_channels_for_log`/
`_al_notify_build_message`/`_al_notify_throttle_check`/
`_MAX_NOTIFY_PER_AGENT_PER_TICK`, and
`event_agents.py:_render_action_notify`, no longer exist.

What this file covers now:
  1. The retired symbols are genuinely gone from `actions.py` and
     `event_agents.py` — not just unused, but removed.
  2. `execute()`'s per-action loop no longer calls anything notify-
     shaped after a successful dispatch — a `notify` sibling key on an
     action is now simply inert data, never read.
  3. `routes/agents.py:_age_validate_action_entries` rejects any
     `actions` entry carrying a `notify` key, so a stale or
     operator-authored one can never round-trip through a save.
  4. `ACTION_NOTIFY_AGENT`/`action-notify-alert` is retired from
     `SEEDED_AGENTS` (deactivated on next startup, never deleted).
  5. `_action_place_order`'s real-outcome capture
     (`context["_action_result_detail"]`, actions_live.py) — this is
     NOT part of the notify mechanism (it reflects the real
     `ChaseResult.status`/`.detail` after `chase_order()` returns,
     independent of whatever consumed it before), so this coverage is
     preserved unchanged from the original Part B test file.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _make_agent(slug: str = "test-agent", id_: int = 1):
    agent = MagicMock()
    agent.slug = slug
    agent.id = id_
    agent.name = "Test Agent"
    return agent


# ---------------------------------------------------------------------------
# 1. The retired symbols are genuinely gone
# ---------------------------------------------------------------------------

def test_notify_action_symbols_removed_from_actions_module():
    import backend.api.algo.actions as actions_mod

    for name in (
        "_al_maybe_notify_action", "_al_notify_channels_for_log",
        "_al_notify_build_message", "_al_notify_throttle_check",
        "_MAX_NOTIFY_PER_AGENT_PER_TICK",
    ):
        assert not hasattr(actions_mod, name), (
            f"{name} must be removed — the notify=[...] action param "
            f"has no replacement"
        )


def test_notify_renderer_removed_from_event_agents_module():
    import backend.api.algo.event_agents as ea

    assert not hasattr(ea, "_render_action_notify")
    assert "action_notify" not in ea.RENDERS


def test_execute_source_no_longer_references_notify_wiring():
    """Structural guard — `execute()`'s own source must not call anything
    notify-shaped. Reading the source (not just behavior) catches a
    future re-introduction under a different name/call-site that a
    behavioral test alone might miss."""
    import inspect
    from backend.api.algo import actions as actions_mod

    src = inspect.getsource(actions_mod.execute)
    assert "notify" not in src.lower()


# ---------------------------------------------------------------------------
# 2. A `notify` sibling key on an action is now inert — never read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_ignores_a_stale_notify_key_on_a_successful_action():
    """An action dict carrying a leftover `notify` key (e.g. a row the
    DB migration hasn't normalized yet, or a hand-built test fixture)
    must not cause `execute()` to raise or to do anything special with
    it — the key is simply never read any more."""
    from backend.api.algo.actions import execute

    agent = _make_agent()
    actions = [
        {
            "type": "place_order",
            "params": {"account": "ZG0790", "symbol": "NIFTY25JULFUT",
                       "side": "SELL", "qty": 1},
            "notify": [{"channel": "telegram"}],
        },
    ]
    context: dict = {}

    mock_dispatch = AsyncMock(return_value=True)

    with patch("backend.api.algo.actions._resolve_mode", return_value="live"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=mock_dispatch), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils._DISPATCH_LOGGER") as mock_logger:
        await execute(agent, actions, context)

    mock_dispatch.assert_called_once()
    mock_logger.info.assert_not_called()
    mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# 3. `_age_validate_action_entries` rejects a `notify` key
# ---------------------------------------------------------------------------

def test_age_validate_action_entries_rejects_notify_key():
    from backend.api.routes.agents import _age_validate_action_entries
    from litestar.exceptions import HTTPException

    actions = [
        {"type": "place_order", "params": {"account": "ZG0790"},
         "notify": [{"channel": "telegram"}]},
    ]
    with pytest.raises(HTTPException) as exc_info:
        _age_validate_action_entries(actions)
    assert "notify" in str(exc_info.value.detail).lower()


def test_age_validate_action_entries_accepts_actions_with_no_notify_key():
    from backend.api.routes.agents import _age_validate_action_entries

    # Must not raise — a real, fully-specified action with no `notify`
    # key sails through unchanged.
    _age_validate_action_entries([
        {"type": "place_order", "params": {
            "account": "ZG0790", "symbol": "NIFTY25JULFUT",
            "side": "BUY", "qty": 1,
        }},
    ])


# ---------------------------------------------------------------------------
# 2026-11-08 audit fix (item 9) — `_age_validate_preamble_entries` rejects
# a `notify` key on a `preamble` entry too, closing the gap
# `_age_validate_action_entries` (actions-only) left open.
# ---------------------------------------------------------------------------

def test_age_validate_preamble_entries_rejects_notify_key():
    from backend.api.routes.agents import _age_validate_preamble_entries
    from litestar.exceptions import HTTPException

    preamble = [
        {"type": "log", "params": {"message": "x"}, "notify": [{"channel": "telegram"}]},
    ]
    with pytest.raises(HTTPException) as exc_info:
        _age_validate_preamble_entries(preamble)
    assert "notify" in str(exc_info.value.detail).lower()


def test_age_validate_preamble_entries_accepts_alert_shaped_entry():
    """An `alert(...)` preamble entry (`{"type": "alert", "events":
    [...]}`, no `params` key, and `"alert"` is not itself a known
    dispatchable action type) must NOT be rejected — this is exactly
    why `_age_validate_preamble_entries` does not reuse the full
    `_age_validate_action_entries` validator wholesale."""
    from backend.api.routes.agents import _age_validate_preamble_entries

    _age_validate_preamble_entries([
        {"type": "alert", "events": [{"log_tag": "demo", "render": "generic_notice"}]},
    ])


def test_age_validate_preamble_entries_accepts_none_and_empty():
    from backend.api.routes.agents import _age_validate_preamble_entries

    _age_validate_preamble_entries(None)
    _age_validate_preamble_entries([])


# ---------------------------------------------------------------------------
# 4. ACTION_NOTIFY_AGENT is retired, not deleted
# ---------------------------------------------------------------------------

def test_action_notify_agent_retired_from_seeded_agents():
    from backend.api.algo import event_agents as ea

    assert ea.ACTION_NOTIFY_AGENT not in ea.SEEDED_AGENTS
    assert ea.ACTION_NOTIFY_AGENT["slug"] not in {s["slug"] for s in ea.SEEDED_AGENTS}
    assert ea.ACTION_NOTIFY_AGENT["slug"] in ea._RETIRED_SLUGS


@pytest.mark.asyncio
async def test_retire_collapsed_event_agents_deactivates_action_notify_row():
    """`_retire_collapsed_event_agents()` deactivates an already-seeded
    `action-notify-alert` row the exact same way it deactivates the 16
    collapsed detectors — reuses that function's existing query, which
    now also matches this slug via the unioned `_RETIRED_SLUGS`."""
    from types import SimpleNamespace
    from backend.api.algo import event_agents as ea

    row = SimpleNamespace(slug="action-notify-alert", status="active")

    class _FakeScalars:
        def __init__(self, rows):
            self._rows = rows
        def all(self):
            return self._rows

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows
        def scalars(self):
            return _FakeScalars(self._rows)

    class _FakeSession:
        async def execute(self, *a, **k):
            return _FakeResult([row])

    await ea._retire_collapsed_event_agents(_FakeSession())
    assert row.status == "inactive"


# ---------------------------------------------------------------------------
# 5. _action_place_order's real-outcome capture — unrelated to notify,
#    preserved unchanged from the original Part B test file.
# ---------------------------------------------------------------------------

def _make_conns_stub(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


def _make_broker_stub(*, ltp_value: float = 23500.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [{"initial": {"total": 5_000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500_000.0},
        "commodity": {"enabled": True, "net": 500_000.0},
    }
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": ltp_value}}
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


@pytest.mark.asyncio
@pytest.mark.parametrize("status_name,detail_text", [
    ("FAILED", "Failed after 20 attempts"),
    ("PARTIAL", "partial fill 50/100"),
    ("CANCELLED", "operator cancelled"),
])
async def test_action_place_order_captures_real_chase_outcome(status_name, detail_text):
    """mock chase_order returning a terminal non-FILLED ChaseResult —
    context['_action_result_detail'] must reflect that real status/detail
    after _action_place_order returns."""
    from backend.api.algo.actions import _action_place_order
    from backend.api.algo.chase import ChaseResult, ChaseStatus

    broker = _make_broker_stub()
    conns = _make_conns_stub("ZG0790")
    agent = _make_agent()
    context: dict = {}
    params = {
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "exchange": "NFO",
        "transaction_type": "SELL",
        "quantity": 50,
    }

    result = ChaseResult(status=ChaseStatus[status_name], detail=detail_text)
    mock_chase = AsyncMock(return_value=result)

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=50)), \
         patch("backend.api.algo.chase.chase_order", new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order", new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.actions_live._place_order_set_product_template", new=AsyncMock()), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False):
        await _action_place_order(agent, context, params)

    mock_chase.assert_called_once()
    assert context["_action_result_detail"] == {
        "status": status_name.lower(),
        "detail": detail_text,
    }
