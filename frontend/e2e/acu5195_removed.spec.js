// B6 (2026-09) — "ACU-5195" removed from every public page + JSON-LD.
//
// Operator-confirmed: not a required legal/LLPIN disclosure, safe to
// remove outright (no jurisdictional hold). Source-grep guard — no
// server/auth needed, so it runs everywhere including CI.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — reads the real route source files.
//   2. Perf    — pure fs read, no browser/server.
//   3. Stale   — this IS the stale-code guard.
//   4. Reuse   — mirrors contrast.spec.js's grep-guard pattern.
//   5. UX      — checks the credential line reads cleanly ("LLP", no
//                dangling "LLP ·" separator fragment) on every touched page.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const FILES = [
  '../src/routes/(public)/+page.svelte',
  '../src/routes/(public)/about/+page.svelte',
  '../src/routes/(public)/+layout.svelte',
];

for (const relPath of FILES) {
  test(`stale code — ${relPath} has no ACU-5195 occurrence`, () => {
    const src = readFileSync(new URL(relPath, import.meta.url).pathname, 'utf8');
    expect(src, `${relPath} must not contain "ACU-5195"`).not.toMatch(/ACU-5195/i);
    // No dangling separator fragment left behind (e.g. "LLP ·" with
    // nothing after it, or two adjacent "|" separators in the footer).
    expect(src, `${relPath} must not leave a dangling "LLP ·" fragment`).not.toMatch(/LLP\s*·\s*(<\/|$)/m);
  });
}

test('stale code — public footer has no adjacent duplicate separators', () => {
  const src = readFileSync(
    new URL('../src/routes/(public)/+layout.svelte', import.meta.url).pathname, 'utf8'
  );
  // Two <span class="pub-sep"> back-to-back (only whitespace between)
  // would indicate a leftover empty segment from the ACU-5195 removal.
  expect(src, 'no back-to-back pub-sep spans').not.toMatch(
    /<span class="pub-sep">\|<\/span>\s*<span class="pub-sep">\|<\/span>/
  );
});
