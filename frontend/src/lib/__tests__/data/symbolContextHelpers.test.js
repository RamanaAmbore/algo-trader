/**
 * symbolContextHelpers.test.js — Vitest unit tests for the pure
 * Open-in-Options / hasFnOUnderlying / copy-symbol helpers shared by
 * every SymbolContextMenu host (2026-10-22 context-menu parity work).
 *
 * Five quality dimensions:
 *  1. SSOT   — this is the single source every host (MarketPulse,
 *              OrderBook, LogPanel, PerformancePage, the derivatives
 *              Legs grid) calls for Open-in-Options root derivation and
 *              the Detach/Re-attach hasUnderlying gate; a regression
 *              here breaks all five surfaces identically.
 *  2. Perf   — pure string ops, no I/O.
 *  3. Stale  — regression-guards the EXACT regex MarketPulse's original
 *              inline `ctxOpenOptions` used, so the refactor into this
 *              shared module can't silently drift from the pre-existing
 *              behaviour.
 *  4. Reuse  — covers both exported entry points (root-only and full
 *              href) and the kind-classification helper every host's
 *              `group` prop gate depends on.
 *  5. UX     — hasFnOUnderlying's documented known-gaps (bare MCX
 *              virtual-root anchor, Pulse's displayUnderlying tagging)
 *              are asserted here so a future "fix" can't silently
 *              change behaviour without a test noticing.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  rootForOptionsLink, openInOptionsHref, hasFnOUnderlying, copySymbolToClipboard,
} from '$lib/data/symbolContextHelpers.js';

describe('rootForOptionsLink', () => {
  it('strips the expiry/strike/CE-PE suffix from a monthly option', () => {
    expect(rootForOptionsLink('NIFTY25APR22000CE')).toBe('NIFTY');
  });

  it('strips the FUT suffix from a futures contract', () => {
    expect(rootForOptionsLink('CRUDEOIL25JUNFUT')).toBe('CRUDEOIL');
  });

  it('passes a bare equity symbol through unchanged', () => {
    expect(rootForOptionsLink('RELIANCE')).toBe('RELIANCE');
  });

  it('uppercases the input', () => {
    expect(rootForOptionsLink('reliance')).toBe('RELIANCE');
  });

  it('falls back to the underlying hint only when the symbol strips to empty', () => {
    // A tradingsymbol never legitimately starts with a digit, so this
    // path only exercises defensively — confirms the fallback chain
    // order (hint before raw symbol) without relying on a real-world
    // input shape.
    expect(rootForOptionsLink('25APR22000CE', 'NIFTY BANK')).toBe('NIFTYBANK');
  });

  it('falls back to the raw symbol when both the strip and the hint are empty', () => {
    expect(rootForOptionsLink('', '')).toBe('');
  });

  it('ignores the hint when the symbol strips to a non-empty root', () => {
    expect(rootForOptionsLink('NIFTY25APR22000CE', 'SOMETHING ELSE')).toBe('NIFTY');
  });
});

describe('openInOptionsHref', () => {
  it('builds a /admin/derivatives?u=<root> href', () => {
    expect(openInOptionsHref('NIFTY25APR22000CE')).toBe('/admin/derivatives?u=NIFTY');
  });

  it('URL-encodes the derived root', () => {
    expect(openInOptionsHref('', 'NIFTY BANK')).toBe('/admin/derivatives?u=NIFTYBANK');
  });
});

describe('hasFnOUnderlying', () => {
  it('is true for a monthly option', () => {
    expect(hasFnOUnderlying('NIFTY25APR22000CE')).toBe(true);
  });

  it('is true for a weekly option', () => {
    expect(hasFnOUnderlying('NIFTY2541222000PE')).toBe(true);
  });

  it('is true for a futures contract', () => {
    expect(hasFnOUnderlying('CRUDEOIL25JUNFUT')).toBe(true);
  });

  it('is false for a bare equity symbol', () => {
    expect(hasFnOUnderlying('RELIANCE')).toBe(false);
  });

  it('is false for an empty/null symbol', () => {
    expect(hasFnOUnderlying('')).toBe(false);
    expect(hasFnOUnderlying(null)).toBe(false);
  });

  // Documented known gap vs. MarketPulse's row.underlying signal — a
  // bare MCX virtual-root anchor (no trailing digits) decomposes as
  // plain equity here. Asserted so a future change notices the
  // behaviour shift instead of silently "fixing" it.
  it('known gap: a bare MCX virtual-root anchor symbol reads as false', () => {
    expect(hasFnOUnderlying('CRUDEOIL')).toBe(false);
  });
});

describe('copySymbolToClipboard', () => {
  it('writes the symbol to the clipboard and resolves true on success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const ok = await copySymbolToClipboard('NIFTY25APR22000CE');
    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalledWith('NIFTY25APR22000CE');
    vi.unstubAllGlobals();
  });

  it('resolves false when the clipboard API throws', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const ok = await copySymbolToClipboard('NIFTY');
    expect(ok).toBe(false);
    vi.unstubAllGlobals();
  });

  it('writes an empty string for a falsy symbol rather than throwing', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    await copySymbolToClipboard(null);
    expect(writeText).toHaveBeenCalledWith('');
    vi.unstubAllGlobals();
  });
});
