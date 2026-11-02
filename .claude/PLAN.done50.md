# Plan: Bracket UX — scope-reset-not-clear, paired display, "Pending" label, basket hold wiring

## Task

Multiple linked fixes to the Bracket/template-exit-hold flow, found and confirmed this session
while investigating two live reports ("Market closed — order will queue" on a CRUDEOIL PE via
Chain, and "Bracket did not attach"). Root cause of the attach failure was already fixed live on
prod (template 5's `tp_order_type` MARKET→LIMIT, in both `order_templates` and the mirrored
`agents.bracket_config`; order #1377's TP GTT is now placed). This plan covers the remaining,
confirmed gaps. The reconciliation against the untriaged 2026-11-01 bracket audit found no
recoverable findings list — the only trace is commits `68f889a1`/`85618c6b`, both already shipped
and unrelated to item 1 below (confirmed by reading the actual diff: `68f889a1` only touches
`OrderTicket.svelte`'s own re-validation effect, never `SymbolPanel.svelte`'s separate one).

## Confirmed findings

1. **`SymbolPanel.svelte`'s `_lastSideScope` effect (lines ~1333-1379) silently clears the Bracket
   to `null` on a scope change, with no flash — distinct from and not covered by `68f889a1`.**
   Operator ask: on scope change (strike/CE-PE/side), a **Default**-slot bracket should re-resolve
   to a fresh default matching the NEW scope (not drop to nothing), and this should be immediate
   and visible. A **named** (non-default) pick is already protected (line ~1366, unchanged —
   explicit choices must not be silently swapped). Fix: when the current pick is the default and
   the new scope's own default differs, re-bind `_sharedTemplateId` to the NEW scope's default
   (reuse whatever helper already resolves "the default template for scope X" — likely shared with
   `_onTemplSelectDefault`), instead of nulling. Only fall through to `null` when no default exists
   for the new scope at all. Either way, fire the same flash mechanism `OrderTicket.svelte` already
   uses (#16, "Bracket cleared for new side" / add a parallel "Bracket switched to default for new
   side" variant), surfaced through whatever prop/callback bridges SymbolPanel → the visible toast
   slot (confirm the exact mechanism during implementation — SymbolPanel already has a brief
   success/error flash slot per the comment at line ~194).

2. **Paired main+bracket display + triggered-with-limit-price status.** Operator ask: once a
   bracket is selected or reset, show it immediately alongside the main order (not only after the
   wing/TP actually fires live, which is the existing `6fbbe205` Chain-grid badge). Once chase fills
   the main order, the paired bracket entry's status should change to "triggered" and show its
   resolved limit price, matching the TP-stays-GTT decision already confirmed with the operator
   (not chased — only the main order is chased; TP/SL remain broker GTT orders, display-only change
   here). Scope this to wherever the main order + its pending bracket are already shown together
   today (OrderCard/ChaseCard) — confirm the exact mount before building; do not duplicate
   `6fbbe205`'s Chain-grid-only badge, which stays as is (that one is strike-row-scoped and
   post-fire only, by design).

3. **"Pending" replaces "HELD"/"Held" label for the template-exit hold state, on every surface** —
   operator: "held" already means something else (a covered-call leg label elsewhere in the app) and
   the longer "Awaiting release" was too big for the chip. Surfaces to change (confirmed via grep,
   this list is exhaustive for the hold-gate's own vocabulary — do NOT touch any "Held"/"HELD" text
   belonging to the covered-call feature, which is a different, correct, pre-existing usage):
   - `frontend/src/lib/order/OrderCard.svelte:245` — badge text `HELD` → `Pending` (keep the CSS
     class `held` as-is; this is an internal selector, not operator-visible text).
   - `frontend/src/lib/OrderBook.svelte:538` and its chip label (grep for the matching display
     string near the `held: st => ...` predicate).
   - `frontend/src/lib/HeldOrdersCard.svelte:27` (`return 'Held';`) and its header/`aria-label`
     ("Held orders" at line 122-123) — rename to "Pending orders" / `return 'Pending';`. Confirm
     whether this card's title should also change, or just the per-row status word — operator asked
     for the STATE label to shrink; the card's own name is a separate call, default to leaving
     "Held orders" as the card title (operator context: templates/positions being reviewed, not
     renamed) unless it reads confusingly once the per-row text says "Pending" — use judgement,
     flag if ambiguous rather than guessing silently.
   - `frontend/src/lib/LogPanel.svelte:971` and `frontend/src/lib/OrderBook.svelte:368` — the toast
     text itself (see item 4 below — this toast's WORDING also needs a correctness fix, not just
     the "Held"/"Pending" swap, so do both in the same edit).
   - Backend: `order_hold_gate.py:hold_template_exit()`'s `write_event(..., "held", f"Template
     exits held for {symbol}", ...)` message text, and `record_held_order()`'s `detail=f"HELD:
     {reason}"` string (`order_hold_gate.py`) — these are operator-facing via `OrderCard`'s
     `title={...holdInfo.reason...}` tooltip and `HeldOrdersCard`'s detail column. The `kind="held"`
     event-type string itself (the DB enum value) should NOT change — only the human-readable
     message text inside it.

4. **"Bracket did not attach — check Order Book" toast is factually wrong for the
   template-exit-hold case, and needs to say something different.** Today it fires identically
   whether the exit genuinely failed to attach OR was correctly held pending release (the normal,
   working case once the global switch is off, or on a per-order override). Fix
   `templateAttachToast.js`'s `noteAttachObservation()` (or wherever it distinguishes these) to
   check `holdInfo?.category === 'template_exit'` first: if held-pending-release, show something
   like `Order #${oid} Bracket pending — release from Held Orders` instead of the current
   "did not attach" wording (which implies a failure). The genuine-failure case (no hold record,
   attach really errored) keeps the current "did not attach" text.

5. **`orders_basket.py` never threads the per-order `hold_template_exit` override through** —
   `orders_place.py` (ticket route, lines ~2107/2562) reads `data.hold_template_exit` and sets
   `AlgoOrder.template_hold_override`; `orders_basket.py` has zero references to either. This means
   a Chain/basket submission can NEVER use a per-order release override — only the global switch
   governs it. Add the same field/threading to the basket leg's `AlgoOrder` construction, mirroring
   `orders_place.py`'s existing pattern exactly (same field name, same default `None`/unset
   behaviour — no change for a leg that doesn't set it).

6. **`HeldOrdersCard` only lists `status == 'HELD'` rows, so a template-exit hold (which leaves the
   PARENT row at `status='FILLED'` with `hold_json` set — see `order_hold_gate.py:
   hold_template_exit()`) is invisible there.** This was the actual reason order #1377 required a
   direct server-side script to release — the UI had no path to it at all, even though
   `release_template_exit()` already works by order id. Extend `orders_held.py`'s list query (or
   add a second query, unioned) to also include rows where `hold_json IS NOT NULL AND
   parse_hold_record(hold_json).category == 'template_exit'`, regardless of `status`. Wire the
   existing `releaseHeldOrder(row.id)` action through unchanged — `release_template_exit` already
   handles this category correctly (confirmed live: re-held safely on a genuine attach failure,
   succeeded once the template's own `tp_order_type` bug was fixed). Apply the item-3 "Pending"
   label to these newly-visible rows too.

## Deferred, not in this plan (explicitly out of scope — do not implement without a fresh decision)

- **Retry-decorator reachability for `ticket_preview`'s `@recoverable` wrapper** (the subagent's
  work, currently uncommitted on `backend/brokers/base.py` / `backend/api/routes/orders.py`) — advisor
  flagged that `apply_template_to_order(..., apply_path="preview")` may never reach
  `_validate_exchange_qty_multiple`/`InstrumentsCacheMissError` in the real preview path, making the
  retry structurally inert. This needs its own investigation (read `_rco_preview_resolve_qty`'s real
  call chain, confirm or refute the subagent's claim) before deciding whether to commit the current
  diff as-is, extend it, or redo the wrapper placement. Not resolved yet — do not commit this diff as
  part of this plan's implementation; it is independent work.
- **"Market closed — order will queue" warning root cause on the CRUDEOIL PE via Chain** — ruled out
  the cold-instruments-cache-after-restart hypothesis (ramboq_conn had 2.5+ hours uptime) and ruled
  out `OrderTicket.svelte`'s own CE/PE rescue logic (symmetric, already covers PE). True cause not
  yet found — needs its own investigation thread, likely in `OptionChainTab.svelte`'s own exchange
  resolution path (this order went through Chain/basket, not the Ticket tab) rather than
  `OrderTicket.svelte`'s. Does not block anything here since the order still placed.

## Agents

- frontend: items 1, 2, 3 (frontend surfaces), 4, 6 (frontend list-query consumption + label),
  per the Confirmed Findings section above. Needs to confirm the exact flash-bridging mechanism for
  item 1 and the exact mount for item 2 before writing code (both explicitly flagged "confirm during
  implementation" above — do not guess).
- backend: items 3 (backend strings), 5, 6 (backend list query). Self-contained; no broker calls
  change, only DB field threading and a query filter addition.
- doc: CLAUDE.md entries for whichever of the above ship — new "Bracket scope-reset on Chain",
  "Pending label for template-exit hold", "orders_basket hold-override wiring", "HeldOrdersCard
  surfaces template-exit holds" sections, following this file's existing dense cross-referencing
  style (file:line links). Also correct the live-DB-fix record (template 5's `tp_order_type`) into
  a dedicated CLAUDE.md entry under Critical math guards — this was a real live incident fix not yet
  documented anywhere, applied directly to prod DB this session (both `order_templates.id=5` and the
  mirrored `agents.id=75.bracket_config`).
- backend-test / playwright: per the standing test-gate rule — every changed file needs a
  corresponding test. Item 1 (scope-reset) and item 4 (toast wording) are the highest-value
  Playwright targets; items 5/6 need pytest coverage on the new field threading and query filter.

## Tests

- pytest: yes (items 3 backend strings — test the message text; item 5 field threading; item 6
  query filter)
- svelte-check: yes
- playwright: yes (item 1 scope-reset-not-clear behavior + flash; item 4 toast wording distinction;
  item 6 HeldOrdersCard showing a template-exit-held row and successfully releasing it)

## Commit message

fix(bracket): scope-change resets Bracket to new default (not silent clear) on Chain; "Pending"
label replaces "Held" for template-exit holds everywhere; basket route threads per-order hold
override; HeldOrdersCard surfaces template-exit holds; toast distinguishes pending-release from
genuine attach failure

## Done when

- Changing scope (strike/CE-PE/side) on Chain while a Default bracket is armed re-binds to the new
  scope's own default (when one exists) instead of silently clearing to nothing, with a visible
  flash either way — proven by a Playwright test, not just code review.
- Every "HELD"/"Held" surface belonging to the template-exit-hold mechanism (never the covered-call
  feature) now reads "Pending".
- The "Bracket did not attach" toast reads differently for a genuinely-held-pending-release order
  vs. a real attach failure.
- `orders_basket.py` leg construction threads `hold_template_exit`/`template_hold_override` exactly
  like `orders_place.py` already does.
- `HeldOrdersCard` lists a template-exit-held row (parent `status='FILLED'`, `hold_json` set) and
  its existing Release button correctly calls `release_template_exit` for it.
- `svelte-check` clean, pytest green, new Playwright specs pass.
