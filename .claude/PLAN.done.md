# Plan: Fix MCX liveSpot cadence/post-close + loss alert baseline bug

## Context

**Bug 3 — Critical: loss alert never fires (session_start missing)**
`loss-positions-total` and `loss-positions-acct` have `pnl_rate_abs`/`pnl_rate_pct` in their `any:` conditions.
`_cycle_baseline_not_ready` in `agent_engine.py` blocks the entire agent when ANY rate metric is present
AND `_v2_baseline_live()` returns False. `_v2_baseline_live()` checks `alert_state['session_start']`,
but `session_start` is **never set anywhere** — it was tracked by the retired check-and-alert engine
and the writer was lost when v2 grammar replaced it. Result: `_cycle_baseline_not_ready` always returns
True for these agents → they are never evaluated → trigger_count = 0.
Confirmed: today's loss reached -₹12,64,175 with zero alerts fired.

Fix A: In `_update_pnl_history` (agent_engine.py), set `session_start = now` when new day (reset) or cold start.
Fix B: Add `_v2_all_rate_metric()` — like `_v2_has_rate_metric` but requires ALL leaves to be rate metrics.
  Change `_cycle_baseline_not_ready` to use `_v2_all_rate_metric` so mixed-condition agents (pnl + pnl_rate_abs)
  are NOT blocked during the baseline window; only pure-rate agents (loss-rate-acct) are gated.

---

Three compounding bugs in the derivatives payoff liveSpot for MCX positions (CRUDEOIL, GOLDM):

**Bug 1 — Post-close wrong value (regression, confirmed by operator)**
After MCX close, liveSpot Tier 2 reads `getSnapshot("CRUDEOIL26SEPFUT")?.ltp` from symbolStore.
During the session, SSE ticks set `ltp_ts = Date.now() > 0` for that key. After close, no more SSE ticks.
`batchQuote` REST polls use `ltp_ts: 0` (BH1 rule), so ltp_ts arbitration blocks them from overwriting the stale intraday value (9908) even though mmap + positions API correctly show settlement (9815).
Fix: gate Tier 2 on `isMarketOpen()` so post-close skips it and falls to Tier 3 (`_postCloseUndLtp` from positions API).

**Bug 2 — Cadence: virtual root aliases fail silently**
`_perf_subscribe_book_symbols` calls `_ticker.get_token_for_sym` and `_ticker.set_virtual_root_alias`
to remap `_token_to_sym[tok] = "CRUDEOIL"` (root) so `_poll_loop` emits `{sym: "CRUDEOIL"}` ticks.
Neither method exists on `MmapTickReader` → `AttributeError` → caught by the outer `try/except`.
Result: SSE emits `{sym: "CRUDEOIL26SEPFUT"}` not `{sym: "CRUDEOIL"}` → symbolStore has no "CRUDEOIL" key
→ Tier 1b misses → Tier 2 is the bottleneck (needs `instrumentsReady` + correct contract resolved).

Additionally, `_add_mcx_spot_anchors` Pass 1 only scans MCX options (CE/PE) to find roots.
For pure-futures positions (no MCX options), `mcx_roots` is empty → no aliases computed → even
with the MmapTickReader methods fixed, `set_virtual_root_alias` is never called for CRUDEOIL.
Fix: Pass 2 scans MCX futures already in `book_pairs` and aliases them to their root.

## Agents

- broker: In `backend/brokers/mmap_ticker.py`, add two methods to `MmapTickReader` after `subscribe_with_sym` (around line 214):

```python
def get_token_for_sym(self, sym: str) -> int | None:
    """Return the token for a tradingsymbol, or None if not registered."""
    return self._sym_to_token.get(str(sym or "").upper())

def set_virtual_root_alias(self, tok: int, root: str) -> None:
    """Override _token_to_sym so _poll_loop emits root sym (e.g. "CRUDEOIL")
    instead of the actual futures sym. _sym_to_token retains the original
    tradingsymbol→token mapping so has_sym() still works; root lookup also works."""
    if root:
        self._token_to_sym[tok] = root
        self._sym_to_token[root.upper()] = tok
```

- backend: Two changes in `backend/api/background.py`:

  1. In `_add_mcx_spot_anchors`, add Pass 2 after the existing options loop (before the `except`):
  ```python
  # Pass 2: alias MCX futures already in book_pairs regardless of options.
  # Covers pure-futures positions (no CE/PE) so set_virtual_root_alias is
  # always called for held MCX contracts — resolveUnderlying Tier 1b works
  # even when MCX instruments haven't loaded in the frontend yet.
  _re_fut_inner = _re_module.compile(r'^([A-Z]+)\d+[A-Z]+FUT$')
  for _sym, _exch in list(book_pairs):
      if _exch == 'MCX':
          _m = _re_fut_inner.match(str(_sym).upper())
          if _m:
              _root = _m.group(1)
              if str(_sym).upper() not in aliases:
                  aliases[str(_sym).upper()] = _root
  ```
  Also update the docstring: "Subscribe MCX spot anchors for MCX options AND futures."

  2. No changes needed in `_perf_subscribe_book_symbols` — it already calls `_ticker.get_token_for_sym` and `_ticker.set_virtual_root_alias` at lines 877–879; those will now resolve correctly.

- frontend: Two changes across derivatives and MarketPulse:

  **1. Derivatives `+page.svelte` — gate Tier 2 on `isMarketOpen()` (line 1770):**
  Change `if (_resolvedTs) {` → `if (_resolvedTs && isMarketOpen()) {`
  Prevents stale symbolStore entry from showing wrong post-close settlement price in liveSpot.

  **2. OptionsPayoff.svelte — add flash to SPOT and CHG% stat rows:**
  - Add `import { createTickFlash } from '$lib/data/tickFlash.svelte.js'` at top of script
  - Create instance: `const _spotFlash = createTickFlash({ threshold: 0, durationMs: 300 })`
  - Add `$effect` to drive flash: `$effect(() => { _spotFlash.update('spot', spot); })`
  - SPOT value span (line 743): add background flash class → `class={'ps-v ' + ltpDayClass(spotPct) + ' ' + _spotFlash.classOf('spot')}`
  - CHG% value span (line 748): add text-color flash → import `_tcFlashClass` from `pulseColumns.js`; apply `_tcFlashClass(spotPct >= 0 ? 'up' : 'down', Math.abs(spotPct ?? 0))` when `_spotFlash.classOf('spot')` is non-empty

  **3. Snapshot card (`.byund-row`) in `+page.svelte` — add CHG% flash:**
  CHG% span (line 4785) has only `ltpDayClass` static color. Reuse the LTP flash key — add `flash.classOf(\`${g.underlying}:ltp\`)` to the span class (same SSE tick drives both LTP and CHG%).
  Day P&L, P&L, Exp P&L, Extrinsic: no flash — these recompute on every 5s broker refresh or every 250ms spot tick; flashing them would produce constant noise across all rows. Directional color (`cell-pos`/`cell-neg`) is sufficient.

  **4. `CandidateLegRow.svelte` — add CHG% / LTP flash only:**
  LTP already has `flash.classOf(\`${_legFlashKey}:ltp\`)`. No changes needed to Day P&L, P&L, Exp P&L, Extrinsic — same rationale as snapshot (derived recomputations, not market events).

  **5. MarketPulse.svelte — LTP and CHG% only:**
  LTP already has `_ltpFlashUp`/`_ltpFlashDown` directional flash. `change_pct` column already has `_tcFlashClass` via `changePctCellClass`. No additional flash needed — P&L columns intentionally excluded (5s poll cadence would produce constant cell-level noise).

- backend-agent-engine: Two changes in `backend/api/algo/agent_engine.py`:

  1. In `_update_pnl_history` (around line 72, after the session_date reset block), add `session_start`:
  ```python
  if today and last_date != today:
      alert_state['pnl_history'] = {}
      alert_state['session_date'] = today
      alert_state['session_start'] = now   # ← ADD: reset per-day baseline anchor
  if 'session_start' not in alert_state:
      alert_state['session_start'] = now   # ← ADD: set on cold start / process restart
  ```

  2. Add `_v2_all_rate_metric()` after `_v2_has_rate_metric` (around line 288), then update `_cycle_baseline_not_ready` to use it:
  ```python
  def _v2_all_rate_metric(cond) -> bool:
      """True when ALL leaf metrics require a rate baseline (contain _rate_).
      Used by baseline gate — blocks only pure-rate agents, not mixed ones."""
      if not isinstance(cond, dict):
          return False
      for key in ('all', 'any'):
          if key in cond:
              children = cond.get(key) or []
              return bool(children) and all(_v2_all_rate_metric(c) for c in children)
      if 'not' in cond:
          return _v2_all_rate_metric(cond['not'])
      m = cond.get('metric', '') or ''
      return '_rate_' in m

  # In _cycle_baseline_not_ready: change _v2_has_rate_metric → _v2_all_rate_metric
  def _cycle_baseline_not_ready(agent, alert_state: dict, now, cfg: dict, *,
                                bypass_schedule: bool) -> bool:
      """True when a PURE rate-metric agent should be suppressed during baseline window."""
      return (
          not bypass_schedule
          and _v2_all_rate_metric(agent.conditions)   # ← was _v2_has_rate_metric
          and not _v2_baseline_live(alert_state, now, cfg['baseline_offset_min'])
      )
  ```
  Note: `_v2_has_rate_metric` in `_v2_should_suppress` stays unchanged (correct semantics there).

- doc: skip
- backend-test: Append to `backend/tests/broker/test_mmap_sym_registration.py`:
  - `test_get_token_for_sym_returns_none_when_unregistered` — fresh reader, call `get_token_for_sym("CRUDEOIL26SEPFUT")`, assert None
  - `test_get_token_for_sym_returns_token_after_subscribe` — `subscribe_with_sym([(144870151, "CRUDEOIL26SEPFUT")])`, then `get_token_for_sym("CRUDEOIL26SEPFUT")` == 144870151
  - `test_set_virtual_root_alias_overrides_poll_sym` — subscribe (144870151, "CRUDEOIL26SEPFUT"), then `set_virtual_root_alias(144870151, "CRUDEOIL")`. Assert `_token_to_sym[144870151] == "CRUDEOIL"` and `has_sym("CRUDEOIL")` is True and `has_sym("CRUDEOIL26SEPFUT")` is True.

  Append to `backend/tests/test_background_spot_anchors.py`:
  - `test_add_mcx_spot_anchors_aliases_pure_futures` — book_pairs has `("CRUDEOIL26OCTFUT", "MCX")` (no CE/PE). Call `_add_mcx_spot_anchors`. Assert aliases contains `{"CRUDEOIL26OCTFUT": "CRUDEOIL"}`.

  Append to `backend/tests/test_agent_engine_baseline.py` (new file):
  - `test_session_start_set_on_first_call` — call `_update_pnl_history({}, now, None, None)`, assert `alert_state['session_start'] == now`
  - `test_session_start_reset_on_new_day` — alert_state with yesterday's session_start + session_date, call with today's `now`. Assert session_start updated to now, pnl_history wiped.
  - `test_session_start_preserved_same_day` — call twice same day, assert session_start unchanged after second call.
  - `test_v2_all_rate_metric_pure_rate` — `{"any": [{"metric": "pnl_rate_abs"}, {"metric": "pnl_rate_pct"}]}` → True
  - `test_v2_all_rate_metric_mixed` — `{"any": [{"metric": "pnl"}, {"metric": "pnl_rate_abs"}]}` → False
  - `test_cycle_baseline_not_ready_mixed_agent_never_blocks` — mixed-condition agent, session_start not set → returns False (not blocked)
  - `test_cycle_baseline_not_ready_pure_rate_blocks_without_start` — pure-rate agent, session_start not set → returns True (blocked)

- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(alerts,derivatives): restore loss alert baseline; gate liveSpot Tier 2 on isMarketOpen; add MmapTickReader virtual-root methods

## Done when

- `loss-positions-total` / `loss-positions-acct` fire immediately when pnl ≤ threshold (no longer blocked by missing session_start)
- `loss-rate-acct` (pure rate metrics) still respects 15-min baseline window after session start
- After MCX close: liveSpot shows settlement price (from positions API) not stale intraday symbolStore value
- During live MCX hours: SSE emits `{sym: "CRUDEOIL"}` ticks → symbolStore["CRUDEOIL"] has ltp_ts>0 → Tier 1b resolves at 250ms cadence
- `get_token_for_sym` and `set_virtual_root_alias` exist on MmapTickReader and work correctly
- `_add_mcx_spot_anchors` returns alias for CRUDEOIL26OCTFUT even when no MCX CE/PE in positions
- pytest green, svelte-check 0 errors
