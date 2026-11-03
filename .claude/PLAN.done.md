# Plan: show a live-placed Bracket wing/hedge leg on its Chain strike row

## Context

Operator, placing Bracket orders through the Chain tab: "when bracket is selected, if will place
live hedge orders sometimes. in chain, I want the live order should be selected as if placed by
the user, while placing orders with brackets online in chain." Confirmed interpretation
("a visual confirmation badge on the wing's strike row once it's actually live, distinct from —
and never re-submittable like — a manually staged leg") with an explicit "yes" before this plan
was written.

A Bracket template's wing/hedge leg is placed asynchronously, on parent fill, by
`_fire_template_attach_on_fill` → `apply_template_to_order` (`backend/api/algo/template_attach.py`)
— not synchronously at Submit time. By the time it lands as a real broker order, the operator may
already be looking at a different strike, or the Chain view may have re-rendered from a fresh
poll. Today there is no visual confirmation inside the Chain grid itself that the wing actually
fired — the operator has to go check OrderBook/positions separately.

**Confirmed via investigation, not assumed**: `OptionChainTab.svelte`'s `chain-leg-badge` (shows
lots + a truncated Bracket template name on a strike's CE/PE cell) is driven entirely by
`_basketLegByKey` (`OptionChainTab.svelte:563`), itself derived from `chainBasket` — the
PRE-SUBMIT staging array (`basketLegs`, shared with `SymbolPanel.svelte`). There is no
position-awareness in this component today; this is new plumbing, not a missing wire-up.

## Critical constraint (why this can't just push into `basketLegs`)

`chainBasket`/`basketLegs` is specifically "legs about to be submitted." If a confirmed-live wing
leg were added to that same array, the next Submit click could attempt to re-place it as a
duplicate live order — a real-money bug, not a cosmetic one. The wing leg's badge must be a
**read-only reflection of a real position**, structurally incapable of re-entering the submit
flow — never a `chainBasket` entry.

## Design

**Signal**: `AlgoOrder.parent_order_id` is the existing SSOT field marking "this order is a
wing/child of a template attach" (CLAUDE.md: "the only order-level pairing signal execution paths
read"). A wing order's `parent_order_id` is the parent ticket's `AlgoOrder.id`.

1. **Track this session's own recently-submitted parent order ids.** `SymbolPanel.svelte`'s
   basket/ticket submit path already receives back the parent `AlgoOrder.id` (used elsewhere for
   `_ticket_seed_broker_order_id` etc. on the backend; confirm the exact response field the
   frontend already reads for this on submit success). Keep a small, session-local
   `$state` set of `{account, symbol, parentAlgoOrderId, submittedAt}` for parents placed THIS
   session — bounded (e.g. drop entries older than ~15 minutes, or once a wing is confirmed/the
   position is seen flat) so this never grows unbounded across a long session.
2. **Detect the wing firing.** Reuse the existing orders-polling mechanism already active on this
   page (`/api/orders/algo/recent` or equivalent — confirm the exact endpoint/store already
   polling here, e.g. `dataStore`/`marketDataStores.svelte.js`, rather than adding a new poll
   loop) — filter for any row whose `parent_order_id` matches one of the tracked parent ids from
   step 1. This is a client-side filter over data already being fetched, not a new network call.
3. **Resolve the matched wing order's row to a strike/optType.** Reuse the existing symbol-parsing
   helpers already used for `addOptionToBasket`'s own strike/optType stamping (confirm exact
   function name — likely `decomposeSymbol`/`_parseRoot` per the DSL/positions work earlier this
   session) so the wing's tradingsymbol resolves to the SAME `${strike}:${optType}` key
   `_basketLegByKey` already uses.
4. **Render a separate, read-only badge map.** New `_liveWingByKey` derived (`Map<string, {lots,
   templateName, side}>`), built from step 3's matches — kept entirely separate from
   `_basketLegByKey`. In the row template (`OptionChainTab.svelte:1306-1307` area, where
   `ceLeg`/`peLeg` are resolved), check `_liveWingByKey` ALONGSIDE `_basketLegByKey`: if a strike
   has a live wing match, render the SAME `chain-leg-badge` markup/classes for visual consistency
   (lots + truncated template name — the operator asked for "selected as if placed by the user"),
   but with a distinct modifier class (e.g. `chain-leg-badge-live`, a solid/filled treatment vs.
   the staged badge's current style) so it's visually distinguishable as "already live," not an
   editable staged leg — and critically, this row's `+`/`-` buttons must NOT treat it as a
   `chainBasket` entry (no accidental qty mutation of a live position through the staging UI).
5. **Expire the badge.** Once the matched wing's `parent_order_id` entry drops out of the
   session-local tracked-parents set (per the bound in step 1), or once the wing order itself is
   confirmed via the poll to be in a terminal non-matching state, the badge disappears — this is a
   session-scoped confirmation UI, not a permanent position indicator (OrderBook/Positions remain
   the permanent record).

## Explicit scope boundary

- Does NOT touch `basketLegs`/`chainBasket` at all — purely additive, read-only.
- Does NOT add a new polling endpoint — reuses whatever orders-poll already feeds this page.
- Does NOT persist across a page reload/new session — session-local tracking only, by design (a
  stale badge from a much earlier session would be confusing, not helpful).
- Does NOT affect any other surface (Positions grid, OrderBook) — this is Chain-grid-only visual
  confirmation.

## Files (to confirm exact names/line numbers during implementation, not guessed here)

- `frontend/src/lib/order/OptionChainTab.svelte` — `_liveWingByKey` derived, row-template badge
  branch, the modifier CSS class.
- `frontend/src/lib/SymbolPanel.svelte` — the session-local tracked-parents state (own this at the
  shell level if `OptionChainTab` doesn't already own submit-response handling directly — confirm
  during implementation which component actually receives the parent order id on submit success).
- Confirm which existing store/poll already supplies recent algo-order rows to this page before
  adding any new fetch.
- Tests: a new Playwright spec — stage+submit a Bracket order with a wing-eligible template,
  mock the backend fill + wing-attach response, assert the wing's strike row shows the live badge
  (not the staged badge), and assert `basketLegs`/`chainBasket` never contains the wing leg.

## Agents

- frontend: full implementation + Playwright test, per the Design section above. Needs to
  confirm (not assume) the exact submit-response shape, the existing orders-poll data source, and
  the symbol-parsing helper names before writing code.

## Tests

- pytest: no (frontend-only)
- svelte-check: yes
- playwright: yes

## Commit message

feat(chain): a live-placed Bracket wing/hedge leg shows a read-only confirmation badge on its
strike row, distinct from and never re-submittable like a staged leg

## Done when

- A wing/hedge order placed via the template-attach mechanism, for a parent submitted THIS
  session from Chain, shows a badge on its strike's CE/PE cell once confirmed live.
- That badge is visually distinct from the staged-leg badge (operator can tell "already live"
  from "about to submit" at a glance).
- The wing leg is never added to `chainBasket`/`basketLegs` — a subsequent Submit click cannot
  re-place it, proven by a test.
- The badge is session-scoped (bounded tracking window), not a permanent position indicator.
- `svelte-check` clean, new Playwright spec passes, full Vitest suite green.
