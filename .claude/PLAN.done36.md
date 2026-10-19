# Plan: GOLD prev_close race + positions fallback + flash correctness across all surfaces

## Context

Started from a GOLD/Pulse/Legs diagnosis session and expanded, at the operator's
explicit request, into a full flash-on-tick audit across every surface in the app. Two
originally-flagged items turned out NOT to need a code change (recorded for the
record, not re-litigated here): Legs was never missing flash (it already had it,
predating this session), and the Payoff anchor-tier live-tick mechanism has no
structural NSE/MCX asymmetry — if GOLD's overlay lagged, it was almost certainly a
symptom of the already-fixed-and-deployed dead-ticker-poller bug (confirmed live:
712 real SSE ticks captured in a 12s window post-deploy), not a payoff-specific defect.

A comprehensive flash audit (this round) found the real remaining scope: two confirmed
correctness bugs in the `close`/`prev_close` data layer, a font bug, and **eight
confirmed flash defects** across Pulse, Derivatives Legs/Snapshot, OptionsPayoff, and
PerformancePage — plus one genuine spec-vs-code conflict requiring a judgment call
(resolved below, proceeding without waiting for approval per operator instruction).

**Judgment call, documented**: `PULSE_SPEC.md §16` (commit `8e1a27bb`, 2026-09-18
10:50) says Pulse's Chg% must never flash — only LTP. The code flashes it anyway, from
an earlier same-day commit (`3f909237`, 03:31) that the later spec commit appears to
have superseded without the code being updated to match. Going with **the spec**:
remove Pulse's Chg% flash entirely. Rationale: the spec commit landed chronologically
after the conflicting code and is this project's documented behavioral contract; and
Chg% is poll-only data on Pulse regardless, so flashing it per-tick is inherently
misleading no matter which direction it points.

## Approach

### Data-layer correctness (frontend)

1. **GOLD `prev_close` source race** — `_publishPositionsRows`
   (`marketDataStores.svelte.js` ~line 110, correct `daily_book.ltp` basis) vs
   `publishPulseQuotes` (~line 228, banned Kite `ohlc.close` basis) race to set a
   symbol's `close` in `symbolStore`, both stamping `snapshot_ts: Date.now()` with no
   source-priority. Fix: `publishPulseQuotes` (called from `MarketPulse.svelte:2933`,
   which has the current positions list in scope) excludes `close` from its field batch
   for any symbol that is a currently-held position with a valid `prev_close` — the
   position publisher stays sole authority for `close` on held-position symbols.
2. **Positions missing the holdings-style `prev_close` fallback** — mirror
   `mergeHoldingRows`'s fallback (`pulseUnified.js:574-586`/`587-596`) into
   `mergePositionRows`'s existing fallback block (~lines 493-500): when `row.close` is
   still null, fall back to `Number(r.prev_close)` if `> 0`.

### Flash correctness — must-fix (8 items, confirmed by audit)

3. **Pulse Chg% flash** (`pulseColumns.js:643-652`) — remove entirely, per the judgment
   call above. LTP flash (`pulseColumns.js:289-295`) is unaffected and correct.
4. **Legs Chg% flash watches the wrong metric** (`+page.svelte:1260`/`:2069`) — rekey to
   `leg:${k}:ltp` (the same key the cell's own `_chgPct` tracks, mirroring how Snapshot
   already correctly reuses its `:ltp` key for Chg% at `+page.svelte:6222`, since Chg%
   moves in lockstep with LTP against a fixed `prev_close`). Remove the separate
   `positionsDerivedStore`-fed `:chg` key entirely rather than fixing it in place.
5. **Closed (qty=0) Legs rows flash while their price is frozen** — the tick loop
   (`+page.svelte:2063-2071`) has no qty check; `CandidateLegRow.svelte:104-109` already
   freezes the displayed price for qty=0. Add the same qty=0 guard to the tick loop so
   it skips `flash.update(...)` for closed legs.
6. **Poll and live writes collide on the same flash key** (`+page.svelte:1207` vs
   `2010/2054` for Snapshot's `${und}:ltp`; `1259` vs `2068` for Legs' `leg:...:ltp`) —
   a poll landing with a slightly-stale value after a live tick can register as a
   reverse flash. Fix: only the live-tick write path should call `flash.update(...)` for
   these keys; the poll path may still update the underlying displayed data but must
   stop feeding the flash tracker.
7. **OptionsPayoff Chg% chip flashes the wrong direction**
   (`OptionsPayoff.svelte:952`) — picks its flash class from the sign of `spotPct`
   rather than the actual tick direction. Fix to use the real move direction, matching
   how every other correct flash site works.
8. **PerformancePage LTP (and Avg) cells flash on every tick despite poll-only values**
   (`PerformancePage.svelte:610/741`, `501-504`) — flashing on an event that doesn't
   change the displayed value is misleading. Fix: gate the flash on whether the
   displayed value actually changed, not merely on tick arrival.
9. **`ltpFlashPct` admin threshold is dead on Pulse** (`MarketPulse.svelte:1686/2341`,
   `_pctThreshold` assigned but never read; the tick handler ignores `pct`) — wire it in
   so Pulse respects the same threshold Derivatives already does, instead of flashing
   every symbol on every tick.
10. **`tickFlash.svelte.js:51`'s percentage-threshold guard only applies to positive
    previous values** (`if (_pctThreshold > 0 && last > 0)`) — negative values skip the
    threshold and flash on every change. Fix to compare `Math.abs(last)`. This affects
    Legs' Chg% (post-fix-#4) and PositionStrip's P&L cells when negative.

### Should-fix, bundled in (same subsystem, low marginal cost since these files are
already being touched for the must-fix items above)

- Legs underlying-anchor row has the `tf-cell` class but no `classOf` wired at all
  (`+page.svelte:5905-5906`) — confirm its LTP value matches Snapshot's
  `${selectedUnderlying}:ltp` source before reusing that key; wire it in if so.
- Pinned/Watchlist's dead `_mpFlash.update(...)` calls for `change_pct`
  (`MarketPulse.svelte:2187-2191`, `2200-2204`) — remove; nothing reads `_mpFlash.classOf`
  so this is a no-op already, cleanup only, touching a file already in scope.
- `PULSE_SPEC.md` drift: lines ~1060-1064/1076 describe a cascade-flash mechanism that
  no longer exists; lines ~1401-1402 omit the Legs/Payoff Chg% behavior. Correct these
  while updating §16 to reflect the Chg%-removal decision.

### Explicitly deferred (lower-visibility, not reported by operator, avoid further scope
creep on an already-large change)

SymbolPanel tab LTP and OrderTicket underlying spot/Chg% having no flash at all
("no documented decision" gaps per the audit) — left for a future pass. Dead
`_dashFlash`/`pnlClsFlash`/unused flash-key cleanup in dashboard/+page.svelte and
PerformancePage.svelte — left as-is, noted in memory for a future cleanup pass, not
blocking this change.

**Gainers/Losers (Market Movers) — root cause confirmed, fix deferred, different class
of problem.** The flash mechanism itself is NOT broken — movers share the exact same
`mkLtpCol`/`tickBus`/repaint code as Pinned/Watchlist, structurally intact. The real gap
is upstream: mover symbols are never subscribed to the live KiteTicker WebSocket feed at
all (only explicit watchlist adds get `subscribe_with_sym` per
`backend/api/routes/watchlist.py:1839`) — LTP instead comes from a 30s-TTL-cached REST
quote polled every 5s client-side, so the flash can fire at most once per 30s per
symbol, easily perceived as "never" even though nothing is disabled. Fixing this
properly means subscribing mover-list symbols to the live ticker feed, a materially
larger backend architectural change (mover lists churn frequently — could mean
subscribing/unsubscribing many symbols often) than a flash-code fix, and out of scope
for this batch. %Chg has the same documented by-design no-flash omission as
Pinned/Watchlist (consistent, not a defect). Recorded in the punchlist memory as its own
follow-up item, not implemented in this pass.

## Critical files

- `frontend/src/lib/data/marketDataStores.svelte.js` (items 1)
- `frontend/src/lib/data/pulseUnified.js` (item 2)
- `frontend/src/lib/data/pulseColumns.js` (item 3, part of 9)
- `frontend/src/lib/MarketPulse.svelte` (items 3, 9, should-fix pinned/watchlist cleanup)
- `frontend/src/lib/data/tickFlash.svelte.js` (item 10)
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (items 4, 5, 6, should-fix
  anchor row)
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` (item 4, 5 — read
  only to confirm the qty=0 freeze pattern being mirrored)
- `frontend/src/lib/OptionsPayoff.svelte` (item 7)
- `frontend/src/lib/PerformancePage.svelte` (item 8)
- `docs/specs/PULSE_SPEC.md` (should-fix spec drift + §16 Chg%-removal documentation)
- Gainers/Losers grid file(s) — TBD from the pending agent's report.

## Tests

- Vitest: GOLD positions-vs-quote publisher race (held position's `close` stays
  `daily_book.ltp`-sourced regardless of write order); `mergePositionRows`
  fallback-to-`prev_close`.
- Vitest/Playwright: Pulse Chg% cell never gets a `tf-up`/`tf-down` class (spec
  compliance); Legs Chg% flash fires in step with the `_chgPct` display value, not the
  old day-P&L metric; closed Legs rows never flash; a poll landing after a live tick
  doesn't reverse-flash; OptionsPayoff Chg% chip flashes the correct direction on a
  down-tick during an up day; PerformancePage LTP doesn't flash when the displayed
  value is unchanged; `ltpFlashPct` threshold actually suppresses sub-threshold Pulse
  flashes; a negative-value tick respects the same threshold as a positive one.
- Legs anchor/TOTAL row font-size assertion (computed style).

## Verification

1. `cd frontend && npx vitest run` and `npx svelte-check --output machine` clean.
2. `cd backend && venv/bin/pytest backend/tests/ -q --tb=line` clean (data-layer changes
   are frontend-only, but run the full gate per standing workflow).
3. Live devtools spot-check on a held GOLD position and a short position: confirm
   Chg%/P.Close never shows a Kite-ohlc.close-looking value, confirm no empty cell,
   confirm no wrong-direction flash on a short.

## Commit message

`fix(pulse,legs,payoff,perf): remove Pulse Chg% flash per spec, fix 8 flash-direction/metric-mismatch defects, fix GOLD prev_close race + positions fallback`

## Done when

All data-layer and flash items above are implemented, tested green, and deployed
through the full pipeline (impl -> ddev -> dprod) without pausing for approval, per
operator instruction. Deferred items and the Movers findings are folded in as they
land; the memory punchlist file is updated to reflect final status.
