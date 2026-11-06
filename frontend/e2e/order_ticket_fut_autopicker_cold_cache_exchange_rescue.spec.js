/**
 * order_ticket_fut_autopicker_cold_cache_exchange_rescue.spec.js
 *
 * 2026-11-06 live operator report: "last night around 10:00 PM, after
 * placing the order I got market closed or mcx closed message." MCX
 * trades until 23:30 IST, so at 22:00 IST MCX is genuinely open —
 * this was a false positive, the third in a recurring bug class (2026-
 * 10-22 bare root, 2026-10-23 suffixed CE/PE option).
 *
 * Root cause: `OrderTicket.svelte`'s `_resolvedExchange` gated its
 * bare-root cold-cache rescue on `_hasContractSuffix`, which tests
 * `_underlying` (the caller's ORIGINAL `symbol` prop) — NOT `sym` (the
 * value actually looked up in that branch, `_resolvedSymbol ||
 * symbol`). The bare-underlying auto-picker (`_wantsFut`) resolves
 * `_resolvedSymbol` to a full contract ("CRUDEOIL26NOVFUT") from a
 * bare-root prop ("CRUDEOIL") — so `_hasContractSuffix` read FALSE
 * (the prop has no suffix) even though `sym` here was the fully-
 * suffixed contract. The branch then called `findNearestFuture(sym)`
 * with the FULL contract symbol; `listFutures()` keys its lookup on
 * `{ROOT}|FUT`, so a full-contract argument never matches, and the
 * rescue silently returned null on any cold-cache miss for that
 * specific contract — falling through to the caller's generic
 * exchange hint (often NFO/NSE), which reads "closed" once NSE's own
 * hours end while MCX is still open (15:30→23:30 IST) — exactly
 * "10:00 PM".
 *
 * Fix: always extract the leading-letters root from `sym` before
 * calling `findNearestFuture` — a no-op for a genuinely bare root, and
 * the correct argument for an already-resolved contract either way.
 * Collapses the old `if (!_hasContractSuffix) {...} else {...}` into
 * one path (the `else` branch's extraction, which was already correct).
 *
 * Source-grep regression guard — `npx playwright test` against the
 * live dev server for this exact file proved flaky in this environment
 * (confirmed via repeated identical runs returning different failure
 * sets with no code change in between — real-clock/live-market-state
 * dependent "live:" tests racing network/timing, not a regression from
 * this fix), so this spec pins the fix at the source level instead,
 * same precedent as chase_poll_kick_wiring.spec.js.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const ORDER_TICKET_PATH = path.resolve(DIR, '../src/lib/order/OrderTicket.svelte');
const INSTRUMENTS_PATH = path.resolve(DIR, '../src/lib/data/instruments.js');

test.describe('OrderTicket _resolvedExchange — bare-root rescue uses the TRUE root, not the resolved symbol', () => {
  test('_resolvedExchange no longer branches on _hasContractSuffix for the findNearestFuture rescue', () => {
    const src = readFileSync(ORDER_TICKET_PATH, 'utf8');
    const block = src.match(/const _resolvedExchange = \$derived\.by\(\(\) => \{[\s\S]*?\n {2}\}\);/)?.[0] ?? '';
    expect(block, 'expected to find _resolvedExchange').not.toBe('');
    // The old buggy shape gated findNearestFuture(sym) on !_hasContractSuffix,
    // which tests the wrong variable (_underlying, not sym) — must be gone.
    expect(block).not.toMatch(/if\s*\(!_hasContractSuffix\)\s*\{\s*\n\s*const fut = findNearestFuture\(sym\);/);
    // The fix: always extract the leading-letters root from `sym` first.
    expect(block).toMatch(/const _bareRoot = String\(sym\.match\(\/\^\(\[A-Z\]\+\)\/\)\?\.\[1\]\s*\|\|\s*sym\)/);
    expect(block).toMatch(/findNearestFuture\(_bareRoot\)/);
  });

  test('listFutures() keys its lookup on the bare underlying root — confirms why a full contract symbol must never be passed', () => {
    const src = readFileSync(INSTRUMENTS_PATH, 'utf8');
    const block = src.match(/export function listFutures\(underlying\)\s*\{[\s\S]*?\n\}/)?.[0] ?? '';
    expect(block, 'expected to find listFutures').not.toBe('');
    expect(block).toMatch(/\$\{underlying\.toUpperCase\(\)\}\|FUT/);
  });
});
