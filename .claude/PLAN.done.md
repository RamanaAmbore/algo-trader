# Plan: Fix "0 Instead of Last-Known-Good" Data Bug (NavStrip + Payoff Chart) and Payoff Chart Flash/Desync

## Context

Operator reported, across several messages, one connected bug cluster:
1. Payoff chart flashes/redraws the whole chart on every refresh instead of showing a small progress indicator over just the LTP/CHG% values.
2. The Payoff chart's overlay values (LTP, CHG%, Exp P&L) can disagree with the curve itself.
3. When ticks/data can't refresh, the Payoff chart draws a flat **0** line and NavStrip positions show **0** — real money displayed as zero. Wanted behavior: **never show 0 as a stand-in for missing data** — freeze and keep showing the **last known good values**, with proper color-coding to signal staleness.

Two read-only audits (this session) traced this precisely:
- **NavStrip audit**: a genuine root cause. The broker conn-service turns a positions-fetch **failure** into an HTTP 200 with `accounts: []`. Every layer above that — the sync client, the route, the snapshot-gate cache, the frontend store, `portfolioStore`, `PositionStrip`, and `PerformancePage.loadAll` — treats that empty-but-"successful" response as genuine fresh data (a real empty book) rather than a degraded read, so it overwrites the last-known-good value (in memory AND in localStorage) with 0. **This is the same root data source the Payoff chart's book-poller reads from** — confirmed shared root cause for the "0 line" symptom.
- **Payoff chart audit**: the flash and the desync are mostly separate mechanisms (not both caused by the 0-data bug): a chart-wide "pulse" animation firing on every routine 5s refetch, the x/y axis re-centering on every refetch, a 4Hz-rebuilding stub replacing the real curve during any brief `strategy`-null window, several overlay props (Exp P&L, DTE, σ, spot) reading stale or differently-clocked data than the curve itself, and — for NSE underlyings specifically — the overlay only updating once per 5s refetch instead of ticking live like the rest of the page.

This plan fixes both, in order: the shared 0-vs-last-known-good root cause first (affects real money display on the highest-traffic surface, NavStrip), then the Payoff-chart-specific flash/desync mechanisms.

**Operator decision**: the Payoff chart's Exp P&L number (shown next to the expiry marker) will be priced at the **anchor-contract spot** (matching what the marker itself points at), not the front-month spot — so the number and the dart always visually agree. The separate Legs-grid Exp P&L total is unaffected (stays front-month).

## Part A — Root cause: failed fetch → HTTP 200 empty → 0 overwrites last-known-good

**A1 (broker, CONFIRMED).** `backend/brokers/service/routes.py:269-281` — the `/internal/positions` handler (and the equivalent `/holdings`, `/margins` handlers) catches every exception and returns `InternalPerAccountResp(accounts=[], errors=[...])` with HTTP 200. `backend/brokers/client/sync.py:42-61` never reads `payload.errors` and treats `accounts=[]` as a real empty result, so no `fetch_failed` marker is ever set for this failure path.
**Fix**: `sync.py`'s per-account fetch should surface the `fetch_failed` sentinel (the same one already used for direct-path broker exceptions) whenever `payload.errors` is non-empty or `accounts` comes back empty while accounts are actually configured for that call.

**A2 (backend, CONFIRMED).** `backend/api/routes/positions.py:858` only treats the response as an outage when **all** per-account results carry `fetch_failed` — an empty list (`per_acct == []`, A1's failure mode) trivially satisfies neither "all failed" nor "not all failed" correctly and falls through to `PositionsResponse(rows=[])` at ~line 869-870, treated as a genuine empty book. Separately, `snapshot_gate.py`'s `_stash_live_response("positions", data)` and the TTL/SSOT caches will happily stash this empty payload as the new "last-good," poisoning the cache for the whole TTL window.
**Fix**: treat `per_acct == []` (with accounts configured) as an outage, matching the existing all-failed path. Never stash or TTL-cache an empty `rows` payload as last-good — only stash genuine non-empty or genuinely-confirmed-empty (e.g. post-08:00-rollover with 0 real positions) results.

**A3 (frontend data layer, CONFIRMED — 4 sub-fixes, keep them together since they interact).**
- `frontend/src/lib/data/marketDataStores.svelte.js:283-293` — `positionsStore`/`pulsePositionsStore` don't set `keepStaleOnEmpty` (movers/activeLists/sparklines already do — reuse that same mechanism, don't invent a new one). A **blanket** `keepStaleOnEmpty` would be wrong on its own, though — a genuinely empty book (operator closed everything, or the 08:00 daily rollover) is legitimate and must still be able to show 0. The real fix is for the frontend to keep last-good only when the **backend explicitly tags the response as degraded** (via A1/A2's fix exposing `source`/`stale_accounts` — these fields already exist on `PositionsResponse`, `backend/api/schemas.py:256-261`, but are currently dropped by the frontend's `parse` step, `marketDataStores.svelte.js:288-291`, which keeps only `r?.rows`). Fix `parse` to retain `{rows, source, as_of, stale_accounts}` together.
- `frontend/src/lib/data/portfolioStore.svelte.js:399,490-501` — two of the three P slots (`_livePositionsPnl` at 490-501, and the `_portfolio` exp-pnl path at 399) have no stale-while-revalidate guard for an empty-but-non-null array; only a `null` `_posAgg` triggers the existing fallback-to-last path, but `[]` produces a non-null `_posAgg` with `exp_pnl: 0`, so the fallback never engages. Extend the fallback condition to also trigger when the response is tagged degraded (per the `source`/`stale_accounts` plumbing above), not only when it's literally `null`.
- `frontend/src/lib/PositionStrip.svelte:35-38,475-480` — filters out only `null`, so `positions = []` passes through, then the existing "prevent 0-flash" guard at 475-480 (`else if (positions.length === 0) dispPositionsToday = 0`) ironically forces the exact 0-flash it was meant to prevent, for a degraded-not-really-empty response. Gate this branch on "confirmed empty" (no degradation tag) vs. "degraded" (keep last value).
- `frontend/src/lib/PerformancePage.svelte:1146-1152` — `loadAll` does `_p_rows = p?.rows ?? []` then unconditionally `positionsStore.set(_p_rows)`, even when only the positions promise rejected (holdings/funds succeeded) — this bypasses every store guard and persists `[]` straight to localStorage, corrupting the shared singleton for every other route in the SPA until the next successful poll. Same bug applies to the holdings/funds `.set()` calls in the same function. **Fix**: only call `.set()` for slices whose promise actually fulfilled; leave a rejected slice's store untouched.

**A4 (frontend, CONFIRMED, should-fix, same pass).**
- **Stale indicator currently dead for this failure mode** — `PositionStrip.svelte:121-122`'s `_staleFailCount` only increments inside `_load()`, which (per an earlier fix, "Fix 4") now only runs on mount/`bookChanged`/mode-transition, not on a timer — so continuous failures seen by the background book-poller never increment it, and the counter only looks at `.error` anyway (an empty-200 "success" never trips it). Wire the staleness signal from the new `source`/`stale_accounts`/`as_of` tags (A3) instead of `_staleFailCount`.
- **Partial-account failure (R1, CONFIRMED)** — an account without circuit-breaker opt-in never gets last-known-good substitution (`_is_circuit_open` false → `_stale_substitute_frame` never reached, `backend/brokers/broker_apis.py:842-844`); if a SECOND account succeeds (even with a real empty book), `positions.py:858`'s "not all failed" check passes and the failing account's rows silently vanish from an otherwise-`'live'` 200. Fix: substitute last-known-good on ANY per-account failure, not only when the breaker is open; at minimum put `fetch_failed` accounts into `stale_accounts` so A3's frontend fix can react to it.
- **`softInvalidate()`/`invalidate()` set `.value = null`** (`dataStore.svelte.js:196-214`), and every `portfolioAggregates` getter returns 0 on null with no stale-while-revalidate guard (R3) — affects lifetime P&L, cash, margin, holdings value until the next poll. Apply the same stale-while-revalidate treatment here as A3's positions fix.

**A5 (visual — reuse existing conventions, no new pattern needed).** The audit confirmed this app already has a full staleness vocabulary: `.ps-strip.ps-stale` (amber strip tint, `PositionStrip.svelte:795-798`), `.ag-row.row-account-stale` (slate desaturation + diagonal hatch, `app.css:819-831`), the `STALE@HH:MM` badge (slate, `pulseColumns.js:408-420`), and `StaleBanner.svelte` (amber "showing last-good" vs. red "unavailable"). **Fix**: drive these from the new `source`/`stale_accounts`/`as_of` tags (A3/A4) instead of introducing anything new — apply the same desaturated/STALE@HH:MM treatment to the P values in NavStrip and (per A6 below) the Payoff chart overlay when showing frozen data.

## Part B — Payoff chart: stop the full-chart flash

**B1 (CONFIRMED).** `OptionsPayoff.svelte:419-421,860` — a `$effect` fires `_pulse.notify('payoff')` (a cyan background flash on the whole SVG stack, `.payoff-svg-stack`) whenever the `payoff` prop's array identity changes, which now happens on **every** routine 5s refetch (since a recent commit made the derivatives page refetch every ~5s even with unchanged legs) — not just on a genuine leg/strategy change.
**Fix**: only notify the pulse when the leg *signature* changes, not on every routine refetch identity-change; move the "something is refreshing" cue to a small spinner in the LTP/CHG% rows of `.payoff-stats` instead (the `rbq-spin` keyframe already exists, `app.css:2887` — this doubles as the operator's originally-requested "progress wheel over LTP/CHG%" feature).

**B2 (CONFIRMED).** The x/y axis re-centers on every refetch — `payoff`'s grid is recomputed centered on `strategy.spot` at fetch time (`backend/api/algo/derivatives.py`, several `np.linspace` call sites) and `yDomain` also rescales on a second, unsynchronized 5s clock (the positions-poll-driven `chartPnlOffset`, `+page.svelte:2336`) — so σ-tick labels and the visible plot range visibly jump independent of any real change.
**Fix**: pin the x-domain across refetches while the leg signature is unchanged, re-centering only once spot drifts outside the middle ~60% of the current range; add hysteresis to `yDomain` (grow immediately, shrink only past a threshold) instead of rescaling every cycle.

**B3 (CONFIRMED).** Whenever `strategy` is null or `_strategyStale` is true, `payoff` falls back to `_clientPayoffStub` (`+page.svelte:2571-2634`), which rebuilds a new array on every 250ms tick — so the pulse fires continuously and the real (amber "today") curve disappears for the whole fetch duration, not "one render frame" as a stale comment claims.
**Fix**: stale-while-revalidate — keep the last good merged payoff for the current root instead of swapping to the stub during a routine refetch; only show the stub/placeholder for a genuine cold start (no data ever rendered for this root yet).

**B4 (CONFIRMED, must fix alongside B1-B3).** A superseded fetch's `finally` block clears `loading` even for a stale generation (`+page.svelte:4033,4055`) — any new "refreshing" flag added for B1's spinner must be generation-guarded, or it inherits this same early-clear bug.

## Part C — Payoff chart: overlay/curve desync

**C1 (CONFIRMED — recurrence of commit b1b946a8's bug class, missed consumer).** The Exp P&L number is evaluated at `liveSpot` (front-month, `+page.svelte:2161-2162`) while the LTP row, spot line, CHG%, and the expiry marker/dart all use `payoffSpot` (anchor-contract basis, per b1b946a8's fix). **Fix (operator-approved)**: evaluate the Exp P&L value shown ON THE CHART at `payoffSpot` (anchor basis) instead of `liveSpot`, so it always agrees with where the dart is drawn. Leave the separate Legs-grid Exp P&L total on `liveSpot`/front-month — that's a different, correctly-scoped consumer.

**C2 (CONFIRMED).** For NSE underlyings, `payoffSpot` has no anchor-contract tick available (`backend/api/routes/options_helpers.py:111` always returns a null anchor for NSE) and falls back to `strategy.spot`, which only changes once per 5s refetch — so the overlay LTP/CHG% visibly "steps" every 5s instead of ticking live like the rest of the page (this is very likely what the operator means by "overlay not in sync").
**Fix**: in `payoffSpot`'s resolution (`+page.svelte:1881-1898`), when the anchor is null and the spot source is the NSE ticker path, use the live tick for that resolved NSE symbol (`liveSpot`'s own Tier-1 lookup already has this) before falling back to `strategy.spot`.

**C3 (CONFIRMED — same stale-root-leak class b1b946a8 partially fixed).** Only `payoff` and `intermediateCurves` are gated on `_strategyStale`; `breakevens`, `spanSigmas`, `spanPct`, `dte`, `ivProxy`, `legCount`, `legSymbols`, and `spotAnchor` all keep reading the OLD `strategy` during the stale window after switching underlyings, while `spot`/`prevClose` have already switched to the new root — so briefly after a symbol switch, the overlay can show the new symbol's LTP next to the old symbol's DTE/σ/legs.
**Fix**: one derived `payoffStrategy = _strategyStale ? null : strategy`, used for every strategy-derived prop (not just the two currently gated), so the whole overlay switches atomically — combine with B3's stale-while-revalidate approach (keep last-good PER ROOT, not a blanket null).

**C4 (CONFIRMED, lower priority — visual only).** The P&L/Exp-P&L readout is read at the nearest payoff-grid point rather than linearly interpolated at the exact spot x-position, so it "steps" in increments and the marker dart can visibly float off the drawn curve line on steep sections (masked for NSE today since `payoffSpot` currently equals a grid point exactly, per C2 — will become visible once C2 lands).
**Fix**: linearly interpolate between the two bracketing grid points (same interpolation the line-drawing already effectively does), applied consistently to both the chart's own readout and `chartTheoreticalAtSpot` (`+page.svelte:2298`).

**C5 (CONFIRMED, lower priority — visual only).** The displayed P&L combines the book-poll's `candidatesActualPnl` (priced at whatever spot the poll landed on) with `curve(payoffSpot) − curve(strategy.spot)` (a second, unsynchronized clock) — when the two 5s timers don't align, the spot move gets double-counted for 0-5s then snaps back, a visible sawtooth (mainly affects MCX anchors with live ticks; NSE is naturally immune since `payoffSpot` already equals `strategy.spot` there).
**Fix**: anchor the offset calculation to the spot the book poll was actually priced at (e.g. store `(pnl, spot_at_poll)` together from the poll response) so both terms share one clock instead of two.

## Explicitly out of scope

- Payoff chart's `_stickyXTicks` dead-code/stale-zoom hazard (audit "Risks/cleanup" section) — real but low-severity, not tied to either reported symptom; leave for a future pass.
- R2 (SUSPECT — `snapshot-fallback` mid-session after 120s of failures could show yesterday's settlement as if live, with no staleness marker) — needs live verification the session can't perform; A5's staleness-tag plumbing should incidentally cover this once `as_of` is properly threaded through, but no dedicated fix is scoped here beyond that.
- Format/alignment consistency work on the order ticket — separate, already-approved, already in-flight plan; no file overlap with this plan (confirmed: this plan never touches `OrderTicket.svelte`/`SymbolPanel.svelte`/`SideToggle.svelte`/`QtyInput.svelte`/`OrderKnobsRow.svelte`/`OrderDepth.svelte`/`Select.svelte`).

## Files

- `backend/brokers/service/routes.py`, `backend/brokers/client/sync.py` — A1.
- `backend/api/routes/positions.py`, `backend/api/helpers/snapshot_gate.py`, `backend/brokers/broker_apis.py` — A2, A4 (R1 substitution).
- `frontend/src/lib/data/marketDataStores.svelte.js`, `frontend/src/lib/data/dataStore.svelte.js`, `frontend/src/lib/data/portfolioStore.svelte.js`, `frontend/src/lib/PositionStrip.svelte`, `frontend/src/lib/PerformancePage.svelte` — A3, A4, A5 (NavStrip data-layer + staleness visuals). Does NOT touch `admin/derivatives/+page.svelte` or `OptionsPayoff.svelte`.
- `frontend/src/lib/OptionsPayoff.svelte`, `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, `backend/api/algo/derivatives.py` — B1-B4, C1-C5, plus the book-poller propagation fallback fix (the Payoff-chart half of A3's shared root cause — `+page.svelte:3743-3778`'s effect needs the same last-good fallback `loadPositions()` already has at `:3811-3813`). Does NOT touch any NavStrip data-layer file.

## Agents

- **broker**: A1 (`backend/brokers/service/routes.py`, `backend/brokers/client/sync.py`).
- **backend**: A2, A4's R1 substitution (`backend/api/routes/positions.py`, `backend/api/helpers/snapshot_gate.py`, `backend/brokers/broker_apis.py`) — dispatched parallel to broker agent, no file overlap.
- **frontend** (agent 1 — NavStrip data layer): A3, A4, A5 (`marketDataStores.svelte.js`, `dataStore.svelte.js`, `portfolioStore.svelte.js`, `PositionStrip.svelte`, `PerformancePage.svelte`).
- **frontend** (agent 2 — Payoff chart): B1-B4, C1-C5, plus the derivatives-page book-poller fallback (`OptionsPayoff.svelte`, `admin/derivatives/+page.svelte`) — dispatched parallel to frontend agent 1, no file overlap (confirmed above).
- **backend-test**: pytest coverage for A1/A2/A4, with the exact repro from the audit (conn-service positions fetch raises → client must NOT silently return `[]`; per-account failure with one healthy sibling account must NOT drop the failing account's rows from a `'live'`-tagged response).
- **doc**: sync CLAUDE.md — this is exactly the kind of cross-cutting invariant CLAUDE.md already has a home for ("Market-close snapshot" is currently only in memory per the audit, not in CLAUDE.md itself — worth promoting it there now, generalized from "market-close" to "any degraded/failed fetch," since this plan is the second time this exact bug class has been found).

## Tests

- pytest: yes — `venv/bin/pytest backend/tests/ -q --tb=line`.
- svelte-check: yes.
- vitest: yes — store-level tests for the stale-while-revalidate guards (A3/A4).
- playwright: yes — a spec that mocks a positions-fetch failure (empty-200 shape) and asserts NavStrip keeps showing the last non-zero value with a stale badge instead of 0; a spec for the Payoff chart confirming no full-chart pulse fires on a routine refetch and the overlay doesn't visibly step for an NSE underlying.

## Commit message

fix(data): never display 0 in place of missing/degraded data on NavStrip or
the Payoff chart — freeze to last-known-good with staleness indicators;
Payoff chart stops full-chart flash on routine refresh and fixes overlay/
curve desync (shared root cause: conn-service swallows fetch failures into
a fake-fresh empty 200)

## Done when

A1-A5 and B1-C5 each have a passing regression test reproducing the original
failure mode; `venv/bin/pytest`, `npx svelte-check`, `npx vitest run` all
green; self-audit confirms the "freeze to last-known-good + staleness tag"
fix reaches every consumer of the shared `positionsStore` singleton (grep
every `.set()`/`.value =` call site on it, not just the ones named above),
matching this session's standing rule for any shared-data-surface fix.
