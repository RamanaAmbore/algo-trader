import { describe, it, expect } from 'vitest';
import { positionStateBadge, ORPHAN_GLYPH, ANCHOR_GLYPH } from '../../data/positionStateBadge.js';

describe('positionStateBadge — priority order (GTT > paired > orphan)', () => {
  it('returns gtt state when has_gtt is true, regardless of other flags', () => {
    const b = positionStateBadge({ has_gtt: true, pair_group_key: 'P1', is_orphan: true });
    expect(b.state).toBe('gtt');
    expect(b.glyph).toBe('GTT');
    expect(b.color).toBe('var(--algo-green)');
  });

  it('returns paired state when only pair_group_key is set', () => {
    const b = positionStateBadge({ pair_group_key: 'P2' });
    expect(b.state).toBe('paired');
    expect(b.glyph).toBe('P2');
  });

  it('returns paired state for a same-side "S" hedge group too, same color as "P"', () => {
    const p = positionStateBadge({ pair_group_key: 'P1' });
    const s = positionStateBadge({ pair_group_key: 'S1' });
    expect(s.state).toBe('paired');
    expect(s.glyph).toBe('S1');
    expect(s.color).toBe(p.color);
    expect(s.bg).toBe(p.bg);
  });

  it('returns orphan state when is_orphan is true and no gtt/pair key', () => {
    const b = positionStateBadge({ is_orphan: true });
    expect(b.state).toBe('orphan');
    expect(b.glyph).toBe(ORPHAN_GLYPH);
    expect(b.color).toBe('var(--algo-amber)');
  });

  it('returns null state (no badge) when no position-state flags are set', () => {
    const b = positionStateBadge({ is_orphan: false, pair_group_key: null, has_gtt: false });
    expect(b.state).toBeNull();
    expect(b.glyph).toBe('');
  });

  it('returns null state for _isTotal rows regardless of flags', () => {
    const b = positionStateBadge({ _isTotal: true, has_gtt: true });
    expect(b.state).toBeNull();
  });

  it('returns null state for null/undefined row', () => {
    expect(positionStateBadge(null).state).toBeNull();
    expect(positionStateBadge(undefined).state).toBeNull();
  });
});

describe('positionStateBadge — orphan glyph is not the letter O', () => {
  it('base glyph (stripped of the text-presentation selector) is not "○", "O", "o", or "0"', () => {
    const base = ORPHAN_GLYPH.replace(/︎$/, '');
    expect(['○', 'O', 'o', '0']).not.toContain(base);
  });

  it('base glyph has no emoji-presentation default', () => {
    const base = ORPHAN_GLYPH.replace(/︎$/, '');
    expect(/\p{Emoji_Presentation}/u.test(base)).toBe(false);
  });

  it('carries an explicit U+FE0E text-presentation selector as a defensive measure', () => {
    expect(ORPHAN_GLYPH.endsWith('︎')).toBe(true);
  });

  it('glyph is non-empty and renders as plain text (no surrogate-pair emoji construction)', () => {
    const base = ORPHAN_GLYPH.replace(/︎$/, '');
    expect(base.length).toBe(1); // single BMP code unit, not a surrogate pair
  });
});

describe('positionStateBadge — tooltip content', () => {
  it('paired tooltip mentions "auto-paired"', () => {
    const b = positionStateBadge({ pair_group_key: 'P1' });
    expect(b.tooltip.toLowerCase()).toContain('auto-paired');
  });

  it('same-side "S" group tooltip mentions "same-side hedge", NOT "auto-paired"', () => {
    const b = positionStateBadge({ pair_group_key: 'S1' });
    expect(b.tooltip.toLowerCase()).toContain('same-side hedge');
    expect(b.tooltip.toLowerCase()).not.toContain('auto-paired');
  });

  it('gtt tooltip mentions GTT', () => {
    const b = positionStateBadge({ has_gtt: true });
    expect(b.tooltip).toContain('GTT');
  });

  it('orphan tooltip mentions pairing', () => {
    const b = positionStateBadge({ is_orphan: true });
    expect(b.tooltip.toLowerCase()).toContain('pair');
  });
});

describe('positionStateBadge — underlying-anchor row ("X", 2026-10-18)', () => {
  it('returns anchor state with glyph "X" when _isUnderlyingAnchor is true', () => {
    const b = positionStateBadge({ _isUnderlyingAnchor: true });
    expect(b.state).toBe('anchor');
    expect(b.glyph).toBe('X');
    expect(b.glyph).toBe(ANCHOR_GLYPH);
  });

  it('uses a distinct muted/gray color, not any of GTT/paired/orphan colors', () => {
    const anchor = positionStateBadge({ _isUnderlyingAnchor: true });
    const gtt = positionStateBadge({ has_gtt: true });
    const paired = positionStateBadge({ pair_group_key: 'P1' });
    const orphan = positionStateBadge({ is_orphan: true });
    expect(anchor.color).toBe('var(--algo-muted)');
    expect(anchor.bg).toBe('var(--algo-muted-bg)');
    expect([gtt.color, paired.color, orphan.color]).not.toContain(anchor.color);
  });

  it('_isUnderlyingAnchor outranks has_gtt/pair_group_key/is_orphan if all were somehow set', () => {
    const b = positionStateBadge({
      _isUnderlyingAnchor: true, has_gtt: true, pair_group_key: 'P1', is_orphan: true,
    });
    expect(b.state).toBe('anchor');
  });

  it('a row with none of the state flags (including _isUnderlyingAnchor) returns null state', () => {
    const b = positionStateBadge({ _isUnderlyingAnchor: false });
    expect(b.state).toBeNull();
  });

  it('tooltip explains this is not a held position', () => {
    const b = positionStateBadge({ _isUnderlyingAnchor: true });
    expect(b.tooltip.toLowerCase()).toContain('not a held position');
  });
});
