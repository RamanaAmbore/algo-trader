# Plan: Suppress all agent alerts on dev

## Context
Agent alert dispatch in `backend/api/algo/events.py` gates `telegram`, `ntfy`, and `mail`
channels via `is_enabled()` checks (all False in `cap_in_dev`). However, the `log`,
`websocket`, and `inapp` channels in `_dispatch_channel()` fire unconditionally —
they are not gated. Loss agents running on dev therefore still emit log entries and
WebSocket/inapp notifications to any connected admin session. The fix adds one
`cap_in_dev` flag and a single early-return guard at the top of `dispatch()`.

## Task

1. **`backend/config/backend_config.yaml`** — add one flag to `cap_in_dev`:
   ```yaml
   agent_alerts:  False  # all agent alert dispatch (dev: off)
   ```
   No change to `cap_in_prod` (missing key defaults to True).

2. **`backend/api/algo/events.py`** — add an early-return at the top of the
   `dispatch()` function (line 80):
   ```python
   async def dispatch(agent, eval_result, broadcast_fn=None, sim_mode: bool = False):
       if not is_enabled('agent_alerts'):
           return
       # ... existing body unchanged
   ```
   `is_enabled` is already imported on line 15 of events.py — no new import needed.

## Agents
- backend: Apply both changes:
  (a) In `backend/config/backend_config.yaml`, add `agent_alerts: False` to the
      `cap_in_dev` block (after the existing `ntfy: False` line).
  (b) In `backend/api/algo/events.py`, insert `if not is_enabled('agent_alerts'): return`
      as the first line of the `dispatch()` function body (after the docstring if any,
      before any other logic).
  For every file you change or create, you MUST write or update at least one test covering
  the changed behaviour. This is mandatory — not optional.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add tests to `backend/tests/test_cap_flags_dev.py` (already exists — extend it):
  - Mock `is_enabled` to return False for 'agent_alerts'
  - Call `events.dispatch(mock_agent, mock_result, broadcast_fn=mock_broadcast)`
  - Assert the function returns immediately (broadcast_fn NOT called, no channels fired)
  - Mock `is_enabled` to return True for 'agent_alerts'
  - Assert dispatch proceeds normally (broadcast_fn IS called)
  For every file you change or create, you MUST write or update at least one test covering
  the changed behaviour. This is mandatory — not optional.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(dev): suppress all agent alert dispatch on dev via agent_alerts cap flag

## Done when
- `cap_in_dev.agent_alerts: False` is in backend_config.yaml
- `events.dispatch()` returns immediately when `is_enabled('agent_alerts')` is False
- No log entries, no WebSocket pushes, no ntfy/telegram from loss agents on dev
- prod behaviour unchanged
- pytest green
