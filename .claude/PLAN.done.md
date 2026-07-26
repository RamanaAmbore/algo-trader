# Plan: Update PULSE_SPEC.md for Five Frontend Behaviour Changes

## Task
Document five frontend behaviour changes shipped to the derivatives/order surfaces in 
`docs/specs/PULSE_SPEC.md`. Changes span OrderTicket header layout, ChaseCard status 
display, CandidateLegRow qty chip logic, cancel reconciliation, and OptionChainTab 
button positioning.

## Changes to Document

1. **OrderTicket header** (§4.0 new section) — Chase toggle + aggressiveness picker 
   (L/M/H) moved from order body to `CardHeader` middle zone. For LIMIT/SL: left 
   (symbol), middle (CHASE+agg pills), right (refresh+close). For MARKET: middle zone 
   empty. LTP now shown in body where chase was (live from depth WebSocket).

2. **ChaseCard status** (§4.5 or new subsection) — Active chase rows show: pulsing dot 
   (green=buy, red=sell) next to symbol name; age column visible in all modes (was 
   compact-only); countdown opacity increased. Existing fields unchanged.

3. **CandidateLegRow qty chip** (Derivatives Legs section) — New `pendingQty` prop from 
   `openOrderQtyBySymbol` store. Display: `isClosed` → `closed` chip (muted); 
   `pendingQty > 0` → `{pendingQty} [open chip]` + remaining qty; else plain qty. Store 
   is `src/lib/data/openOrdersStore.svelte.js` (symbol→pending qty map), co-polled by 
   layout alongside `pollChase`.

4. **Cancel reconciliation** (Derivatives page order_update handler) — CANCELLED/REJECTED 
   statuses now call `loadPositions({ fresh: true })` immediately in terminal branch 
   (previously returned without refreshing, leaving stale positions until next poll).

5. **OptionChainTab +/- buttons** (§19 or Chain subsection) — Calls side (CE): buttons 
   cluster at right edge (adjacent to strike), quotes at outer left. Puts side (PE): 
   buttons cluster at left edge (adjacent to strike), quotes at outer right. Uses 
   `justify-content: flex-end` / `flex-start` per side.

## Agents
- doc: Read existing sections, append/update concisely to Order Entry, Chase, 
  Derivatives Legs/CandidateRow, and Chain sections; preserve all existing content

## Tests
- none (spec-only update)

## Commit message
docs(pulse): document OrderTicket header, ChaseCard, CandidateLegRow, cancel 
reconciliation, OptionChain button layouts

## Done when
- All five behaviour changes documented in PULSE_SPEC.md
- Existing content unchanged
- Cross-references to sections/subsections added where helpful
