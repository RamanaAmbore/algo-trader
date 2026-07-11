"""
Regression test for P2 NameError in _action_live_close_position.

If get_broker() raises (registry cold, account not found), `broker` was never
bound and the diagnosis error handler raised NameError which was silently
swallowed as "diagnosis unavailable", hiding the real failure reason.

Fix: broker = None before the try block; if broker is None, set
diag = "broker resolve failed — no diagnosis available".

Covers:
  - Function does NOT raise an unhandled exception when get_broker raises.
  - Logged diagnosis string contains "broker resolve failed", not the old
    silent sentinel "diagnosis unavailable".
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

import backend.api.algo.actions as _mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(slug: str = "test-agent") -> MagicMock:
    agent = MagicMock()
    agent.slug = slug
    agent.id = 1
    return agent


def _make_conns_stub_no_account() -> MagicMock:
    """Connections stub with an empty conn map — simulates cold registry."""
    c = MagicMock()
    c.conn = {}
    return c


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_close_broker_resolve_failure_does_not_raise_and_logs_correctly():
    """
    When get_broker raises KeyError (account not found / registry cold):
    1. _action_live_close_position must not propagate the KeyError.
    2. The logged diagnosis must contain "broker resolve failed", not the
       old silent sentinel "diagnosis unavailable".
    """
    account = "ACC001"
    symbol  = "NIFTY25JULFUT"
    exchange = "NFO"
    qty     = 50
    side    = "SELL"

    agent = _make_agent()

    # Preflight broker stub — needs enough margin to pass checks.
    broker_stub = MagicMock()
    broker_stub.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker_stub.instruments.return_value = []
    broker_stub.basket_order_margins.return_value = [{"initial": {"total": 10_000.0}}]
    broker_stub.margins.return_value = {
        "equity":    {"enabled": True, "net": 500_000.0},
        "commodity": {"enabled": True, "net": 500_000.0},
    }
    broker_stub.normalise_qty.side_effect = lambda exch, q, ls: int(q)

    logged_errors: list[str] = []
    mock_logger = MagicMock()
    mock_logger.warning.side_effect  = lambda msg, *a, **kw: None
    mock_logger.error.side_effect    = lambda msg, *a, **kw: logged_errors.append(msg)
    mock_logger.info.side_effect     = lambda msg, *a, **kw: None

    # Connections stub so preflight can build a broker for margin check.
    conns_stub = MagicMock()
    conns_stub.conn = {account: object()}

    def _raising_get_broker(acc: str):
        raise KeyError(f"account not found: {acc!r}")

    with (
        patch.object(_mod, "logger",       mock_logger),
        # get_broker is imported locally from backend.brokers inside the function;
        # patch the source so the local `from backend.brokers import get_broker` picks it up.
        patch("backend.brokers.get_broker", side_effect=_raising_get_broker),
        # run_preflight is defined in the same module; patch via module object.
        patch.object(_mod, "run_preflight",
                     new=AsyncMock(return_value={"ok": True, "blocked": []})),
        # _write_live_order is defined in the same module.
        patch.object(_mod, "_write_live_order", new=AsyncMock()),
        # chase_order is imported locally from backend.api.algo.chase.
        patch("backend.api.algo.chase.chase_order",
              new=AsyncMock(side_effect=RuntimeError("broker unavailable"))),
        # diagnose_live_failure is defined in the same module; should NOT be called
        # (broker is None), but stub it to catch any accidental invocation.
        patch.object(_mod, "diagnose_live_failure",
                     new=AsyncMock(return_value="should not be reached")),
        # alert helper — stub to avoid network/import side-effects.
        patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
              new=MagicMock()),
    ):
        params = {
            "account":  account,
            "symbol":   symbol,
            "exchange": exchange,
            "quantity": qty,
            "side":     side,
            "product":  "NRML",
        }

        # Must not raise — chase_order failure is re-raised, so we expect
        # RuntimeError (the chase failure), NOT NameError or KeyError.
        with pytest.raises(RuntimeError, match="broker unavailable"):
            await _mod._action_live_close_position(agent, {}, params)

    # At least one logged error line must surface the correct diagnosis string.
    assert logged_errors, "Expected at least one logger.error call"
    combined = " ".join(logged_errors)
    assert "broker resolve failed" in combined, (
        f"Expected 'broker resolve failed' in error log, got: {combined!r}"
    )
    assert "diagnosis unavailable" not in combined, (
        f"Old silent sentinel 'diagnosis unavailable' must not appear; got: {combined!r}"
    )
