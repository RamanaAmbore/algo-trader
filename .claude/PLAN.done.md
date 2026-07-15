# Plan: docs(CLAUDE.md): add ntfy/broker arch notes + post-plan /impl /depl prompt

## Context
Two architectural findings from this session need to survive context compression by
living in CLAUDE.md (always loaded). Also, the plan workflow should prompt the operator
with `/impl` or `/depl` options after plan approval, so they don't need to remember
to type the next command.

## Changes

### 1. CLAUDE.md — Key Patterns section (after "Singleton Connections")
Add a new pattern entry:

**RemoteBroker.translate_qty** — `RemoteBroker` (used when `RAMBOQ_USE_CONN_SERVICE=1`)
inherits a no-op `translate_qty` from the base class; it MUST override to forward to the
conn service so MCX/NCO contracts→lots translation happens correctly. Fixed 2026-07-15:
`backend/brokers/client/remote_broker.py` now delegates `translate_qty` via `self._call`.
Any new broker proxy layer must do the same — failing to do so causes raw contract qty
(e.g. 100) to hit the 50-lot adapter ceiling and be refused.

### 2. CLAUDE.md — Things to Avoid section
Add one bullet:

- Don't use `httpx` for outbound ntfy.sh calls from the prod server — server resolves
  ntfy.sh to IPv6 first (happy-eyeballs) and FCM push delivery silently fails. Use
  `urllib.request` which picks IPv4 (first in `getaddrinfo`). See `send_ntfy_alert()`
  in `backend/shared/helpers/alert_utils.py`.

### 3. CLAUDE.md — Default Workflow section (around line 69-80)
Add a line after the ExitPlanMode description to document the expected prompt:

After calling `ExitPlanMode`, always append to the response:
> Plan ready — run `/impl` to build only, or `/depl` to build + deploy to prod.

Update the Default Workflow block to include this, and update the Custom slash
commands table to add `/depl`.

### 4. CLAUDE.md — Custom slash commands section
Add missing `/depl` entry:
- **`/depl`** — Full pipeline: impl → ddev → dprod in one command (bypass-permissions)

## Agents
- doc: Update `CLAUDE.md` with all four changes above. Use exact text from this plan.
  Do NOT rewrite unrelated sections. Edit only the three sections identified:
  Key Patterns (add RemoteBroker.translate_qty entry after Singleton Connections),
  Things to Avoid (add ntfy/httpx bullet), Default Workflow (add post-ExitPlanMode note),
  Custom slash commands (add /depl line).
- backend: skip
- frontend: skip
- broker: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: no
- playwright: no

## Commit message
docs(CLAUDE.md): add ntfy IPv6 guard, broker translate_qty note, /depl command, post-plan prompt

## Done when
CLAUDE.md contains all four additions. No existing content removed.
