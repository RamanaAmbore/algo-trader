// Phase 4 of the order/agent grammar unification — frontend CLI side.
//
// Proves orders.js genuinely reads order_fields.yaml (the shared catalog
// already single-sourced on the Python side since Phase 2) rather than
// relying solely on orders.yaml's hand-kept-in-sync literal `values:`
// lists, and that the resolution step runs BEFORE _wireTokens/_wireKwargs
// build their output — not a decorative import left unused.
//
// The byte-identical wired-output regression guard (the critical "CLI
// parsing/suggester/payload-building behavior must not change at all"
// requirement) lives in a Vitest spec instead of here, since it needs to
// actually execute orders.js (Vite's `?raw` + `$lib` alias resolution) —
// see frontend/src/lib/__tests__/orderGrammarFieldRefs.test.js.
// Source-level guard: reads the real module source.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/lib/command/grammars/orders.js', import.meta.url).pathname, 'utf8'
);

test.describe('orders.js — order_fields.yaml $ref resolution (Phase 4)', () => {
  test('imports order_fields.yaml via a second ?raw import, sibling to orders.yaml', () => {
    expect(src).toMatch(/import orderFieldsYamlText from '\.\/order_fields\.yaml\?raw';/);
    expect(src).toMatch(/const ORDER_FIELDS_DOC = .*yaml\.load\(orderFieldsYamlText\)/);
  });

  test('builds an ORDER_FIELDS catalog from the parsed doc\'s `fields` key', () => {
    expect(src).toMatch(/const ORDER_FIELDS = \(ORDER_FIELDS_DOC && ORDER_FIELDS_DOC\.fields\) \|\| \{\};/);
  });

  test('_resolveOrderFieldRef exists, passes through specs without $ref unchanged', () => {
    expect(src).toMatch(/function _resolveOrderFieldRef\(spec\)/);
    expect(src).toMatch(/if \(!spec \|\| typeof spec !== 'object' \|\| !spec\.\$ref\) return spec;/);
  });

  test('unknown $ref throws (mirrors the Python-side KeyError behavior)', () => {
    const idx = src.indexOf('function _resolveOrderFieldRef');
    const bodyEnd = src.indexOf('\n}', idx);
    const body = src.slice(idx, bodyEnd);
    expect(body).toMatch(/if \(!catalogEntry\) \{/);
    expect(body).toMatch(/throw new Error\(/);
  });

  test('enum divergence between local `values` and catalog throws a loud error — local order is never silently overwritten by catalog order', () => {
    const idx = src.indexOf('function _resolveOrderFieldRef');
    const bodyEnd = src.indexOf('\n}', idx);
    const body = src.slice(idx, bodyEnd);
    expect(body).toMatch(/_valuesSetEqual\(resolved\.values, catalogEntry\.enum\)/);
    expect(body).toMatch(/throw new Error\(/);
  });

  test('resolver call sites appear BEFORE _wireTokens(/_wireKwargs( in the wiring loop', () => {
    const loopIdx = src.indexOf('for (const [name, def] of Object.entries(GRAMMAR_DOC.verbs))');
    expect(loopIdx).toBeGreaterThan(-1);
    const loopBody = src.slice(loopIdx, src.indexOf('}', src.indexOf('};', loopIdx)));
    const resolveTokensIdx = loopBody.indexOf('_resolveTokenSpecs(def.tokens)');
    const wireTokensIdx = loopBody.indexOf('_wireTokens(');
    const resolveKwargsIdx = loopBody.indexOf('_resolveKwargSpecs(def.kwargs)');
    const wireKwargsIdx = loopBody.indexOf('_wireKwargs(');
    expect(resolveTokensIdx).toBeGreaterThan(-1);
    expect(wireTokensIdx).toBeGreaterThan(-1);
    expect(resolveKwargsIdx).toBeGreaterThan(-1);
    expect(wireKwargsIdx).toBeGreaterThan(-1);
    // Both resolver calls are nested INSIDE the _wireTokens(...)/_wireKwargs(...)
    // call expression itself (`_wireTokens(_resolveTokenSpecs(def.tokens))`),
    // so the resolver's start index comes after _wireTokens( opens but its
    // result is what's actually passed in — assert the nesting directly.
    expect(loopBody).toMatch(/_wireTokens\(_resolveTokenSpecs\(def\.tokens\)\)/);
    expect(loopBody).toMatch(/_wireKwargs\(_resolveKwargSpecs\(def\.kwargs\)\)/);
  });

  test('a spec with $ref but no local `values` is populated straight from catalog.enum', () => {
    const idx = src.indexOf('function _resolveOrderFieldRef');
    const bodyEnd = src.indexOf('\n}', idx);
    const body = src.slice(idx, bodyEnd);
    expect(body).toMatch(/resolved\.values = catalogEntry\.enum;/);
  });
});
