# Plan: fix chain-tab permanent spinner (AbortError not resetting _chainExpiriesLoading)

## Task
The option chain tab ("Fetching expiries…" spinner) hangs forever with no backend call visible in nginx logs. Root cause confirmed: when `chainUnderlying` changes and the Svelte 5 `$effect` cleanup fires `controller.abort()`, the in-flight fetch throws `AbortError`. The catch handler at line 295 of `OptionChainTab.svelte` did `if (err?.name === 'AbortError') return;` — returning WITHOUT resetting `_chainExpiriesLoading = false`. The spinner stays true forever and no subsequent attempt fires. Fix: reset `_chainExpiriesLoading = false` before returning in the AbortError branch, plus add a `controller.signal.aborted` guard at the top of `attempt()` as a defensive valve.

**Changes already applied** (edits made before ExitPlanMode):
- `frontend/src/lib/order/OptionChainTab.svelte` — two changes, total +7 lines

## Agents
- backend: skip
- frontend: skip (fix already applied)
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(chain): reset _chainExpiriesLoading on AbortError so spinner never hangs

## Done when
- svelte-check 0 errors
- Chain tab opens, expiries appear, no permanent spinner
