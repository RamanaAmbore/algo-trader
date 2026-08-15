# Plan: Order Pair Button + O Chip + Mobile Footer Fix

## Context

Four related improvements:

1. **O chip**: Position rows and order rows have no visual signal when an order is "orphaned" — either (A) a broker position exists with no active AlgoOrder tracking it, or (B) a child AlgoOrder exists whose parent has been cancelled/filled and is no longer in the active chase list. Operators cannot spot these at a glance.

2. **Pair button + modal**: There is no mechanism to manually establish a parent-child relationship between two existing AlgoOrders. The pair button lives in the header row of both the Pulse position grid and the Derivatives legs grid; clicking it opens a two-picker modal (parent picker + child picker) that calls `POST /api/orders/pair`.

3. **Pair-group sort**: Once two AlgoOrders are paired (child.parent_order_id = parent.id), their corresponding broker positions should behave as one unit in the sort order — child row always follows its parent regardless of which column the user sorts by.

4. **Mobile footer fix**: "RamboQuant Analytics" appears in the footer on mobile views where space is tight; keep it on desktop, remove on mobile.

---

## Agents

- backend: Add `is_orphan` to PositionRow + `/api/orders/pair` endpoint. Working dir: `/Users/ramanambore/projects/ramboq`.

  **Change 1 — `backend/api/schemas.py` (PositionRow, ~line 106)**
  Add two fields:
  - `is_orphan: bool = False` — True when no AlgoOrder with `status='OPEN'` matches this position's (account, tradingsymbol).
  - `pair_group_key: str | None = None` — shared key for positions linked via an AlgoOrder parent-child relationship. Set to the ROOT parent AlgoOrder id (as string) for both the parent position and all child positions in the same pair. `None` when no AlgoOrder matches.

  **Change 2 — `backend/api/routes/positions.py`**
  Before building the PositionRow list in the live-broker path (and snapshot path):
  - Run one extra query: `SELECT id, account, symbol, parent_order_id FROM algo_orders WHERE status = 'OPEN'` — build:
    - `open_order_set: set[tuple[str,str]]` for orphan detection
    - `order_by_sym: dict[tuple[str,str], row]` mapping (account, symbol) → order row
  - For each PositionRow:
    - `is_orphan = (account, tradingsymbol) not in open_order_set`
    - `matched = order_by_sym.get((account, tradingsymbol))`
    - If matched: `pair_group_key = str(matched.parent_order_id or matched.id)` (root parent id)
    - Else: `pair_group_key = None`
  - Do the same in `build_row_from_snapshot_raw`: accept an optional `order_map` parameter.

  **Change 3 — New endpoint in `backend/api/routes/orders.py`**
  ```python
  @dataclass
  class PairOrdersInput:
      parent_id: int
      child_id: int

  @post("/pair", guards=[admin_guard])
  async def pair_orders(self, data: PairOrdersInput, session: AsyncSession) -> dict:
      parent = await session.get(AlgoOrder, data.parent_id)
      child  = await session.get(AlgoOrder, data.child_id)
      if not parent or not child:
          raise NotFoundException("order not found")
      if child.parent_order_id is not None:
          raise ClientException("child already has a parent — unpair first")
      child.parent_order_id = data.parent_id
      await session.commit()
      return {"ok": True, "child_id": data.child_id, "parent_id": data.parent_id}
  ```
  Register route in the orders router (same router class that has `/pair` etc.).

  For every file you change, write or update at least one pytest test in `backend/tests/`.
  - Test `is_orphan=True` when no open AlgoOrder matches (mock `open_order_set` as empty)
  - Test `is_orphan=False` when a matching open AlgoOrder exists
  - Test `POST /api/orders/pair` happy path (sets parent_order_id)
  - Test `POST /api/orders/pair` with invalid IDs (404)
  - Test `POST /api/orders/pair` when child already has a parent (400)

- frontend: O chip + Pair button + modal + mobile footer. Working dir: `/Users/ramanambore/projects/ramboq`.

  **Change A — O chip in MarketPulse position rows**
  File: `frontend/src/lib/MarketPulse.svelte`, `_symCellBadges()` (~line 3195).
  After the existing `if (row.src?.m)` block, add:
  ```js
  if (row.src?.p && row.is_orphan) {
    badges.push(`<span class="sym-badge badge-o" title="No active order tracking this position">O</span>`);
  }
  ```
  Add CSS for `.badge-o` (coral accent, e.g. `background: rgba(251,113,133,0.18); color: #fb7185; border: 1px solid rgba(251,113,133,0.4);`).

  **Change B — O chip in ChaseCard for dangling children**
  File: `frontend/src/lib/order/ChaseCard.svelte`, orphaned-children rendering (~line 296-310).
  The `_orderedChases` derived already separates orphaned children (those at end of list). Detect orphan: `row.parent_order_id != null && !parentIdsInList.has(row.parent_order_id)`.
  Add an inline `<span class="cc-chip cc-chip-o" title="Orphan — parent order not in active chases">O</span>` in the cc-row for those rows.
  Add `.cc-chip-o` CSS matching `badge-o` color scheme.

  **Change C — Pair button in Pulse positions CardHeader**
  File: `frontend/src/lib/MarketPulse.svelte`, positions CardHeader `{#snippet left()}` (~line 4207).
  After the AccountMultiSelect block, add:
  ```svelte
  <span class="mp-head-sep" aria-hidden="true"></span>
  <button class="mp-pair-btn" onclick={() => pairModalOpen = true}
    title="Pair parent/child orders">⟷ Pair</button>
  ```
  Declare `let pairModalOpen = $state(false)` near the existing state declarations.
  Import `OrderPairModal` (new component, see Change E).
  Add `{#if pairModalOpen}<OrderPairModal bind:open={pairModalOpen} />{/if}` at the bottom of the template.

  **Change D — Pair button in Derivatives legs headrow**
  File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, `.leg-headrow` (~line 4154-4161).
  Replace the trailing empty `<span></span>` with:
  ```svelte
  <button class="leg-pair-btn" onclick={() => legPairModalOpen = true}
    title="Pair parent/child orders">Pair</button>
  ```
  Declare `let legPairModalOpen = $state(false)` and add the same modal at template bottom:
  `{#if legPairModalOpen}<OrderPairModal bind:open={legPairModalOpen} />{/if}`

  **Change E — New `OrderPairModal.svelte` component**
  Create `frontend/src/lib/order/OrderPairModal.svelte`.
  Layout: modal overlay → card → title "Pair Orders" → two labeled selectors → Submit + Cancel buttons.
  - **Data**: on mount, fetch `GET /api/orders/recent?n=200&mode=all` → `orders` list.
  - **Picker 1 (Parent)**: searchable `<select>` or `<input list>` over all orders. Shows `#${id} ${symbol} ${status}`.
  - **Picker 2 (Child)**: same list filtered to orders where `parent_order_id == null` (unlinked only).
  - **Submit**: `POST /api/orders/pair {parent_id, child_id}` → on success show brief "Paired" toast, close modal, dispatch `invalidate('/api/orders/chases/active')` so ChaseCard refreshes.
  - **Validation**: disable Submit if either picker is empty or if same order selected in both.
  Style with existing `.modal-overlay`, `.modal-card` classes (check existing modal components in codebase for the pattern).

  **Change F — Mobile footer: algo layout**
  File: `frontend/src/routes/(algo)/+layout.svelte`, lines 1359-1360.
  Wrap the brand span + its separator in a container:
  ```svelte
  <span class="algo-footer-brand">
    <span class="algo-footer-text">RamboQuant Analytics</span>
    <span class="algo-footer-sep">·</span>
  </span>
  ```
  Add CSS (near line 2107 where mobile overrides live):
  ```css
  @media (max-width: 639px) { .algo-footer-brand { display: none; } }
  ```

  **Change G — Mobile footer: public layout**
  File: `frontend/src/routes/(public)/+layout.svelte`, mobile paragraph (~line 207-213).
  The mobile paragraph (`<p class="md:hidden ...">`) currently has two lines: "© RamboQuant Analytics LLP" and "Built by...". Remove the first line (line 208) so the mobile footer only shows the "Built by" attribution.

  **Change H — Pair-group sort: postSortRows in positions grid**
  File: `frontend/src/lib/MarketPulse.svelte`, `makeBucketGrid()` or the gridPositions options block (~line 3542).
  Add a `postSortRows` callback to `gridPositions` options only (not holdings or other grids):
  ```js
  postSortRows(params) {
    const nodes = params.nodes;
    // Group nodes by pair_group_key; undefined/null = standalone
    const groups = new Map();   // key → [nodes in order]
    const order  = [];          // final ordered output
    const seen   = new Set();
    for (const n of nodes) {
      const k = n.data?.pair_group_key;
      if (k) {
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(n);
      }
    }
    for (const n of nodes) {
      if (seen.has(n)) continue;
      seen.add(n);
      order.push(n);
      const k = n.data?.pair_group_key;
      if (k) {
        // Splice remaining members of this pair immediately after
        for (const m of groups.get(k) ?? []) {
          if (!seen.has(m)) { seen.add(m); order.push(m); }
        }
      }
    }
    // Mutate nodes array in place
    nodes.length = 0;
    nodes.push(...order);
  }
  ```
  This mirrors the existing `postSortGroups` pattern used for underlying-grouping reorder (check `api.getColumnState()` early-return guard if an active user sort should still apply — keep the pair-group enforcement always on since it's an explicit user-defined relationship).

  For every file you change, write or update tests:
  - Vitest test: `_symCellBadges` returns a `badge-o` span when `row.src.p && row.is_orphan`
  - Vitest test: `_symCellBadges` returns no `badge-o` when `row.is_orphan` is false
  - Vitest test: `postSortRows` keeps child node immediately after parent when pair_group_key matches

- broker: skip
- doc: skip
- backend-test: skip (backend agent handles tests)
- playwright: skip (frontend agent handles Vitest)

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

---

## Commit message

feat(orders): O chip for orphan positions/orders, pair-button + modal, pair-group sort, hide RamboQuant Analytics from mobile footer

---

## Done when

- Position rows in Pulse grid show `O` badge when no open AlgoOrder tracks that (account, symbol)
- Dangling child orders in ChaseCard show `O` chip (parent not in active-chase list)
- "Pair" button in Pulse positions header → `OrderPairModal` opens with two order pickers
- "Pair" button in Derivatives legs headrow → same modal opens
- Submitting the modal calls `POST /api/orders/pair` and re-links child.parent_order_id to parent
- After pairing, the two positions share a `pair_group_key`; they sort as one unit (child always immediately after parent regardless of column sort)
- `O` chip disappears from ChaseCard row after successful pairing
- "RamboQuant Analytics" hidden on mobile in both algo + public layouts; still visible on desktop
- pytest passes, svelte-check 0 errors, vitest passes
