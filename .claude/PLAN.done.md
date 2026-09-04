# Plan: Terminate chase immediately on BrokerInputError

## Context

The chase loop's `except Exception` handler passes all broker exceptions through
`_ch_handle_attempt_error`, which increments `consecutive_errors` and sleeps
`interval_seconds` (20s) between retries. It only aborts after `_MAX_CHASE_ERRORS=3`
consecutive exceptions.

`BrokerInputError` (mapped from Kite's `InputException`) is raised *before* any broker
order is created — e.g. insufficient margin, invalid symbol, permission gap. These are
structural conditions that will not self-resolve. Retrying 3× wastes 40 seconds and
produces misleading "consecutive errors" log noise when the real cause is a funds
shortfall.

The correct treatment is the same as a poll-returned `"REJECTED"` status: terminate
immediately without retrying.

## Task

In `_ch_handle_attempt_error`, detect `BrokerInputError` at the top of the function
and return a terminal `ChaseResult` immediately — same FAILED state, alert, and
terminal event as the poll-rejected path, but with `reason="input_rejected"`.

## Agents

- backend: Modify `backend/api/algo/chase.py` as specified below
- backend-test: Add test to `backend/tests/test_chase_extended.py`
- frontend: skip
- broker: skip
- doc: skip
- playwright: skip

## Changes

### `backend/api/algo/chase.py`

**Import** (top-level, line ~27 after existing broker imports):
```python
from backend.brokers.errors import BrokerInputError as _BrokerInputError
```

**`_ch_handle_attempt_error`** — add early-exit block before `consecutive_errors += 1`:
```python
if isinstance(exc, _BrokerInputError):
    abort_msg = f"Order rejected by broker (input error): {exc}"
    logger.error(f"Chase {symbol}: BrokerInputError — terminating immediately. {exc}")
    result.status = ChaseStatus.FAILED
    result.detail = abort_msg
    emit("chase_failed", {"attempts": attempt, "error": str(exc), "reason": "input_rejected"})
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=account, symbol=symbol, exchange=cfg.exchange,
            side=transaction_type, qty=quantity, mode="live", source="chase",
            error=abort_msg,
        )
    except Exception:
        pass
    if current_order_id:
        import asyncio as _asyncio
        _asyncio.create_task(_emit_chase_terminal(
            current_order_id, "chase_failed",
            symbol, transaction_type, quantity,
            attempts=attempt, error=abort_msg, algo_order_id=algo_order_id,
        ))
    return result, consecutive_errors
```

This returns `(result, consecutive_errors)` — caller sees `abort is not None` and exits.
No sleep, no counter increment.

### `backend/tests/test_chase_extended.py`

Add one test: `test_broker_input_error_terminates_immediately` — patches
`place_order` to raise `BrokerInputError("insufficient funds")`, runs `chase_order`,
asserts:
- Result status is `FAILED`
- `result.detail` contains `"input_rejected"` or `"input error"`
- Total elapsed < 5s (no retries)
- `send_order_failure_alert` called exactly once

## Files touched

- `backend/api/algo/chase.py` — import + `_ch_handle_attempt_error` early-exit
- `backend/tests/test_chase_extended.py` — new test

## Tests

- pytest: yes (`backend/tests/test_chase_extended.py`)
- svelte-check: no
- playwright: no

## Commit message

fix(chase): terminate immediately on BrokerInputError instead of retrying 3×

## Done when

`pytest backend/tests/test_chase_extended.py -v` passes and the new test confirms
zero-sleep termination on `BrokerInputError`.
