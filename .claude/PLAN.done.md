# Plan: Margin fix_qty MCX math + Automation status-card colors + Agents row text size

## Task

Three bounded fixes:

1. **Margin-shortfall "reduce qty to N" is wrong for MCX/NCO orders** —
   `backend/api/algo/actions_preflight.py:_preflight_margin_shortfall_fix_qty`
   divides the quoted margin by raw CONTRACTS, but `basket_order_margins`
   was called with the LOT-converted quantity for MCX/NCO (`translate_qty`
   converts contracts→lots on the wire for those two exchanges only). The
   suggested qty is off by a factor of `lot_size` and often isn't even a
   valid lot multiple. NFO/BFO/CDS are unaffected. Fix: thread
   `exchange`/`lot_size` through `_preflight_margin_shortfall_fix_qty` →
   `_preflight_handle_positive_margin` → `_preflight_check_margin`, price
   per LOT for MCX/NCO, convert the suggestion back to contracts for
   display. Re-fetch `lot_size` via the cached `get_lot_size()` inside
   `run_preflight` rather than changing `_preflight_build_basket_orders`'s
   return signature (it has a second caller in `backend/api/routes/
   funds.py:275` and its own test mock in
   `backend/tests/test_funds_order_margin.py:26` — do not touch either).

2. **Automation status cards show no color** — rows already carry
   `data-status={row.status}` on `.algo-status-card`
   (`frontend/src/routes/(algo)/automation/+page.svelte:748-749`), and a
   global `[data-status]` color rule exists in `app.css:2740-2753`, but
   this page's own scoped CSS override (`+page.svelte:917-927`) sets a
   flat border color unconditionally, ignoring status. Real agent status
   vocabulary (`statusDot()`, line 443): `active/inactive/triggered/
   running/cooldown/error` — `active`/`cooldown` aren't in the global map.
   Brackets/Templates rows have no `status` field at all. Fix: add local
   `data-status` border-color variants for the full agent vocabulary
   (reuse existing app.css palette tokens, no new hex colors), and change
   line 749 to `data-status={row.status ?? (row.is_active ? 'active' : 'inactive')}`
   so Brackets/Templates derive a color from `is_active`.

3. **Agents-page row text too large** — `+page.svelte:758,760` render
   `row.name`/`row.long_name` at `--fs-lg`, inconsistent with the rest of
   the app's row-text convention (`--fs-xs`/`--fs-sm`). Fix: change both
   to `--fs-sm`.

## Agents

- backend: Fix `_preflight_margin_shortfall_fix_qty` (actions_preflight.py
  line 388) to accept `exchange: str = ""` and `lot_size: int = 0` params.
  When `exchange in ("MCX", "NCO") and lot_size > 0`: price per lot
  (`priced_units = qty // lot_size`, `per_unit = required / priced_units`),
  then convert the suggested fit back to contracts
  (`max_qty_fit = max_units_fit * lot_size`). Otherwise unchanged (today's
  contracts-based math). Thread the new params through
  `_preflight_handle_positive_margin` (line 412) and
  `_preflight_check_margin` (line 494). In `run_preflight` (line 752),
  re-fetch `lot_size` via `get_lot_size(exchange, symbol)`
  (`backend.brokers.adapters.kite.get_lot_size` — O(1) cached lookup, no
  network) right before the `_preflight_check_margin` call and pass it +
  `exchange` through. Do NOT change `_preflight_build_basket_orders`'s
  signature or touch `backend/api/routes/funds.py`. Add a test to
  `backend/tests/test_preflight_characterization.py` (new test alongside
  the existing `test_preflight_margin_shortfall_fix_qty_calculation` at
  line 972): an MCX case with a known lot_size/required/available,
  asserting the suggested qty in the `fix` string is a valid multiple of
  lot_size and matches the corrected lot-priced math. Confirm the
  existing NFO test still passes unchanged (regression-free).

- frontend: In `frontend/src/routes/(algo)/automation/+page.svelte`: (a)
  add `[data-status="active"]`, `[data-status="cooldown"]` variants to
  the existing `.algo-status-card[data-status="..."]` pattern already
  used elsewhere (app.css:2740-2753) — but scoped locally in this file's
  `<style>` block (lines 917-927), setting `border-color` (and optionally
  a left-edge accent) per status, reusing existing `--algo-*`/`--c-long`/
  `--c-short` tokens, matching the decoration level of `OrderBook.svelte`'s
  `.ob-sc[data-status]` chips — do not touch the global app.css rule or
  OrderBook.svelte. (b) Change line 749's `data-status={row.status}` to
  `data-status={row.status ?? (row.is_active ? 'active' : 'inactive')}`.
  (c) Change lines 758 and 760's `--fs-lg` to `--fs-sm`.

- playwright: Add a spec asserting that on `/automation`, an active
  agent row's `.algo-status-card` has a visibly different computed
  `border-color` than an inactive agent row's, and that a Brackets-kind
  row (no native `status` field) also gets a non-default border color
  when `is_active` is true. Place in `frontend/tests/` per the project's
  test-location map.

## Tests

- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message

fix(preflight,automation): correct MCX margin-shortfall qty math, color-code status cards, fix Agents row text size

## Done when

- An MCX/NCO order's MARGIN_SHORTFALL `fix` text suggests a valid
  lot-multiple contract quantity, priced correctly per lot.
- NFO/BFO/CDS margin-shortfall behavior is byte-identical to today
  (regression-free — covered by the existing NFO test).
- Every Automation row card (all four kinds) shows a status-keyed
  border color, matching Orders' status-card decoration style.
- Agents-page row names render at `--fs-sm`, matching the rest of the
  app's row-text convention.
- pytest green, svelte-check 0 errors, new Playwright spec green.
