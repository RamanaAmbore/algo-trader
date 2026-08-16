# Plan: Pair button visibility + modal redesign + position selection + backend + template integration path

## Context

Extensive audit across all three layers reveals **two disconnected pairing systems** that confuse the UX:

1. **Position pair groups** — `pair_group_key` ("P1", "P2"…) computed server-side by lot-waterfall algorithm in `_auto_pair_positions()` (`positions.py:71-172`). Ephemeral — never persisted to DB. Operator has zero control over which positions are paired.

2. **Order parent-child** — `parent_order_id` on AlgoOrder, set via `POST /api/orders/pair`. Currently used for TP/SL child linking. The "Pair" button in the derivatives page (`+page.svelte:4184`) opens `OrderPairModal` which does THIS — not position pairing.

The current "Pair" button is semantically misleading: it opens an order-linking modal (dropdowns showing AlgoOrders by ID), but the operator expects it to control the position pair groups shown in the St column. These two systems are completely unconnected.

**Key audit findings:**
- No `--` prefix exists on the pair button itself — the `-- select parent --` / `-- select child --` text is in the OrderPairModal dropdown option placeholders (`OrderPairModal.svelte:50,58`)
- Pair button is 0.68rem, muted grey-blue, low contrast — essentially invisible (`+page.svelte:5545-5554`)
- MarketPulse has `⟷ Pair` (bidirectional arrow icon) while derivatives has plain "Pair" — inconsistency
- `TemplatePlan` is single-leg only — no `pair_group_id`, no `sibling_qty` fields
- `paired_legs` parameter in orders preflight (`orders.py:1018-1021`) is parsed but unused
- Paper positions never enter the waterfall — they don't get `pair_group_key` set (bug)

---

## Task

Four deliverables in order of scope:

1. **Pair button visibility** — match MarketPulse styling: add `⟷` icon, raise contrast, increase font/padding
2. **OrderPairModal cleanup** — remove `--` from dropdown placeholder option text
3. **Position selection flow documentation** — wire the checkbox selection into the modal so it pre-filters to checked positions' existing AlgoOrders (not all 200 recent orders)
4. **Template integration path** — document (in DESIGN_GUIDE section and code comments) what data model changes are needed for dual-leg template placement in the future

Scope: items 1 and 2 are code changes. Items 3 and 4 are architecture notes + placeholder wiring. Full dual-leg template placement is future work — do NOT implement the order coordination logic now.

---

## Agents

- frontend: Two fixes:
  1. **Pair button styling** (`+page.svelte:5545-5554`): Change `.leg-pair-btn` to match MarketPulse's `.mp-pair-btn` — increase font-size from `0.68rem` to `0.75rem`, increase padding from `0.1rem 0.4rem` to `0.18rem 0.55rem`, raise color from `rgba(160,185,220,0.8)` to `rgba(190,210,240,0.95)`, raise border from `rgba(160,185,220,0.3)` to `rgba(160,185,220,0.55)`, add hover `color: #e2eeff`. Also update the button text in `+page.svelte:4185` from `>Pair<` to `>⟷ Pair<` to match MarketPulse.
  2. **OrderPairModal placeholders** (`OrderPairModal.svelte:50,58`): Change `— select parent —` and `— select child —` (or `-- select parent --` / `-- select child --`) option text to `Select parent order` and `Select child order` (remove the `—`/`--` prefix decorators from the default option).
  3. **Modal pre-filter + lot-size split preview (account-aware)** (`OrderPairModal.svelte` + `+page.svelte`): 
     - Add `export let symbolHint = ''` and `export let pairedCandidates = []` props to `OrderPairModal.svelte`. `pairedCandidates` is an array of at most 2 candidate position rows (each with `tradingsymbol`, `quantity`, `account`, and qty field).
     - **Account-aware pairing (hard constraint — no cross-account)**: pairing is only valid within the same account. In `+page.svelte`:
       - Compute `_pairedCandidates` as the first two enabled candidates (`_isLegEnabled(c) === true`) that share the **same `account`** value. If enabled candidates span multiple accounts, only take same-account pairs from the first account seen.
       - The `⟷ Pair` button itself should be disabled (greyed out, `disabled` attribute set) when fewer than 2 same-account candidates are checked. Add a tooltip: "Select 2 positions from the same account to pair".
     - At the top of the modal body (before the order dropdowns), if `pairedCandidates.length >= 2`, render a **lot-size preview panel**:
       - Show each leg: `ACCOUNT | SYMBOL | ±N lots` (account display: last 4 chars masked, e.g. `••••1234`)
       - Both legs guaranteed same account (enforced at button-enable level), so no cross-account warning needed in modal
       - Compute `proposedPairedQty = Math.min(Math.abs(qty_a), Math.abs(qty_b))` and `orphanQty = Math.abs(Math.abs(qty_a) - Math.abs(qty_b))`. Display: "Matched: N lots | Orphan: N lots". Style amber if `orphanQty > 0`, cyan if perfectly matched.
     - Filter the parent order dropdown to orders whose symbol contains `symbolHint` AND whose account matches `pairedCandidates[0].account`.
     - In `+page.svelte` (wherever `<OrderPairModal>` is rendered), compute `_pairedCandidates` (first two same-account enabled candidates), `_firstCheckedSymbol` (first candidate's `tradingsymbol`), and pass: `<OrderPairModal bind:open={_legPairModalOpen} symbolHint={_firstCheckedSymbol} pairedCandidates={_pairedCandidates} />`.
     - No backend change needed — purely cosmetic preview.

- backend: skip (no backend changes in this scope — pair group data model extension is future work)
- backend-test: skip
- doc: Add a `## Pair System Architecture` section to `docs/DESIGN_GUIDE.md` describing:
  - The two disconnected pair concepts (position pair groups vs order parent-child)
  - The waterfall algorithm and its inputs/outputs
  - What `pair_group_key`, `paired_qty`, `orphan_qty`, `is_orphan` mean
  - **Account constraint invariant**: pairing is always intra-account. The waterfall groups by `(account, root_symbol)` — cross-account pairing is never valid. The UI enforces this at the button-enable level (pair button disabled unless ≥2 same-account candidates checked). Document why: hedges are settled per-account; cross-account position offsets are not recognized by brokers.
  - Current OrderPairModal purpose (TP/SL child linking, NOT position pairing)
  - **Future integration path**: to support dual-leg template placement, the following are needed:
    1. `pair_group_id: Optional[str]` on AlgoOrder (not parent_order_id — separate field)
    2. `TemplatePlan.sibling_account + sibling_symbol + sibling_qty` fields
    3. `apply_plan_live()` to accept a counterpart fill event and fire GTTs on both legs atomically
    4. `POST /api/positions/pair` endpoint (new) to let operator manually override waterfall pair groups and persist them to a new `position_pair_groups` table
  - Reference: `backend/api/routes/positions.py:71-172` (waterfall), `backend/api/routes/orders.py:1807-1837` (order pairing), `backend/api/algo/template_attach.py:78-94` (TemplatePlan)
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(derivatives): pair button ⟷ icon + visibility + modal placeholder cleanup + pair arch doc

## Done when
- Pair button in derivatives shows `⟷ Pair`, same icon as MarketPulse
- Pair button is clearly readable (higher contrast, slightly larger)
- Pair button is disabled when fewer than 2 same-account candidates are checked
- OrderPairModal shows lot-size split preview (Matched / Orphan) for the two selected same-account positions
- OrderPairModal dropdown placeholders read "Select parent order" / "Select child order" (no `--` prefix)
- Modal pre-filters parent dropdown by symbol and account of selected candidates
- Cross-account pairing is blocked at the button level (hard constraint, not just a warning)
- DESIGN_GUIDE.md has a Pair System Architecture section documenting both concepts, account constraint, and future integration path
- svelte-check 0 errors
