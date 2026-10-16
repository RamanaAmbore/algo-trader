# Plan: Real orphan/paired-position display + persisted manual pin (Round 1)

## Context

Operator: the Legs grid's "O" orphan marker is currently decorative (always
orphan), wants a proper icon for orphan vs. paired state, and wants to be
able to select a position and manually assign a paired position to it —
"there is partially built functionality for it." Stated long-term goal:
"going forward, chase, bracket and paired positions are important."

Two read-only investigations + a 4-agent council (architect, risk, UX,
devil's advocate) ran against the actual code before this plan was written.
Key findings, all converging on the same shape:

- **Three independent, non-interacting "pairing" concepts already exist.**
  Only one is real and live: `backend/api/routes/positions.py:_auto_pair_positions()`
  (quantity-waterfall by root symbol, ephemeral — recomputed every request,
  no persisted identity) feeds MarketPulse's Positions bucket + PerformancePage
  correctly. A second (`_classify_mcx_group`/`_scope_positions_expiring_today_mcx_unhedged`
  in expiry.py/grammar.py) is a *different*, CE/PE-net algorithm used only
  for MCX expiry auto-close and disagrees in principle with the first. A
  third, `POST /api/orders/pair` + `OrderPairModal.svelte`, is fully built
  and admin-guarded but pairs ORDERS (not positions) via `AlgoOrder.parent_order_id`
  — a column chase.py already overloads to mean "this order already has a
  child, skip TP/template auto-attach." Calling the existing "Pair" button
  today silently disables TP/template protection on the child order, with
  zero operator-facing indication, and has no effect on any orphan/paired
  badge anywhere.
- **The specific bug the operator is looking at** is narrower than "the
  pairing logic has gaps": `buildPositionRowFromBroker()` in
  `frontend/src/lib/data/expiryPnl.js:243-288` strips `is_orphan`/
  `pair_group_key`/`has_gtt` via an explicit field whitelist before the
  derivatives Legs grid's pipeline ever sees them — so `CandidateLegRow.svelte`
  always falls through to the amber `'○'` glyph, regardless of real state.
  The sibling MarketPulse/PerformancePage grids already show the REAL data
  correctly via the same `_auto_pair_positions` computation.
- **Council consensus (architect, risk, UX, devil's advocate — all four
  independently converged on this):** do NOT retrofit `_auto_pair_positions`
  with new type-aware semantics in this round — keep it as the unchanged
  netting SSOT for MarketPulse/PerformancePage/Legs-grid display. Do NOT
  wire chase/bracket to consult any pairing concept yet — the two existing
  algorithms disagree and reconciling them is a separate, larger decision;
  risk's agent built a concrete scenario where acting on the wrong/stale
  pairing signal leaves a position naked with no stop (~₹40k unguarded
  loss example). Do NOT reuse `AlgoOrder.oco_pair_id` (reserved for a
  different future feature, zero writers today) or `parent_order_id`
  (already overloaded) for a new persisted manual-pin concept — it needs
  its own table. Do NOT leave `/api/orders/pair`'s silent TP/template
  side-effect undisclosed.

This round ships the real, safe, high-value slice every council member
agreed on: fix the Legs-grid display bug with the EXISTING correct data
(no new algorithm), add real icons, build a genuinely persisted
operator-pin feature with its own table and an unpair action (neither
exists today), surface the `/api/orders/pair` side effect instead of
hiding it, and reach the entry point from both pages regardless of
draft/live state. Chase/bracket pairing-awareness and algorithm
reconciliation are explicitly deferred to a future round — flagged below,
not silently dropped.

## History (git archaeology — operator asked "what happened to it,
## refer to previous commits")

The Legs-grid pairing display was **never accidentally removed** — the
operator's instinct that it used to work was correct, but the chip
markup itself has survived two refactors untouched; only the DATA
feeding it was ever missing for this one surface.

- `6f374a1a` (Aug 14) — original design: pairing was order-based
  (`AlgoOrder.parent_order_id`), with `is_orphan`/`pair_group_key`
  meaning "no open AlgoOrder tracks this position." Manual "Pair"
  button + modal added to both MarketPulse and the Legs grid.
- `382f3205` (Aug 15, next day) — **deliberate redesign**: replaced the
  order-based pairing with the quantity-waterfall algorithm in use
  today (`_auto_pair_positions`). This SAME commit wired real
  `pair_group_key`/`orphan_qty`/`paired_qty` chips directly into the
  Legs grid (`CandidateLegRow.svelte`) and a pair-group sort in
  `+page.svelte` — at this point the Legs grid display was genuinely
  wired and working.
- Sometime after that, the Legs grid's own data pipeline
  (`buildCandidatePositions` → `buildPositionRowFromBroker` in
  `expiryPnl.js`) was built with an explicit field whitelist that never
  included these fields — confirmed via `git log -S` (the field names
  never appear anywhere in that file's history). The chips kept
  rendering; the data feeding them silently went missing.
- `56712d19` (Sep 26, "Commit 9 completion") — an audit pass **found
  and explicitly documented** that the pair-group sort was dead
  ("`buildCandidatePositions` output rows never carry `pair_group_key`
  ... a guaranteed-stable no-op") and removed just the sort. Did not
  touch the chip markup.
- `897baee0` — consolidated the original two chips into the single
  state-column cell seen today (`'GTT'` / `pair_group_key` / `'○'`
  fallback) — why every row has shown `'○'` ever since.

**On "it should be present in positions too"**: it already is —
MarketPulse's Positions bucket (and PerformancePage) read the real
`_auto_pair_positions` output correctly today, confirmed by both
investigation agents independently. This plan's section 2 brings the
Legs grid up to the SAME standard those two surfaces already meet —
not a new capability, closing a gap unique to one page's pipeline.

## Approach

### 1. Backend — new persisted manual-pin table + API

- New table `position_pairs`: `id`, `account`, `symbol_a`, `symbol_b`,
  `created_by`, `created_at`, `active` (bool). Keyed on
  `(account, symbol_a, symbol_b)` with a uniqueness constraint on the
  unordered pair per account (store normalized so A/B order doesn't
  create duplicates). Migration in `backend/api/database.py`.
- `GET /api/positions` (both `_auto_pair_positions` call sites —
  `positions.py:591` and `:1317`) — after computing the existing
  quantity-waterfall `pair_group_key`/`is_orphan`/`paired_qty`/`orphan_qty`,
  overlay any ACTIVE `position_pairs` row matching the account+symbol: a
  pinned pair gets its own distinct `pin_group_key` field (new, separate
  from `pair_group_key` — never collide the two concepts under one name,
  per the architect's explicit guidance) so the frontend can render
  "operator-pinned" differently from "auto-detected."
- New routes: `POST /api/positions/pin` (`{account, symbol_a, symbol_b}`)
  and `DELETE /api/positions/pin/{id}` (unpair — does not exist today).
  Same admin-guard pattern as the existing `/api/orders/pair` route.
  400 if either symbol isn't a real open position for that account at
  call time (no pinning phantom/closed positions).
- `PositionRow` schema (`backend/api/schemas.py`): add `pin_group_key:
  Optional[str] = None`.

### 2. Frontend — fix the Legs-grid data-wiring bug (no new algorithm)

- `frontend/src/lib/data/expiryPnl.js:buildPositionRowFromBroker()` —
  add `is_orphan`, `pair_group_key`, `pin_group_key`, `has_gtt` to the
  field whitelist so they survive into `buildCandidatePositions()`'s
  spread (`frontend/src/lib/derivatives/pageLoad.js:312-414`). This is
  the entire fix for "the Legs grid always shows orphan" — reusing the
  exact same real, server-computed data MarketPulse/PerformancePage
  already render correctly, per architect's "keep #1 unchanged, don't
  invent new semantics silently" guidance.

### 3. Frontend — icon/glyph fix + shared helper (both grids)

- New shared helper (e.g. `frontend/src/lib/data/positionStateBadge.js`):
  `positionStateBadge(row) → {glyph, color, tooltip}` consuming `has_gtt`
  / `pin_group_key` / `pair_group_key` in that priority order. Replace
  the duplicated inline color/glyph logic in `CandidateLegRow.svelte:244-257`
  and `pulseColumns.js:572-593` with calls to this helper, and switch the
  hardcoded hex/rgba literals to the existing `app.css` tokens
  (`--algo-amber`, `--algo-cyan`, `--algo-green` + bg/border variants).
- Orphan glyph: replace `'○'` (reads as the letter "O") with a distinct,
  non-ambiguous glyph that has no color-emoji presentation variant on
  macOS/Safari (verify rendering; force text-presentation with U+FE0E if
  needed). Paired (auto-detected) keeps showing its group id (`P1`/`P2`)
  — per UX, the operator needs to see which rows are linked, a bare icon
  loses that. Pinned (operator-set) gets its own visually distinct
  treatment (e.g. a border/pin-mark variant of the paired badge) so it's
  never confused with an auto-detected pair that could silently reshuffle
  on the next poll.

### 4. Frontend — reachable pin/unpair UI on both pages

- Make the state badge itself clickable (mouse + touch) on both the
  derivatives Legs grid and MarketPulse's Positions bucket, opening a
  lightweight picker scoped to the SAME account's other open positions
  — not gated behind draft-row existence like today's `⟷ Pair` button
  (`+page.svelte:5459-5461`, currently inside the Drafts-only card header).
  Also add "Pair with…" to `SymbolContextMenu` where it's already wired
  (`+page.svelte:5863`/`6402`) as a second entry point.
  Calls the new `POST /api/positions/pin`.
- Unpair: a visible action on a pinned row's badge/context menu, calling
  `DELETE /api/positions/pin/{id}`.
- Retire the position-selection path inside `OrderPairModal.svelte`
  (checkbox-select-2-legs → this modal) since it never actually paired
  positions — replace its call sites with the new direct pin action.
  Leave `OrderPairModal`'s own order-to-order pairing form and
  `POST /api/orders/pair` endpoint itself untouched (see #5).

### 5. Surface (don't silently fix) the `/api/orders/pair` side effect

- `OrderPairModal.svelte` — add a visible inline warning when the modal
  is used for its existing order-pairing purpose: "Pairing these orders
  will suppress automatic take-profit / template attachment on the
  child order." No backend behavior change in this round — this is the
  minimal safe disclosure risk's and devil's agents both insisted on,
  short of the larger migration/deprecation decision (explicitly
  deferred, see below).

### 6. Docs

- `CLAUDE.md` — new entry documenting: the `position_pairs` table and
  its routes, the `pin_group_key` vs `pair_group_key` distinction (never
  collide the two names), the Legs-grid whitelist fix, and the explicit
  invariant that chase/bracket must NOT consult any pairing field yet.
- `docs/specs/PULSE_SPEC.md` — note the new `pin_group_key` field and
  badge semantics.

## Explicitly deferred (flag to operator, do not silently build or drop)

- **Chase/bracket consuming pairing state for execution decisions** (e.g.
  skip an individual stop because "the pair" is the real risk unit, or
  size a bracket off the pair's net delta). Blocked on reconciling —
  or an explicit operator decision not to reconcile — the two disagreeing
  automatic algorithms (`_auto_pair_positions` quantity-waterfall vs.
  `_classify_mcx_group` CE/PE-net). Candidate simpler path for a future
  round: chase/bracket could consult ONLY the new operator-pinned
  `position_pairs` table (unambiguous, operator-confirmed) rather than
  either automatic algorithm — but that's the operator's call.
- **Deprecating or changing `/api/orders/pair`'s actual behavior** (vs.
  just disclosing its side effect, #5 above) — it's live, admin-guarded,
  and something may already depend on its current behavior; auditing
  its real callers first is its own task.
- **Reconciling the two automatic pairing algorithms into one.**

## Critical files

- `backend/api/database.py` — new `position_pairs` table migration
- `backend/api/routes/positions.py` — `_auto_pair_positions` call sites,
  new pin/unpair routes
- `backend/api/schemas.py` — `PositionRow.pin_group_key`
- `frontend/src/lib/data/expiryPnl.js` — whitelist fix
- `frontend/src/lib/data/positionStateBadge.js` — new shared helper
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`,
  `+page.svelte` — badge rendering, pin entry point, SymbolContextMenu item
- `frontend/src/lib/data/pulseColumns.js`, `frontend/src/lib/MarketPulse.svelte`,
  `frontend/src/lib/PerformancePage.svelte` — badge rendering, pin entry point
- `frontend/src/lib/order/OrderPairModal.svelte` — side-effect warning,
  retire the position-checkbox path
- `CLAUDE.md`, `docs/specs/PULSE_SPEC.md`

## Agents

- backend: implement the `position_pairs` table/migration, overlay logic
  in both `_auto_pair_positions` call sites, `POST /api/positions/pin` +
  `DELETE /api/positions/pin/{id}` routes, `PositionRow.pin_group_key`
  schema field — per section 1 above.
- frontend: `expiryPnl.js` whitelist fix (section 2); new
  `positionStateBadge.js` helper + glyph/color consolidation in
  `CandidateLegRow.svelte`/`pulseColumns.js` (section 3); clickable
  badge + context-menu pin/unpair entry points on both pages, retire
  `OrderPairModal`'s position-checkbox path (section 4); side-effect
  warning in `OrderPairModal.svelte` (section 5).
- broker: skip
- doc: CLAUDE.md + PULSE_SPEC.md updates (section 6)
- backend-test: pytest coverage for the new table, overlay logic, pin/
  unpair routes — happy path, duplicate-pin rejection, phantom-symbol
  400, unpair clears `pin_group_key` on next fetch.
- playwright: badge glyph rendering (orphan/paired/pinned visually
  distinct), pin flow from both pages (Legs grid + MarketPulse), unpair
  flow, OrderPairModal warning text renders.

## Tests

- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message

fix(positions): wire real orphan/paired state into Legs grid, add persisted manual pin + unpair, disclose /api/orders/pair's TP-suppression side effect

## Done when

- Legs grid shows the real GTT/paired/orphan state for every row
  (matching MarketPulse/PerformancePage), not a hardcoded orphan glyph.
- Orphan/paired/pinned glyphs are visually distinct from each other and
  from a plain letter "O".
- Operator can select a position on either the Legs grid or MarketPulse
  Positions bucket and pin it to another open position in the same
  account; the pin persists across polls/reloads and is visually
  distinguishable from an auto-detected pair.
- Operator can unpair a pinned pair from either surface.
- `OrderPairModal` discloses its TP/template-suppression side effect
  before submit.
- Chase/bracket code is untouched — no new pairing-field reads anywhere
  in `chase.py`, `template_attach.py`, `bracket_registry.py`.
