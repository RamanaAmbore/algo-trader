# Plan: refactor(events): extract _dispatch_channel to reduce dispatch() CC from D→B

## Context
`dispatch()` in `backend/api/algo/events.py` is CC=21 (grade D) after the ntfy channel
was wired in. The CC gate in dprod blocks the merge to main. The fix is a pure structural
refactor: extract the per-channel if/elif chain into a `_dispatch_channel()` helper so
`dispatch()` is a thin loop (CC ≈ 4) and the channel logic sits in a focused helper (CC ≈ 8).
No behaviour changes — same channels, same guards, same order.

## Task
In `backend/api/algo/events.py` (lines 80–171):

1. Extract a new `async def _dispatch_channel(channel, agent, telegram_body, email_subject,
   email_body, condition_text, ist_display, eval_result, broadcast_fn, sim_mode, branch)`
   that contains exactly the current if/elif block (lines 131–165). The function has no
   return value and raises on error (caller wraps in try/except).

2. Replace the if/elif block inside `dispatch()` with a single call:
   `await _dispatch_channel(channel, agent, telegram_body, email_subject, email_body,
   condition_text, ist_display, eval_result, broadcast_fn, sim_mode, branch)`

3. No other changes — do NOT touch _send_telegram, _send_email_raw, _log_event, or any
   other function. Do NOT rename any variables. Do NOT reorder channels.

## Agents
- backend: Apply the refactor described above to
  `backend/api/algo/events.py`. Extract `_dispatch_channel` from the body of `dispatch()`.
  Place `_dispatch_channel` immediately after the closing brace of `dispatch()` (before
  `log_event`). Verify with `venv/bin/python -m radon cc backend/api/algo/events.py -s`
  that `dispatch` drops to grade B or better. Patch must be purely structural.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip (no new behaviour; existing agent dispatch tests cover this)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
refactor(events): extract _dispatch_channel to reduce dispatch() CC from D (21) to B

## Done when
`venv/bin/python -m radon cc backend/ -s -n D` produces no output (no D/E/F grades).
