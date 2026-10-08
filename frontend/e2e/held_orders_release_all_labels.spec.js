// "Release all" confirm dialog must never read "Delete"/"Cancel" — that
// labeling is ConfirmModal's generic default for `danger: true` with no
// override, and reads backwards here: releasing a held order sends it to
// the broker (non-destructive), so a button labeled "Delete" risks an
// operator avoiding the correct action, or clicking it expecting it to
// discard the orders instead. Fix: explicit confirmLabel/cancelLabel on
// this ONE call site, matching the existing per-row cancel()'s own
// unambiguous-labeling convention in the same file.
// Source-level guard: reads the real component source, same pattern as
// held_orders_cancel.spec.js.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const cardSrc = readFileSync(
  new URL('../src/lib/HeldOrdersCard.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('held orders — release all confirm labels', () => {
  test('releaseAll() passes explicit confirmLabel/cancelLabel, not the danger default', () => {
    const fnMatch = cardSrc.match(/async function releaseAll\(\) \{[\s\S]*?\n  \}/);
    expect(fnMatch).not.toBeNull();
    const fn = fnMatch[0];
    expect(fn).toMatch(/const ok = await confirmRef\?\.ask\(\{/);
    expect(fn).toMatch(/danger: true/);
    // Must NOT rely on ConfirmModal's bare `danger: true` default, which
    // resolves to 'Delete'/'Cancel' — wrong for a non-destructive release.
    expect(fn).toMatch(/confirmLabel: 'Release all'/);
    expect(fn).toMatch(/cancelLabel: 'Keep held'/);
    expect(fn).not.toMatch(/confirmLabel: 'Delete'/);
  });

  test('releaseAll() still gates the broker-release loop behind the confirm result', () => {
    const fn = cardSrc.match(/async function releaseAll\(\) \{[\s\S]*?\n  \}/)[0];
    expect(fn).toMatch(/if \(!ok\) return;/);
    expect(fn.indexOf('confirmRef?.ask(')).toBeLessThan(fn.indexOf('if (!ok) return;'));
    expect(fn.indexOf('if (!ok) return;')).toBeLessThan(fn.indexOf('releaseHeldOrder(row.id)'));
  });

  test("ConfirmModal's own generic default is untouched (other callers may rely on 'Delete')", () => {
    const confirmModalSrc = readFileSync(
      new URL('../src/lib/ConfirmModal.svelte', import.meta.url).pathname, 'utf8'
    );
    expect(confirmModalSrc).toMatch(/_confirmLabel\s*=\s*opts\.confirmLabel\s*\?\?\s*\(opts\.danger \? 'Delete' : 'Confirm'\)/);
  });
});
