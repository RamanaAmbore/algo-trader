# Plan: Per-account Positions rows + CE/PE-aware pairing

## Context

Two related operator asks:

**A.** MarketPulse's Positions grid currently clubs the same symbol held across
MULTIPLE accounts into ONE aggregated row (`mergePositionRows`,
`pulseUnified.js`). Operator wants it to "mimic legs" — repeat per account,
like the Derivatives Legs grid and `PerformancePage.svelte` already do
(confirmed: both already key by `account|tradingsymbol`, no aggregation).

**B.** Pairing identification (`_auto_pair_positions`, `positions.py`, mirrored
in `lotPairSplit.js`) groups positions by `(account, root_symbol)` — stripping
CE/PE entirely — then waterfall-matches purely by opposite sign. Confirmed bug:
this can match an unrelated short PE against a long CE of the same
root/account/quantity and call it a "pair". Operator-confirmed rule (verbatim):
*"short pe and long pe, short ce and long ce are pairs"* — i.e. pairing must
also require the SAME option type; a CE leg must never pair with a PE leg.
Explicitly **not** in scope: same-side CE+PE (a short straddle/strangle, e.g.
short CE + short PE) is a different, legitimate strategy shape that today's
sign-only waterfall never caught and this fix does not add — out of scope,
raise separately if wanted.

**Why A and B are linked**: `mergePositionRows` nulls `is_orphan`/
`pair_group_key`/`pin_group_key`/`has_gtt` the instant 2+ accounts merge into
one row (ambiguity guard) — so today, Part B's badges (including the fix
below) are invisible in MarketPulse for any multi-account symbol holder. Part
A makes them visible there for the first time.

6-agent council ran (2 APPROVE, 4 CONCERN, 0 BLOCK → proceed with changes
below folded in):
- **Risk (CONCERN, critical)**: Part A breaks the Positions TOTAL row's Exp
  P&L — `MarketPulse.svelte`'s `_accumTotalsRow` sums `_filteredExpPnlByKey[sym]`
  (keyed by bare symbol, already cross-account) ONCE PER ROW. Today safe
  because there's one row per symbol; after Part A, N account-rows for one
  symbol would each add the full cross-account value, inflating the TOTAL by
  N×. Zero existing test coverage on this path. **Must fix as part of Part A.**
  Part B confirmed fully display-only/P&L-safe (only mutates `is_orphan`/
  `pair_group_key`/`paired_qty`/`orphan_qty`; `build_summary_from_rows`/
  `build_symbol_summary_from_rows` run on pre-merge rows, unaffected).
- **UX (CONCERN)**: losing the "total exposure to SYM across all my accounts
  at a glance" read is a real regression, softened by `sortUnifiedRows`
  already grouping same-symbol rows adjacent. Recommends surfacing the
  backend's existing-but-unused `PositionsResponse.symbol_summary` as a
  subtotal. Also: badges appearing/reclassifying post-deploy isn't new risk,
  just newly visible — worth noting, not alarming (no code requirement).
- **Devil (CONCERN)**: confirm `option_type` is derived via tradingsymbol
  suffix regex (same mechanism as `_root_symbol`), not an instruments-cache
  field that can be `None` for an expired/unparseable contract (two `None`s
  would silently re-collide). Frontend's `decomposeSymbol(sym).optType` is
  already regex-derived off the raw string, not a cache lookup — safe as-is.
  Flag explicitly: existing `mergePositionRows` tests assert "two accounts,
  same symbol → one merged row" — these need REWRITING, not just extending.
- **Architect (APPROVE)**: both changes stay in their respective layers, no
  new coupling. Suggests a parity test between `_auto_pair_positions` (Python)
  and `splitPairedLots` (JS) since this is the second time they've drifted.
- **Perf (APPROVE)**: row-count multiplication is small at realistic
  account-count scale, poll-driven not tick-driven, negligible.
- **Research (CONCERN, informational)**: Zerodha Kite/Console explicitly does
  NOT aggregate positions cross-account either — direct precedent for Part A.
  CE-with-CE/PE-with-PE-only is the standard options-taxonomy rule for
  vertical/covered pairing (confirms Part B's direction). Confirms straddles/
  strangles are a different, unaddressed strategy shape (matches our scope note).

## Changes

### Part B — CE/PE-aware pairing (do first; backend is low-risk, pure function)

- `backend/api/routes/positions.py`: add `_option_type(tradingsymbol: str) -> str | None`
  next to `_root_symbol()` — same suffix-regex mechanism (`CE$`/`PE$`), not an
  instruments-cache lookup. Change `_auto_pair_positions()`'s group key from
  `(r.account, _root_symbol(r.tradingsymbol))` to
  `(r.account, _root_symbol(r.tradingsymbol), _option_type(r.tradingsymbol))`.
  Futures/equity (`_option_type` → `None`) keep one bucket per root, unaffected.
- `frontend/src/lib/data/lotPairSplit.js`: mirror identically — group key becomes
  `${account}|${root}|${optType}` using `decomposeSymbol(sym).optType` (already
  imported/available, no new parsing).
- `backend/tests/test_positions_auto_pair.py` (or equivalent): extend with a
  short-PE + long-CE-same-qty-same-root case that must NOT pair (was a false
  pair before the fix), alongside existing short-PE+long-PE / short-CE+long-CE
  cases that must still pair.
- New parity test (architect's suggestion): one fixture run through both
  `_auto_pair_positions()` and `splitPairedLots()`, asserting identical
  pair-group labels — catches future drift between the two implementations.

### Part A — Positions grid per-account rows (do second; depends on nothing above)

- `frontend/src/lib/data/pulseUnified.js`, `mergePositionRows()`: drop the
  `_isLotSplit`-only conditional; `lookupKey` is now always
  `` `${sym}::${String(r.account ?? '')}` `` (lot-split pieces append their
  existing `::${lot_piece_label}` suffix on top). Remove the now-unreachable
  `row.accounts.size > 1` branch (lines ~473-483) — every row is single-account
  by construction, so only the `if (row.accounts.size === 1)` body survives;
  simplify to an unconditional assignment of `is_orphan`/`pair_group_key`/
  `pin_group_key`/`has_gtt` straight from `r`.
- **Required fix, same commit** — `MarketPulse.svelte`'s `_accumTotalsRow`
  (Exp P&L TOTAL accumulation): currently adds `_filteredExpPnlByKey[sym]`
  once per row in `positionsRows`. Re-key `_filteredExpPnlByKey` by
  `${account}|${symbol}` (or track a `Set` of already-counted bare symbols)
  so splitting one symbol into N account-rows does not multiply its
  contribution to the TOTAL row. New Vitest/Playwright test: a symbol held in
  two accounts, assert TOTAL Exp P&L equals the un-split sum (same pattern as
  the existing `position_lot_pair_split.spec.js` test (b)).
- **Subtotal mitigation (UX + research convergent ask)** — wire the backend's
  already-computed, currently-unconsumed `PositionsResponse.symbol_summary`
  (`_build_polars_symbol_summary`, `positions.py`) into MarketPulse as a thin
  group-header/subtotal row rendered above each symbol's split account-rows
  (only shown when a symbol has 2+ contributing rows) — preserves the "total
  exposure to this symbol across all my accounts" at-a-glance read that the
  old aggregated row used to provide for free. Reuses an existing backend
  field; no backend change needed.
- `pulseGridSetup.js:pulseRowId()` — confirmed needs NO change (already
  prefers `data.key`, which `mergePositionRows` sets from `lookupKey`).
- **Rewrite, not extend**: existing Vitest tests for `mergePositionRows` that
  assert "two accounts, same symbol → one merged/summed row" must be rewritten
  to assert "two accounts, same symbol → two rows, each correct per-account,
  plus a symbol-subtotal row" — their core assertion inverts, per devil's
  explicit flag.
- `docs/specs/PULSE_SPEC.md`: amend to describe per-account rows + the new
  subtotal row + the CE/PE pairing rule.
- `CLAUDE.md`: amend the "Position manual pairing" entry with the option_type
  grouping fix and the per-account-row change (follows existing entry style).

## Verification

- `cd frontend && npx vitest run` — full suite green (includes rewritten +
  new tests above).
- `venv/bin/pytest backend/tests/ -q --tb=line` — green, including the new/
  extended `_auto_pair_positions` cases and the parity test.
- `cd frontend && npx svelte-check --output machine` — no new errors.
- Playwright: extend `frontend/e2e/position_lot_pair_split.spec.js` (or a new
  spec) with a 2-account-same-symbol fixture — assert 2 rows render, each
  shows its own St-column badge (not nulled), TOTAL Exp P&L matches the
  un-split sum, and the new subtotal row shows the combined qty/value.
- Manual: a short PE + long CE (same root/account/qty) no longer badges as
  paired (shows orphan on both); a short PE + long PE still pairs; a
  multi-account symbol shows N rows + 1 subtotal row in MarketPulse.

## Done when

- `_auto_pair_positions`/`splitPairedLots` never cross-pair CE against PE.
- MarketPulse shows one row per (account, symbol), matching Legs/PerformancePage.
- TOTAL row's Exp P&L is unaffected by the per-account split (verified by test).
- A multi-account symbol still shows its combined exposure via the new subtotal row.
- All rewritten/new tests pass; `docs/specs/PULSE_SPEC.md` and CLAUDE.md reflect
  the change.
