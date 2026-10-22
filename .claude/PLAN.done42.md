# Plan: Pre-open LTP pinning, daily draft sweep, 05:30 market-status alert, empty-state fix, unified context menu

Six independent items, all discussed and approved this session. Ship as **separate commits**
during `/impl` (per standing "one issue at a time" convention) even though they're planned
together — do not squash them into one commit.

---

## Item 1 — Pin LTP to prev_close until each exchange segment's real session opens

### Context
Live incident, confirmed with real data: at 08:47 IST (past the system's 08:00 "window open"
boundary but before NSE's 09:15 and MCX's 09:00 real trading-session starts), MCX GOLD option
legs already showed a live/reference `last_price` diverging from `prev_close` (e.g.
`GOLD26OCT142000PE`: last_price 439 vs prev_close 420), producing a real but premature Day P&L
move (-₹3,800 on that one leg; -₹7,050 summed across two accounts' GOLD books — the "-7k" the
operator saw on NavStrip's P badge, first slot).

Root cause: the system's 08:00-23:31 IST "window" (broker connectivity / ticker lifecycle) is
broader than each exchange SEGMENT's own real trading-session hours (`market_segments.equity`
09:15-15:30, `market_segments.commodity` 09:00-23:30, from `backend/config/backend_config.yaml`).
Once the broker reconnects at 08:00, live quote/LTP calls start flowing immediately and MCX
options already carry a reference price tied to international gold movement, well before the
local MCX order book technically opens — so `last_price` can legitimately differ from
`prev_close` during this gap even though no local trade has occurred.

Operator's explicit instruction: LTP and prev_close must read as the SAME value (so Day P&L
reads 0) until each row's own exchange segment has genuinely opened — not just until the broader
08:00 system window opens. Operator also corrected the MCX commodity segment's real market
trading time: 09:30 IST, not the 09:00 currently configured in `market_segments.commodity.sessions`
(`backend/config/backend_config.yaml`) — confirmed blanket for the whole commodity segment
(futures and options alike), not instrument-specific.

### Scope
- **Config change, broader blast radius than this item alone**: update
  `market_segments.commodity.sessions` start from `"09:00"` to `"09:30"` in
  `backend/config/backend_config.yaml` (and the matching `hours_start` legacy-shape key). This
  value is read by every existing MCX-open consumer, not just the new pinning logic added here —
  order placement gating (`_ticket_gate_market_hours_and_align_price`), basket per-leg gate,
  chase (`_chase_order_impl`), GTT/wing template-attach, `market_lifecycle.py`'s poll (drives
  `close_settled`/snapshot writes), `/market/status`, the navbar market badge
  (`marketHours.js:MCX_OPEN_MIN`, which needs the matching frontend constant updated too — see
  below), watchlist movers gate, and the simulator's market-hours block guard. Correcting it here
  fixes all of them consistently (true SSOT), but means this change is validated against MORE
  than Item 1's own pytest list — see the added test below.
- `frontend/src/lib/marketHours.js`'s `MCX_OPEN_MIN` constant (feeding `isMcxOpen()`, used by
  `segmentOpenForExchange()` and the Chain/OrderTicket market badges) must move in lockstep with
  the backend config — it's a deliberately duplicated value (per that file's own docstring, kept
  separate from OrderTicket.svelte's own `_mktOpenForExchange` for a Playwright regex-matching
  reason), so BOTH copies need the 09:00→09:30 edit or they'll disagree.
- Backend-only fix for the LTP-pinning logic itself, in the two existing `prev_close`-resolution
  functions (every frontend consumer inherits it for free, no separate frontend gating needed):
  `backend/api/routes/positions.py:_override_stale_close_from_snapshot` and
  `backend/api/routes/holdings.py:_override_stale_close_for_holdings`.
- For a row whose exchange segment is NOT yet genuinely open (reuse `_segment_is_open()` from
  `backend/shared/helpers/date_time_utils.py` — holiday/special-session aware, not a hand-rolled
  hour check — following the per-file exchange-matching precedent `quote.py:_qt_is_seg_open`
  already establishes, not the `agent_engine.py` `_EXCHANGE_TO_SEGMENT` table, which CLAUDE.md
  documents can drift from a given route file's own match rule):
  - Set `last_price = prev_close`.
  - Recompute `unrealised = (prev_close − avg_price) × qty`, `pnl = realised + unrealised`,
    `day_change = 0`, `day_change_val = 0`, `day_change_percentage = 0`.
  - Tag `price_source = 'pending_open'` (new, distinct from `'live'` / `'snapshot'` /
    `'snapshot_settled'`).
- Pins BOTH Day P&L and Lifetime P&L during the gap (operator asked for ltp == prev_close, not
  just a Day-P&L-specific suppression) — correct since no real local trade has happened yet.
- Does NOT touch the closed-hours (23:31→08:00 IST) `_positions_snapshot` path — already serves
  the frozen snapshot directly.
- Does NOT change `frontend/src/lib/data/nav.js:baseDayPnlForPosition` — it naturally computes
  ~0 once the backend row's `pnl` already equals `prev_settlement_pnl`.
- Grep all `price_source` consumers (frontend + backend) for an exhaustive switch that would
  silently mishandle the new `'pending_open'` value.

### Tests
pytest: position/holding on an unopened segment (mock "now" 08:30 IST) gets pinned values; same
row once its segment's real open time has passed gets genuine live values; a boundary case at
exactly session-open; holiday/special-session correctness preserved (via `_segment_is_open`, not
bypassed).

---

## Item 2 — Sweep unpromoted draft positions at the next market-open 08:00 IST cycle

### Context
Draft positions (`AlgoOrder` rows, `mode='draft'`, `status='OPEN'`) currently have no lifecycle —
nothing ever expires them. A draft's whole premise is "what if I enter *now*": its `avg_cost` is
frozen at creation-time LTP, so a day-old draft no longer answers that question. Operator approved
auto-clearing any draft that survives to the next 08:00 IST trading-day boundary after its
creation (skipping weekends/holidays — "on market open day").

### Scope
- New scheduled background task, alongside the existing `_task_holiday_refresh`/snapshot-reset
  jobs in `backend/api/background.py`, firing at 08:00 IST on a trading day (reuse the same
  trading-day/holiday-aware gating those jobs already use).
- Sweep: any `AlgoOrder` row with `mode='draft'` AND `status='OPEN'` whose `created_at` is before
  the CURRENT 08:00 IST boundary gets deleted (same deletion the manual Clear button already
  does — reuse `_draft_delete_by_id`'s underlying delete, not a separate code path).
- A draft created on the SAME trading day the sweep runs (i.e., after today's 08:00 boundary
  already passed) is NOT swept until the NEXT occurrence — only drafts that existed before the
  boundary that's currently firing.
- Per the earlier recommendation (operator didn't object): log + a brief toast-equivalent — since
  this is a backend-only sweep with no open frontend session necessarily listening, use the
  existing alert/notification path at `tier='info'` so an operator who has notifications on sees
  "N draft(s) cleared at market open" rather than a silent disappearance. Keep this lightweight —
  do not build new UI for it.

### Tests
pytest: a draft created before today's 08:00 boundary is swept when the job runs; a draft created
after today's boundary survives; sweep is a no-op on a non-trading day (weekend/holiday); sweep
never touches a non-draft `AlgoOrder` row regardless of status/age.

---

## Item 3 — 05:30 IST market-status alert agent (NSE + MCX open/closed + hours)

### Context
At `holiday_refresh_time: "05:30"` (`backend/config/backend_config.yaml`), the system already
determines today's holiday/open status for NSE and MCX (`_task_holiday_refresh`, feeding
`market_holidays` + MCX evening-closed-dates). Operator wants this determination reported via
alert: NSE/equity and MCX/commodity open-or-closed status, each segment's trading hours when
open, and the closure reason when closed (e.g., holiday name, weekend).

**Channel routing (operator's explicit instruction):** Telegram gets the alert on BOTH an open
day and a closed day (full detail either way). ntfy gets the alert ONLY when the market is open —
no ntfy push on a closed day. Default for the edge case where NSE and MCX disagree (one open, one
closed on the same day) — ntfy fires if **either** segment is open today (so the operator doesn't
miss a tradeable day); Telegram's message always covers both segments regardless. Flag this
default to the operator after building — easy to flip to "ntfy only when both open" if that's not
what they meant.

### Scope
- New built-in agent, `is_system=true`, slug e.g. `market-status-report`, `schedule='always'`,
  `fire_at_time='05:35'` (5-minute buffer after the 05:30 holi/mcx-evening refresh so this reads
  settled data, not a race against it — avoids the two independently-scheduled-at-05:30 problem),
  `tier='info'`, `conditions` = the same schedule-only sentinel pattern as `market-open-nse` /
  `market-preclose-mcx` (`avail_margin >= -999999999`), `actions=[{"type":
  "send_market_status"}]`.
- New action token `send_market_status` added to `backend/config/grammars/agent_grammar.yaml`
  (catalog metadata) — follow the `send_summary` entry added earlier this session as the
  template, including updating `agent_grammar_baseline.json` + its docstring amendment paragraph,
  per the established golden-baseline test workflow (`test_agent_grammar_yaml_catalog.py`).
- New handler in `backend/api/algo/actions_live.py` (e.g. `_action_live_send_market_status`),
  registered in `_LIVE_ACTION_HANDLERS` (same category as `send_summary`, not the noop table,
  since it performs a real side effect — sending alerts). Add its resolver to
  `ACTION_DISPATCH_CATALOG_ALLOWLIST` only if needed per `validate_action_dispatch_catalog`'s
  rules (mirror how `send_summary` was wired, fully resolved, not allowlist-only).
- Handler logic:
  1. Reads today's determined status for `equity` (NSE/NFO/BSE/CDS) and `commodity` (MCX)
     segments — open/closed, and if closed, the reason (holiday name from `market_holidays`, or
     "weekend"). Use the already-refreshed data (post-05:30 holiday refresh), not a fresh
     recomputation.
  2. Formats one message covering both segments, e.g.:
     `Market Status — 2026-10-22\nNSE/Equity: OPEN 09:15–15:30 IST\nMCX/Commodity: OPEN
     09:00–23:30 IST`
     or, closed: `NSE/Equity: CLOSED (Diwali)\nMCX/Commodity: CLOSED (Diwali)` — independently per
     segment (they can differ).
  3. Sends via `backend/shared/helpers/alert_utils.py:_send_telegram()` unconditionally.
  4. Sends via `alert_utils.py:send_ntfy_alert()` only when at least one segment is open today
     (priority `'low'`, matching tier=info convention — per the existing `_TIER_TO_NTFY_PRIORITY`
     mapping fixed earlier this session).
  5. Does NOT go through the generic `_dispatch_agent()`/`RICH_ALERT_AGENT` channel fan-out
     (that mechanism sends identically to every configured channel — this alert needs
     per-channel-conditional routing on content, which that mechanism doesn't support) — the
     action handler calls the alert_utils primitives directly.

### Tests
pytest: open day → both telegram and ntfy fire, message content correct for both segments;
closed day (both segments) → telegram fires, ntfy does NOT fire; mixed day (one open, one closed)
→ telegram fires with correct per-segment detail, ntfy fires (per the "either open" default
above); golden-baseline catalog test updated for the new `send_market_status` token.

---

## Item 4 — Fix "No candidates" empty-state positioning on the derivatives Legs grid

### Context
Operator screenshot: in fullscreen, the "No candidates" empty state appears to float off-center
— neither centered in the visible panel nor cleanly top-left. Root cause confirmed via CSS read:
`.cand-empty { grid-column: 1 / -1 }` sits inside `.cand-grid`, which is `width: max-content`
with 21 column tracks — most of them (IV/Δ/Γ/Θ/𝒱/EV, account, etc.) sit off-screen to the right
of the visible scroll viewport when there's no row data to inform a narrower natural width. The
inner `EmptyState` component (`frontend/src/lib/EmptyState.svelte`, `.es-rich`) centers itself
on that FULL 21-column grid width, which is wider than what's actually visible — so it renders
shifted right of the visible center. `align-self: start` (plus fullscreen's `align-content:
start` on `.cand-body-rows`) additionally pins it near the top rather than vertically centering
in the available fullscreen height.

### Scope
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`: render `.cand-empty` OUTSIDE the
  21-column grid-column flow — e.g. as a sticky overlay (`position: sticky; left: 0;` sized to
  the SCROLL VIEWPORT width, not the grid's full content width) so it centers on what the
  operator can actually see, not on off-screen columns.
- Center both axes within that visible viewport (replace `align-self: start` with proper
  vertical centering in the fullscreen 1fr-track case; keep a sensible top-anchored fallback for
  the non-fullscreen, short-panel case where true centering would look odd in a small card).
- Also flagged in the same screenshot: the "LEGS (1)" tab badge shows a count of 1 while the
  panel simultaneously says "No candidates for ABLBL on the current filter" — investigate whether
  the badge counts something other than currently-displayed candidates (e.g. total legs across
  all underlyings, or a draft/provisional count) and confirm that's intentional, not a second
  inconsistency.

### Tests
playwright: fullscreen Legs grid empty state renders within the visible scroll viewport bounds
(not shifted past what's on-screen) for a no-candidates underlying.

---

## Item 5 — Unify the right-click/long-press context menu across Pulse surfaces

### Context
Operator-requested audit (completed this session, read-only) found TWO separate context-menu
implementations instead of one:
- `SymbolContextMenu.svelte` (shared component, 5 items: Place order, Close (sell/buy) [only if
  `currentQty≠0`], Chart, Orders, Log) — used by OrderBook, LogPanel, PerformancePage,
  derivatives Legs grid (real rows), `/orders` page.
- MarketPulse.svelte's own separate, hand-rolled menu (up to 10 items: Chart→, Open in Options→,
  Place order→, Orders→, Log→, ★/Remove watchlist, Copy symbol, ↩/↗ group attach-detach, ↻ Reset
  all overrides) — used by ALL SIX MarketPulse buckets (Positions, Holdings, Pinned watchlist,
  Watchlist, Top Winners, Top Losers).

Notable gaps found: MarketPulse's own menu has **no Close action at all** (can't flatten a
position from its Positions-row right-click, unlike every other surface using the shared
component). ↩/↗ group attach-detach shows on Holdings/Watchlist rows where it's not relevant
(no underlying-root concept there). ↻ "Reset all overrides" is global, not row-scoped, oddly
placed in a per-row menu.

Symbol **click** (not right-click/long-press) was confirmed already consistent everywhere —
always opens the order ticket (or, mid-draft-selection, toggles selection) — no changes needed
there.

**Design decision taken** (operator didn't object when this tension was raised, so proceeding
with the recommended direction — flag for correction if this reads wrong): collapse to ONE
context menu implementation (the shared `SymbolContextMenu.svelte`, extended with a row-type-
aware item set) used everywhere, including MarketPulse's six buckets and the derivatives Legs
grid. This REVERSES the round-2 (`d361bf9c`) choice to divert long-press/right-click on DRAFT
rows to mean "select for clear" — instead, "Select for clear" and "Promote to real" become
draft-only items INSIDE the unified menu, and the gesture itself goes back to always opening the
menu, same as every other row type.

### Scope
- Extend `SymbolContextMenu.svelte`'s item set to be row-type-aware: accept a row-type prop
  (positions / holdings / watchlist-movers / draft) and show only the items relevant to that
  type, per the audit table above (Close only where a real position exists; group attach/detach
  only where an underlying root concept applies; ★/Remove watchlist only where relevant; Reset-
  all-overrides either dropped from the per-row menu entirely or moved to a card-level control,
  operator's call during implementation if it's ambiguous — default to dropping it from the
  per-row menu since it isn't row-scoped).
- Add "Select for clear" and "Promote to real" as draft-only items inside the unified menu.
- Replace MarketPulse.svelte's hand-rolled inline menu with `SymbolContextMenu.svelte` across all
  six buckets.
- Replace the round-2 draft-row long-press/right-click diversion (MarketPulse `_startDraftSelection`-
  on-contextmenu, derivatives Legs grid's `onContextMenu` draft branch) with: long-press/right-
  click always opens the unified menu; the menu's own "Select for clear" item is what enters
  selection mode. Keep the EXISTING touch-long-press-timer / `pointercancel` handling machinery
  (it's still needed to open the menu on mobile) — only the ACTION taken on long-press/right-
  click changes (open menu, not direct-select).
- Update `frontend/e2e/draft_positions_pulse.spec.js` / `draft_positions_derivatives_legs.spec.js`
  test C/C2/C3 accordingly (select-for-clear now goes through the menu, not direct gesture).

### Tests
playwright: per-surface menu item set matches the row-type table above (no Close on a Watchlist
row with no position, no group attach/detach on Holdings, draft rows show Select-for-clear and
Promote); existing draft select/clear/cancel flow still works end to end through the new path.

---

## Item 6 — Investigate derivatives Legs-grid promote-and-submit test (flag only, no fix yet)

### Context
`draft_positions_derivatives_legs.spec.js` test D (click a draft leg → promote → Submit → broker
call fires) failed in 3 of 4 real (non-skipped) runs this session, with market depth/bid-ask
showing all "—" in the failing runs. Could not confirm whether this is a round-2 regression or a
pre-open-hours/dev-environment depth-timing artifact — the leg itself only renders once
`getInstrument()` has already resolved (same symbol `executeDraft()` re-resolves), which argues
against the "cold instrument cache" theory, but no stronger evidence was found either way.

### Scope
This item is NOT a build task yet — re-run the spec (isolated, `-g "D:"`) during NSE regular
trading hours (09:15–15:30 IST) once Items 1–5 are shipped, when real market depth genuinely
exists. If it still fails with real depth present, that's a confirmed regression — file as its
own, separately-scoped fix (per "one issue at a time") rather than folding into this plan.

---

## Agents
- backend: Items 1, 2, 3.
- frontend: Items 1 (price_source grep), 4, 5.
- backend-test: pytest for Items 1, 2, 3.
- playwright: Items 4, 5.
- doc: one CLAUDE.md entry (Critical math guards) for Item 1's pending_open pinning, cross-
  referencing "Market daily window" / "Market open/closed determination"; a brief note under
  "Common Tasks" for Item 3's new agent + action; mention Item 2's sweep in the draft-positions
  section already present in CLAUDE.md; update the context-menu description if one exists.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message (one per item, see note at top)
1. `fix(positions): pin LTP to prev_close until each exchange segment's real session opens`
2. `feat(drafts): sweep unpromoted draft positions at the next market-open 08:00 IST cycle`
3. `feat(agents): add 05:30 IST market-status alert agent (NSE/MCX open-closed + hours)`
4. `fix(derivatives): center Legs grid empty-state within the visible scroll viewport`
5. `refactor(context-menu): unify right-click/long-press menu across Pulse surfaces; draft select/promote become menu items`

## Done when
- Item 1: a position/holding on a not-yet-open segment shows last_price==prev_close,
  day_change(_val/_percentage)==0, price_source=='pending_open'; reverts once that segment's real
  session starts.
- Item 2: a draft surviving to the next trading-day 08:00 IST boundary after its creation is
  auto-cleared; same-day-created drafts are untouched until their own next boundary.
- Item 3: 05:35 IST fires a market-status message to Telegram every day (open or closed), and to
  ntfy only when at least one segment is open.
- Item 4: empty state renders within the visible viewport on the fullscreen Legs grid.
- Item 5: one context menu implementation, row-type-aware, used everywhere; draft select/clear/
  promote reachable through it; MarketPulse's hand-rolled menu is gone.
- Item 6: noted as a follow-up, not built this round.
- Backend pytest green, svelte-check 0 errors, playwright green for Items 4/5.
