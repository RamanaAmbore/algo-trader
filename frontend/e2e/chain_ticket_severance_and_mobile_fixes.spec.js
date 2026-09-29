/**
 * chain_ticket_severance_and_mobile_fixes.spec.js
 *
 * Source-scan guards for the 2026-09-29 batch of fixes:
 *
 *  1. Ticket/Chain template architectural severance — the operator's
 *     final, repeatedly-confirmed decision ("make sure order ticket is
 *     not wired to template"): the Ticket tab places/closes positions
 *     with NO template attach at all; templates only ever apply to the
 *     Chain tab's basket legs.
 *  2. Mobile chain-area height cap — operator reported the Templ row
 *     was invisible on mobile because .chain-grid-wrap's unbounded
 *     flex-grow starved every sibling below it (including the Templ
 *     row) of any box height. Fixed with a mobile max-height cap.
 *  3. Desktop chain row gap — removing the row border-bottom (an
 *     earlier declutter fix) left rows with zero vertical padding on
 *     desktop, reading as "very close". Fixed with restored padding.
 *  4. Submit button labels — lot size dropped from the Ticket-tab
 *     label; the Chain-tab basket label is now plain "Submit" instead
 *     of "Submit (N)".
 *  5. Chase indicator right-alignment — CHASE was only pushed to the
 *     right edge via the LTP pill's margin-left:auto; when LTP doesn't
 *     render (common on Chain tab, no single-symbol LTP while staging
 *     a multi-leg basket) CHASE lost its right anchor. Fixed with its
 *     own margin-left:auto.
 *  6. Chain +/- buttons — :active pressed-state styling so a tap/click
 *     reads as tactile feedback, not just a hover restatement.
 *
 * All source-scan (no browser) — mirrors this session's established
 * pattern for guarding CSS/markup decisions that are cheap to verify
 * from source and expensive/flaky to verify live on every push.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const dir = path.resolve(import.meta.dirname ?? new URL('.', import.meta.url).pathname, '..');
const SYMBOL_PANEL = readFileSync(path.join(dir, 'src/lib/SymbolPanel.svelte'), 'utf8');
const ORDER_TICKET = readFileSync(path.join(dir, 'src/lib/order/OrderTicket.svelte'), 'utf8');
const CHAIN_TAB = readFileSync(path.join(dir, 'src/lib/order/OptionChainTab.svelte'), 'utf8');
const SUBMIT_HELPERS = readFileSync(path.join(dir, 'src/lib/order/orderTicketSubmit.js'), 'utf8');

test.describe('Ticket/Chain template severance', () => {
  test('TemplateBar row only renders for the Chain tab, never Ticket', () => {
    // Both branches that render the shell-level Templ row (live + demo)
    // must gate on _activeTab === 'chain', not the old '!== chart'.
    const demoBranch = SYMBOL_PANEL.match(/\{#if _activeTab === 'chain' && _isDemo[\s\S]{0,120}/)?.[0] ?? '';
    const liveBranch = SYMBOL_PANEL.match(/\{:else if _activeTab === 'chain' && _templates\.length > 0[\s\S]{0,120}/)?.[0] ?? '';
    expect(demoBranch, 'demo Templ-row branch must gate on chain tab').toContain("_activeTab === 'chain'");
    expect(liveBranch, 'live Templ-row branch must gate on chain tab').toContain("_activeTab === 'chain'");
    expect(SYMBOL_PANEL).not.toMatch(/_activeTab !== 'chart'[\s\S]{0,40}_isDemo/);
  });

  test('OrderTicket mount no longer auto-selects a template', () => {
    const rawMountFn = ORDER_TICKET.match(/onMount\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(rawMountFn, 'onMount body').not.toBe('');
    // Strip comment-only lines — the removal is explained in a code
    // comment that itself mentions the old call names in prose.
    const mountFn = rawMountFn
      .split('\n')
      .filter((line) => !/^\s*(\/\/|\*)/.test(line))
      .join('\n');
    expect(mountFn, 'onMount executable body must not call _autoSelectTemplate()')
      .not.toMatch(/_autoSelectTemplate\(\)/);
    expect(mountFn, 'onMount executable body must not call loadOrderTemplates()')
      .not.toMatch(/loadOrderTemplates\(\)/);
  });

  test('OrderTicket submit payload always sends template_id: null (not just for close orders)', () => {
    expect(ORDER_TICKET).toMatch(/templateId:\s*null,/);
    // The old conditional form (`_isCloseOrder ? null : templateId`) must be gone.
    expect(ORDER_TICKET).not.toMatch(/templateId:\s*_isCloseOrder\s*\?\s*null\s*:\s*templateId/);
  });

  test('side-aware Default resolution survives the severance — still wired into the Chain-only TemplateBar', () => {
    // Replaces the old e2e/template_default_pill.spec.js (removed):
    // that spec drove the 4-scope Default-pill matrix through the
    // Ticket tab's BUY/SELL toggle + the old pill UI, both gone now
    // (Ticket has no template row; the pill toggle became a dropdown
    // in an earlier commit this session). The _sideAwareDefault
    // resolver itself is untouched by the severance — only where it
    // renders changed (Chain tab only) — so this is a structural
    // continuity check, not a behavioural regression test.
    expect(SYMBOL_PANEL).toMatch(/const _sideAwareDefault = \$derived\.by/);
    expect(SYMBOL_PANEL).toContain('sideAwareDefault={_sideAwareDefault}');
  });

  test('Chain tab still owns the shared templateId binding (severance is Ticket-only)', () => {
    expect(SYMBOL_PANEL).toContain('bind:templateId={_sharedTemplateId}');
    // Only one bind:templateId consumer should exist in the shell markup
    // (OptionChainTab) — OrderTicket's mount no longer receives it.
    const bindCount = (SYMBOL_PANEL.match(/bind:templateId=/g) || []).length;
    expect(bindCount, 'exactly one bind:templateId in SymbolPanel (Chain only)').toBe(1);
  });
});

test.describe('Mobile chain height cap + desktop row gap', () => {
  test('chain-grid-wrap has a mobile max-height so siblings below it (Templ row) get box height', () => {
    const mobileBlock = CHAIN_TAB.match(/@media \(max-width: 760px\) \{\s*\.chain-grid-wrap \{[\s\S]{0,80}/)?.[0] ?? '';
    expect(mobileBlock, 'mobile .chain-grid-wrap max-height rule').not.toBe('');
    expect(mobileBlock).toMatch(/max-height:\s*16rem/);
  });

  test('desktop chain rows have restored vertical padding after border-bottom removal', () => {
    const desktopBlock = CHAIN_TAB.match(/@media \(min-width: 640px\) \{\s*\.chain-row > td \{[\s\S]{0,900}?\n    \}/)?.[0] ?? '';
    expect(desktopBlock, 'desktop .chain-row > td override block').not.toBe('');
    expect(desktopBlock).toMatch(/border-bottom:\s*none/);
    expect(desktopBlock).toMatch(/padding-top:\s*0\.3rem/);
    expect(desktopBlock).toMatch(/padding-bottom:\s*0\.3rem/);
  });
});

test.describe('Submit button labels', () => {
  test('formatSubmitLabel drops lot size from Ticket-tab labels', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).not.toBe('');
    expect(fn, 'no qty/qtySuffix should be interpolated into the label anymore')
      .not.toMatch(/qtySuffix/);
  });

  test('formatSubmitLabel returns plain "Submit" for basket mode (Chain tab), not "Submit (N)"', () => {
    const fn = SUBMIT_HELPERS.match(/export function formatSubmitLabel[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fn).toMatch(/if \(ctx\.basketCount > 0\) return 'Submit';/);
  });

  test('SymbolPanel common-action submit button also renders plain "Submit" for basket mode', () => {
    expect(SYMBOL_PANEL).not.toMatch(/Submit \(\$\{basketLegs\.length\}\)/);
    expect(SYMBOL_PANEL).not.toMatch(/`Submit \(\$\{basketLegs\.length\}\)`/);
  });
});

test.describe('Chase indicator right-alignment', () => {
  test('CHASE label owns its own margin-left:auto, independent of the LTP pill', () => {
    const rule = SYMBOL_PANEL.match(/\.oes-common-chase-label \{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.oes-common-chase-label rule').not.toBe('');
    expect(rule).toMatch(/margin-left:\s*auto/);
  });
});

test.describe('Chain +/- pressed-state feedback', () => {
  test('chain-btn-buy/-sell have :active rules distinct from :hover', () => {
    expect(CHAIN_TAB).toMatch(/\.chain-btn-buy:active\s*\{/);
    expect(CHAIN_TAB).toMatch(/\.chain-btn-sell:active\s*\{/);
    const buyActive = CHAIN_TAB.match(/\.chain-btn-buy:active\s*\{[\s\S]*?\}/)?.[0] ?? '';
    expect(buyActive, 'pressed state should visually differ from rest/hover (border/box-shadow highlight)')
      .toMatch(/box-shadow|border-color/);
  });
});
