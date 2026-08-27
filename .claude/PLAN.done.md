# Plan: Chain tab two-phase grid + NavStrip animation refinement

---

## Part A — Chain tab strike-grid hang (two-phase load)

### Root cause
The hang is in the **strike grid** (after expiry selection), not expiry fetch.

`_chain_quotes_batch_quote()` calls `broker.quote(~400 keys)` which routes:
`RemoteBroker → UDS → conn service → shared rate limiter (1/s, shared with ltp()) → Kite HTTP (7s timeout)`.

Combined latency: 3s rate limiter wait + 7s HTTP = 10s. Right at `asyncio.wait_for(timeout=10.0)`.
Any extra delay → `TimeoutError` → `quote_resp = {}` → all bid/ask null → frontend retries forever.

**Key insight**: The instruments cache has everything needed for the grid skeleton
(strike, CE/PE symbol, lot_size, exchange). `broker.quote()` is only needed for
live prices (LTP, bid, ask). The grid can render immediately from instruments; prices overlay separately.

**Secondary bug**: `option_quote_key()` returns `None` for unparseable symbols; no null check before
`keys.append(qk)` at options.py:2258 → sends `None` to Kite API → exception → empty grid.

### Fixes

**Fix A1 — Two-phase endpoint** (`backend/api/routes/options.py`)

Add `prices: bool = False` query param to `chain_quotes`.

- `prices=False` (default, fast path): skip `_chain_quotes_batch_quote()` entirely.
  Return rows built from sym_map only — bid=null, ask=null, ltp=null. Sub-100ms, no broker call.
- `prices=True` (slow path): call `_chain_quotes_batch_quote()` as today, return price data.

Change timeout: `10.0` → `30.0` in `_chain_quotes_batch_quote` (safety net, not primary fix).
Fix None key: add `if qk is None: continue` before `keys.append(qk)` at line 2258.

**Fix A2 — Two-phase frontend** (`frontend/src/lib/order/OptionChainTab.svelte`)

When an expiry is selected:
1. Fire `chain-quotes?und=X&expiry=Y` (no `prices=1`) → renders grid skeleton immediately (strikes visible, bid/ask show as `—`)
2. Fire `chain-quotes?und=X&expiry=Y&prices=1` in parallel → overlays bid/ask/ltp when ready
3. If prices call fails/times out: grid stays visible with `—` placeholders (no retry loop)
4. No loading spinner on the grid itself — only a subtle "loading prices…" indicator in the bid/ask columns while pending

---

## Part B — NavStrip animation refinement

### Current (distracting)
- **Heartbeat** (`.ps-heartbeat`): amber border-bottom brightens on every 30s poll — OK but slightly noisy
- **Tick shimmer** (`.cell-freshness-pulse::after`): sky→indigo gradient **sweeps left→right** via `transform: scaleX(0→1)` over 0.6s — this is the distracting one

### New design
- **Base**: amber `border-bottom` unchanged — `var(--algo-amber-border-soft)` always present
- **Tick animation**: replace the sweep with a **soft rainbow opacity fade** — no movement, just a very light iridescent glow that appears and dissolves on the 1px border
  - Multi-stop gradient: warm amber → green → cyan → indigo at **max 20% opacity** (`rgba(..., 0.20)`)
  - Keyframe: `opacity: 0 → 0.5 → 0` over 1.0s, ease-out — no `transform`, no spatial motion
  - Result: the border softly iridescences for ~1s then returns to plain amber — barely perceptible, confirms "data just arrived"
- **Heartbeat** (poll refresh): keep frequency and duration unchanged (every 30s, 300ms). Only reduce the visual delta — amber opacity change from 0.25→0.32 (was 0.25→0.40). Same timing, barely perceptible color shift.
- **Poll pulse** (closed hours): keep as-is (slate, 300ms)

### Files
- `frontend/src/app.css`: Replace `freshness-sweep` keyframe + `.cell-freshness-pulse::after` styles
- `frontend/src/lib/PositionStrip.svelte`: Reduce heartbeat amber delta + shorten to 200ms

---

## Agents
- backend: Fix A1 — In `options.py`, add `prices: bool = False` query param to `chain_quotes`. When `prices=False`, skip `_chain_quotes_batch_quote()` and build rows from sym_map only (bid=None, ask=None). When `prices=True`, call as today. Change `timeout=10.0` → `timeout=30.0` in `_chain_quotes_batch_quote`. Add `if qk is None: continue` guard at line 2258. Write pytest tests: (a) `prices=False` returns rows with null bid/ask and no broker call; (b) None key is excluded from quote call; (c) `prices=True` with mocked broker returns populated bid/ask.
- frontend: Fix A2 + Part B — In `OptionChainTab.svelte`, implement two-phase load: fire skeleton call first (render immediately), then prices call (overlay on completion). Show `—` in bid/ask until prices arrive. In `app.css`, replace `freshness-sweep` keyframe with `rainbow-fade` (opacity 0→0.5→0, no transform, 1.0s ease-out, rainbow gradient at ≤20% opacity). Update `.cell-freshness-pulse::after` to use new keyframe and gradient. In `PositionStrip.svelte`, reduce heartbeat visual only: amber opacity to 0.32 (was 0.40) — keep 300ms duration and 30s frequency unchanged.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(chain): two-phase grid load (instruments first, prices overlay) + subtle navstrip rainbow animation

## Done when
1. `chain-quotes?prices=false` returns rows without broker call (instant)
2. `chain-quotes?prices=true` returns bid/ask (30s timeout, None key guarded)
3. Frontend renders grid skeleton immediately on expiry select, overlays prices when ready
4. NavStrip tick animation is opacity-fade rainbow (no sweep), heartbeat reduced
5. pytest passes; svelte-check 0 errors
