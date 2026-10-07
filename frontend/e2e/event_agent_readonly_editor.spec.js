// Automation page: event agents (seeded by the alert pipeline) are read-only in the
// inline editor. Only Activate / Deactivate can change them.
// Source-level guard: reads the real page source.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('automation editor — event agents are read-only', () => {
  test('a notice explains the read-only state', () => {
    expect(src).toMatch(/\{#if agent\.kind === 'event'\}/);
    expect(src).toMatch(/System event agent — this agent is seeded by the alert pipeline/);
  });

  test('the form fields are disabled via one fieldset, not field by field', () => {
    expect(src).toMatch(/<fieldset disabled=\{agent\.kind === 'event'\} style="display:contents;">/);
    expect(src).toMatch(/<\/fieldset>/);
  });

  test('Save and Validate are disabled for event agents', () => {
    expect(src).toMatch(/onclick=\{saveEdit\} disabled=\{agent\.kind === 'event'\}/);
    expect(src).toMatch(/await runValidation\(\); \}\}\s*\n\s*disabled=\{agent\.kind === 'event'\}/);
  });
});
