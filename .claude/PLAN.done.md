# Plan: Fix bg-sparkline-warm crash + NavStrip Exp P&L divergence + VS Code svelte error

## Context

Three bugs found:

1. **bg-sparkline-warm crash (P1)** — `NameError: name 'is_engine_idle' is not defined` crashes `_task_sparkline_warm` every 60s on prod. Logs confirm at `2026-09-14 02:30:00`. Lines 3990 and 4006 in `background.py` call `is_engine_idle()` without a local import (all other call sites at 1110, 4293, 6031 correctly use `from backend.shared.helpers.utils import is_engine_idle` inline).

2. **NavStrip Exp P&L diverges from snapshot (wrong value, not zero)** — NavStrip shows 37k; derivatives snapshot shows 2.79L. Root cause:
   - `_enrich_position_greeks` (positions.py line 762) is called inside the live broker path only. When market is closed (NSE holiday + MCX morning closed), positions route fast-returns the DB snapshot (positions.py ~1392) **before** enrichment runs → `underlying_ltp` is null on snapshot rows.
   - NavStrip `_resolveOptionSpot` (PositionStrip.svelte:716) checks `p.underlying_ltp` first — falls through to symbolStore when null. KiteTicker has no MCX spot subscribed directly (only option contracts, not the underlying futures) → symbolStore miss. Row-scan finds stale `last_price` from old position rows (may be yesterday's settlement, giving wrong spot) → wrong exp P&L (37k instead of 2.79L).
   - Derivatives snapshot avoids this via `_underlyingQuotes` (batchQuote REST, refreshed every 30s), which fetches underlying via `broker.quote()` even on closed hours — returns current last known price from Kite.
   - **During MCX evening open** (after 17:00 IST): live broker path runs, `_enrich_position_greeks` stamps `underlying_ltp` via `broker.quote()`. NavStrip and snapshot should align then. If still diverging, likely a contract resolution mismatch (NavStrip resolves nearest future, snapshot uses batchQuote with a different key) — verify post-fix.

3. **VS Code `import('svelte')` "module not found" error** — JSDoc type annotations `@type {import('svelte').Snippet}` in `.svelte` files cause TypeScript LS to show red squiggles. Root cause: VS Code is opened from repo root (`/Users/ramanambore/projects/ramboq`), TypeScript LS resolves modules from root `node_modules/`, but `svelte` is only in `frontend/node_modules/`. The Svelte VS Code extension has a TypeScript plugin that fixes this when enabled. Fix: add `"svelte.enable-ts-plugin": true` to `.vscode/settings.json`. The build and `svelte-check` are unaffected; this is a dev-experience-only issue.

**Holiday recognition**: System handled today correctly. NSE holiday was in the in-memory Tier-1 cache at 8:00 IST (populated by the 5:30 IST Tier-4 NSE-API fallback). MCX `evening_open_on_holidays: true` (backend_config.yaml line 106) correctly reopens MCX at 17:00 IST on holidays.

## Task

### Bug 3 (trivial, no agent needed) — VS Code svelte.enable-ts-plugin

**File**: `.vscode/settings.json`

Add one key:
```json
"svelte.enable-ts-plugin": true
```

This registers the Svelte extension's TypeScript Language Service Plugin, which resolves `import('svelte')` module references from `frontend/node_modules/` regardless of workspace root. No code change, no tests needed.

---

### Bug 1 — background.py: missing is_engine_idle import

**File**: `backend/api/background.py`

Around line 3988 (just before `if now >= midnight_dt_now and midnight_warm_date != today:`), add a local import:
```python
from backend.shared.helpers.utils import is_engine_idle
```
This single import covers both line 3990 and 4006 (both are inside the same outer loop).

### Bug 2 — positions.py: enrich snapshot rows with underlying_ltp

**File**: `backend/api/routes/positions.py`

The fast-return snapshot path at ~line 1388:
```python
if source not in ("live", "stale-live") and getattr(resp, "as_of", None):
    logger.debug(...)
    return resp   # ← enrichment never runs here
```

Change to:
```python
if source not in ("live", "stale-live") and getattr(resp, "as_of", None):
    logger.debug(...)
    await _asyncio.to_thread(_enrich_position_greeks, resp.rows)   # stamp underlying_ltp
    return resp
```

`_enrich_position_greeks` → `_batch_fetch_spots` → `broker.quote()` works during closed hours (Kite REST returns last known price). Single round-trip per positions request, acceptable latency.

**Skip-condition in `_enrich_position_greeks`** (line 1274): `r.last_price <= 0` — confirm snapshot rows have `last_price > 0` from the DB snapshot. They do (snapshot captures LTP at settlement time).

### Tests required

**Backend**: Add a test case to `backend/tests/test_positions_snapshot_prev_ltp.py` (or similar) that:
- Simulates closed-hours snapshot path (mocking `closed_hours_or_broker` to return snapshot + `source='snapshot'`)
- Verifies that returned rows have `underlying_ltp > 0` for option rows
- Patches `_batch_fetch_spots` to return a fixed spot

**Frontend** (Vitest): Add a test in `frontend/src/lib/__tests__/data/expiryPnl.test.js` (or PositionStrip-level) confirming:
- When `p.underlying_ltp = 0` (null), `_resolveOptionSpot` returns 0 and `expiryPnl` returns null (leg skipped) — documents the known failure mode
- When `p.underlying_ltp = 5100`, NavStrip correctly computes intrinsic

## Agents
- backend: Fix background.py (missing import) + positions.py (enrich snapshot rows). Files: `backend/api/background.py`, `backend/api/routes/positions.py`
- backend-test: Write test for positions snapshot enrichment. Target: `backend/tests/test_positions_snapshot_prev_ltp.py` or `test_positions_snapshot_fixes.py`
- frontend: Edit `.vscode/settings.json` — add `"svelte.enable-ts-plugin": true`
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(positions): stamp underlying_ltp on closed-hours snapshot rows; fix is_engine_idle NameError in sparkline-warm; enable svelte TS plugin for VS Code

## Done when
- `bg-sparkline-warm` no longer crashes on prod (NameError gone)
- `/api/positions` snapshot rows have `underlying_ltp > 0` for option rows
- NavStrip Exp P&L for CRUDEOIL options matches derivatives snapshot value when market is closed
- VS Code no longer shows `import('svelte')` module-not-found squiggle
- pytest green
