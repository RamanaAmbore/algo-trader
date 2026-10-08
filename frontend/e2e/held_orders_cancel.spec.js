// Held-orders UI gains a Cancel action alongside the existing Release action.
// Cancel abandons the held order outright (DB state change only, no broker
// call) and must be gated behind the shared ConfirmModal before firing —
// unlike Release, which has no per-row confirm. Both actions share the SAME
// `busy` record so either action disables the row correctly during the other.
// Source-level guard: reads the real component source.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const apiSrc = readFileSync(
  new URL('../src/lib/api.js', import.meta.url).pathname, 'utf8'
);
const cardSrc = readFileSync(
  new URL('../src/lib/HeldOrdersCard.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('held orders — cancel action', () => {
  test('api.js exposes cancelHeldOrder posting to the cancel route', () => {
    expect(apiSrc).toMatch(/export const cancelHeldOrder = \(orderId\) =>/);
    expect(apiSrc).toMatch(
      /_post\(`\/orders\/held\/\$\{Number\(orderId\)\}\/cancel`, \{\}, \{ auth: true \}\)/
    );
  });

  test('kindOf() labels agent_order held rows as Repeated rejection', () => {
    expect(cardSrc).toMatch(/h\.category === 'agent_order'/);
    expect(cardSrc).toMatch(/return 'Repeated rejection'/);
  });

  test('kindOf() still labels template_exit and falls back to Expiry close', () => {
    expect(cardSrc).toMatch(/h\.category === 'template_exit'/);
    expect(cardSrc).toMatch(/return 'Bracket exit'/);
    expect(cardSrc).toMatch(/return 'Expiry close'/);
  });

  test('Cancel button renders per row, next to Release', () => {
    expect(cardSrc).toMatch(/class="held-cancel"/);
    expect(cardSrc).toMatch(/onclick=\{\(\) => cancel\(row\)\}>Cancel</);
  });

  test('cancel() is gated behind confirmRef.ask() before calling cancelHeldOrder', () => {
    const fnMatch = cardSrc.match(/async function cancel\(row\) \{[\s\S]*?\n  \}/);
    expect(fnMatch).not.toBeNull();
    const fn = fnMatch[0];
    expect(fn).toMatch(/const ok = await confirmRef\?\.ask\(\{/);
    expect(fn).toMatch(/danger: true/);
    // Confirm/cancel labels must be unambiguous — the dialog is titled
    // "Cancel held order?" so a bare default "Cancel" button on the
    // confirm side would read backwards (misclick risk on a destructive
    // trading action).
    expect(fn).toMatch(/confirmLabel: 'Cancel order'/);
    expect(fn).toMatch(/cancelLabel: 'Keep held'/);
    expect(fn).toMatch(/if \(!ok\) return;/);
    expect(fn).toMatch(/await cancelHeldOrder\(row\.id\)/);
    // ask() must happen before the cancelHeldOrder call, not after.
    expect(fn.indexOf('confirmRef?.ask(')).toBeLessThan(fn.indexOf('cancelHeldOrder(row.id)'));
  });

  test('cancel() mirrors release()\'s try/catch/finally busy-tracking shape', () => {
    const fn = cardSrc.match(/async function cancel\(row\) \{[\s\S]*?\n  \}/)[0];
    expect(fn).toMatch(/busy = \{ \.\.\.busy, \[row\.id\]: true \};/);
    expect(fn).toMatch(/toast\?\.success\?\.\(`Cancelled \$\{row\.side\} \$\{row\.qty\} \$\{row\.symbol\}`\);/);
    expect(fn).toMatch(/toast\?\.error\?\.\(e\?\.message \|\| 'Cancel refused'\);/);
    expect(fn).toMatch(/busy = \{ \.\.\.busy, \[row\.id\]: false \};/);
  });

  test('Release and Cancel buttons share the single busy record, not a second variable', () => {
    // Only one busy-tracking $state declaration exists in the component.
    const busyDecls = cardSrc.match(/let busy = \$state/g) || [];
    expect(busyDecls.length).toBe(1);
    // Both buttons disable off that same `busy` record.
    expect(cardSrc).toMatch(/class="held-release"\s*\n\s*disabled=\{busy\[row\.id\]\}/);
    expect(cardSrc).toMatch(/class="held-cancel"\s*\n\s*disabled=\{busy\[row\.id\]\}/);
  });

  test('Cancel button is styled with the danger (--btn-sell) token family, not a new color', () => {
    // 2026-10 token cleanup: switched from the generic --c-short text
    // token to the --btn-sell-* button family (same red hue, the
    // dedicated button-color convention OrderCard's own buttons use —
    // see app.css's own "--btn-sell-*: red (SELL/short/negative/close)"
    // comment) — still the one danger color, not a new one.
    const rule = cardSrc.match(/\.held-cancel\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/border:\s*1px solid var\(--btn-sell-border\)/);
    expect(rule).toMatch(/color:\s*var\(--btn-sell\)/);
  });
});
