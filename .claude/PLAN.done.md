# Plan: MCX pre-close alert — time + name + suppress margin condition

## Task
Three changes to `backend/api/algo/agent_engine.py`:

1. **Rename + reschedule**: "MCX market close" → "MCX pre-close", fires at 23:00 IST (30 min before actual MCX close at 23:30).
2. **No margin condition in alert body**: info agents (NSE open, MCX pre-close) currently show `funds.any_acct avail_margin=338,625 (>= -999999999)` in the alert — this is the always-true sentinel condition used to trigger on schedule, not a real condition. Fix: after `_v2_build_evalresult()` is called in `_v2_fire_if_matches()`, if the agent has `fire_at_time` set, override `result.condition_text` to `"Scheduled — {fire_at_time} IST"`. Applies to NSE open (09:15) too, which has the same problem.
3. **Update descriptions and long_name** to reflect the new name and time.

## Agents
- backend: In `backend/api/algo/agent_engine.py`:

  **Edit 1** — `_INFO_AGENTS` MCX entry (~line 1155):
  ```python
  # OLD:
  {
      "slug": "market-close-mcx",
      "name": "MCX market close",
      "long_name": "when:fire_at=23:30   alert:info/tg+ntfy(default)+log   do:notify-only",
      "description": "Fires once at MCX close (23:30 IST) on trading days.",
      "fire_at_time": "23:30",
      "conditions": {"op": ">=", "scope": "funds.any_acct", "metric": "avail_margin", "value": -999999999},
  },
  # NEW:
  {
      "slug": "market-preclose-mcx",
      "name": "MCX pre-close",
      "long_name": "when:fire_at=23:00   alert:info/tg+ntfy(default)+log   do:notify-only",
      "description": "Fires 30 minutes before MCX close (23:00 IST) on trading days.",
      "fire_at_time": "23:00",
      "conditions": {"op": ">=", "scope": "funds.any_acct", "metric": "avail_margin", "value": -999999999},
  },
  ```

  **Edit 2** — `_v2_fire_if_matches()`, after line 1612 (`result = _v2_build_evalresult(matches, agent.name)`):
  ```python
  # Add immediately after the _v2_build_evalresult call:
  if getattr(agent, 'fire_at_time', None):
      result.condition_text = f"Scheduled — {agent.fire_at_time} IST"
  ```
  This replaces the auto-generated margin dump with a clean "Scheduled — 09:15 IST" / "Scheduled — 23:00 IST" for all schedule-only agents.

- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add test to `backend/tests/test_alert_routing.py` (or a nearby alert test) verifying:
  1. `market-preclose-mcx` slug exists in BUILTIN_AGENTS with fire_at_time="23:00"
  2. `market-close-mcx` slug no longer exists
  3. A fire_at_time agent produces condition_text "Scheduled — HH:MM IST", not a margin dump
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(alerts): MCX pre-close at 23:00 + suppress margin condition for fire_at agents

## Done when
- BUILTIN_AGENTS has slug=market-preclose-mcx with fire_at_time="23:00"
- Alert body for MCX pre-close and NSE open shows "Scheduled — HH:MM IST" not funds values
- pytest green, broker cov ≥ 80%, api cov ≥ 45%
