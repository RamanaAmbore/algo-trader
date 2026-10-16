import { describe, it, expect } from 'vitest';
import { positionStateBadge, ORPHAN_GLYPH, rawPinId } from '../../data/positionStateBadge.js';

describe('positionStateBadge — priority order (GTT > pinned > paired > orphan)', () => {
  it('returns gtt state when has_gtt is true, regardless of other flags', () => {
    const b = positionStateBadge({ has_gtt: true, pin_group_key: 'PIN1', pair_group_key: 'P1', is_orphan: true });
    expect(b.state).toBe('gtt');
    expect(b.glyph).toBe('GTT');
    expect(b.color).toBe('var(--algo-green)');
  });

  it('returns pinned state when pin_group_key is set and has_gtt is falsy', () => {
    const b = positionStateBadge({ has_gtt: false, pin_group_key: 'PIN1', pair_group_key: 'P1', is_orphan: false });
    expect(b.state).toBe('pinned');
    expect(b.glyph).toBe('PIN1');
  });

  it('pinned outranks auto-paired even when both keys are present', () => {
    const b = positionStateBadge({ pin_group_key: 'PIN9', pair_group_key: 'P3' });
    expect(b.state).toBe('pinned');
    expect(b.glyph).toBe('PIN9');
  });

  it('returns paired state when only pair_group_key is set', () => {
    const b = positionStateBadge({ pair_group_key: 'P2' });
    expect(b.state).toBe('paired');
    expect(b.glyph).toBe('P2');
  });

  it('returns orphan state when is_orphan is true and no gtt/pin/pair key', () => {
    const b = positionStateBadge({ is_orphan: true });
    expect(b.state).toBe('orphan');
    expect(b.glyph).toBe(ORPHAN_GLYPH);
    expect(b.color).toBe('var(--algo-amber)');
  });

  it('returns null state (no badge) when no position-state flags are set', () => {
    const b = positionStateBadge({ is_orphan: false, pair_group_key: null, pin_group_key: null, has_gtt: false });
    expect(b.state).toBeNull();
    expect(b.glyph).toBe('');
  });

  it('returns null state for _isTotal rows regardless of flags', () => {
    const b = positionStateBadge({ _isTotal: true, has_gtt: true, pin_group_key: 'PIN1' });
    expect(b.state).toBeNull();
  });

  it('returns null state for null/undefined row', () => {
    expect(positionStateBadge(null).state).toBeNull();
    expect(positionStateBadge(undefined).state).toBeNull();
  });
});

describe('positionStateBadge — visual distinctness (pinned vs auto-paired)', () => {
  it('pinned and paired use the same cyan text color but different bg/border (not confusable by state alone)', () => {
    const pinned = positionStateBadge({ pin_group_key: 'PIN1' });
    const paired = positionStateBadge({ pair_group_key: 'P1' });
    expect(pinned.color).toBe(paired.color); // same cyan family text
    expect(pinned.border).not.toBeNull();     // pinned carries an explicit border
    expect(paired.border).toBeNull();          // auto-paired does not
    expect(pinned.bg).not.toBe(paired.bg);     // distinct background tokens
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
  it('pinned tooltip mentions the pin group id and "unpin"', () => {
    const b = positionStateBadge({ pin_group_key: 'PIN7' });
    expect(b.tooltip).toContain('PIN7');
    expect(b.tooltip.toLowerCase()).toContain('unpin');
  });

  it('paired tooltip mentions "auto-paired"', () => {
    const b = positionStateBadge({ pair_group_key: 'P1' });
    expect(b.tooltip.toLowerCase()).toContain('auto-paired');
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

describe('rawPinId — recovers the DELETE route\'s bare row id from a "PIN{id}" display string', () => {
  it('strips the "PIN" prefix', () => {
    expect(rawPinId('PIN7')).toBe('7');
  });

  it('strips the "PIN" prefix regardless of digit count', () => {
    expect(rawPinId('PIN123')).toBe('123');
  });

  it('is a no-op (besides stringifying) when there is no "PIN" prefix', () => {
    expect(rawPinId('7')).toBe('7');
  });

  it('returns an empty string for null/undefined', () => {
    expect(rawPinId(null)).toBe('');
    expect(rawPinId(undefined)).toBe('');
  });

  it('round-trips the exact format _apply_pin_overlay() produces (backend/api/routes/positions.py)', () => {
    // f"PIN{row.id}" for some real position_pairs row id — confirms the
    // frontend's unpin call recovers the int the DELETE /pin/{pair_id:int}
    // route actually expects, not the formatted display string.
    const backendRowId = 42;
    const displayKey = `PIN${backendRowId}`;
    expect(rawPinId(displayKey)).toBe(String(backendRowId));
  });
});
