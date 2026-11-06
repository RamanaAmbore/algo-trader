/**
 * legs_grid_closed_fo_qty_lots.spec.js
 *
 * 2026-11-06 live operator report: "lots and qty for close positions in
 * derivatives legs grid is zero. can you keep the correct value."
 *
 * Root cause: Kite zeroes a position's `quantity` once net qty goes flat
 * (documented broker behaviour — correct, `isClosed` still derives from
 * it). The Legs grid's Lots cell already read a backend-supplied
 * `c.lots` field (expiryPnl.js), but `PositionRow` (backend/api/
 * schemas.py) never exposed one for the live-fetch path, so it was
 * always `null` and `lotsForRow()` fell back to `qty / lot_size` = 0.
 * The Qty cell had an equivalent carve-out for sold-today EQ rows
 * (`_eqDisplayQty`, reads `c.opening_qty`) but no F&O equivalent, so a
 * closed CE/PE/FUT row's Qty cell always showed the bare (zeroed)
 * `c.qty`.
 *
 * Fix: backend now derives the real entry lot count for a flat F&O row
 * from its own whole-life buy_quantity/sell_quantity fields (same
 * `pnl_math.closed_position_entry_lots` already used by the daily_book
 * snapshot path) and exposes it as `PositionRow.lots`. The frontend's
 * `_foDisplayQty` derived (CandidateLegRow.svelte) multiplies that lot
 * count back out by the instrument's own lot size to show the real
 * entry quantity, mirroring the pre-existing eq-row pattern.
 *
 * Source-grep regression guard, same precedent as
 * chase_poll_kick_wiring.spec.js — reproducing a genuinely CLOSED F&O
 * position with known whole-life buy/sell fields against a live broker
 * sandbox is impractical to automate deterministically in CI.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const ROW_PATH = path.resolve(DIR, '../src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte');
const EXPIRY_PNL_PATH = path.resolve(DIR, '../src/lib/data/expiryPnl.js');
const SCHEMAS_PATH = path.resolve(DIR, '../../backend/api/schemas.py');
const POSITIONS_PATH = path.resolve(DIR, '../../backend/api/routes/positions.py');

test.describe('closed F&O row shows real entry Qty/Lots, not zero', () => {
  test('backend PositionRow exposes a `lots` field for the live-fetch path', () => {
    const src = readFileSync(SCHEMAS_PATH, 'utf8');
    expect(src).toMatch(/lots:\s*Optional\[int\]\s*=\s*None/);
  });

  test('positions.py restores lots for a flat (quantity==0) row from whole-life buy/sell fields', () => {
    const src = readFileSync(POSITIONS_PATH, 'utf8');
    expect(src).toMatch(/def _restore_closed_position_lots\(/);
    expect(src).toMatch(/closed_position_entry_lots/);
    // Must be called in the live-fetch path, alongside its avg_cost sibling.
    expect(src).toMatch(/_restore_closed_position_avg_cost\(raw\)\s*\n\s*_restore_closed_position_lots\(raw\)/);
  });

  test('expiryPnl.js already threads the backend lots field through (no change needed)', () => {
    const src = readFileSync(EXPIRY_PNL_PATH, 'utf8');
    expect(src).toMatch(/lots:\s*p\?\.lots\s*!=\s*null\s*\?\s*Number\(p\.lots\)\s*:\s*null/);
  });

  test('CandidateLegRow.svelte derives a non-eq closed-position display qty from lots × instrument lot size', () => {
    const src = readFileSync(ROW_PATH, 'utf8');
    const block = src.match(/const _foDisplayQty = \$derived\([\s\S]*?\n\s*\);/)?.[0] ?? '';
    expect(block, 'expected a _foDisplayQty derived').not.toBe('');
    // Must exclude eq kind (that case is handled by the pre-existing _eqDisplayQty).
    expect(block).toMatch(/c\.kind !== 'eq'/);
    // Must gate on the row actually being flat (qty === 0) and lots present.
    expect(block).toMatch(/Number\(c\.qty \|\| 0\) === 0/);
    expect(block).toMatch(/c\.lots != null/);
    // Must multiply lots back out by the instrument's lot size, not divide.
    expect(block).toMatch(/Number\(c\.lots\)\s*\*/);
    expect(block).toMatch(/getInstrument/);

    // displayQty must prefer the two carve-outs over the bare (zeroed) qty.
    const displayQtyBlock = src.match(/const displayQty = \$derived\([\s\S]*?\n\s*\);/)?.[0] ?? '';
    expect(displayQtyBlock).toMatch(/_eqDisplayQty\s*\?\?\s*_foDisplayQty\s*\?\?\s*Number\(c\.qty \|\| 0\)/);
  });

  test('the Close action stays gated on isClosed (c.qty), never on the display-only qty', () => {
    const src = readFileSync(ROW_PATH, 'utf8');
    // isClosed must read the real c.qty, not displayQty — otherwise a
    // closed row whose display qty was just restored to a nonzero value
    // would wrongly re-enable the Close action on an already-flat position.
    expect(src).toMatch(/const isClosed\s*=\s*\$derived\(Number\(c\.qty \|\| 0\) === 0\)/);
    expect(src).toMatch(/const isClosable\s*=\s*\$derived\(!isClosed/);
  });
});
