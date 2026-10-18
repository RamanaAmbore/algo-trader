import { describe, it, expect } from 'vitest';
import { chipHtml, chipsHtml, chipsAsText, chipsAsTextFromJson, chipsFromJson, parseJsonObject } from '$lib/logChips';

// ── parseJsonObject ──────────────────────────────────────────────────────
// Covers the 2026-10-18 LogPanel Agents/Alerts tab fix (CLAUDE.md item 9):
// AgentEventInfo.detail / AlertEvent.detail are JSON-ENCODED STRINGS
// (backend/api/routes/agents.py + alerts.py), not parsed objects — a bare
// `e.detail.level` read is always undefined. This is the shared helper
// that replaced that bug.

describe('parseJsonObject', () => {
  it('parses a realistic AgentEventInfo-shaped detail JSON string, exposing .level and .agent_kind', () => {
    const raw = JSON.stringify({ level: 'debug', account: 'ZG0790', qty: 5, agent_kind: 'cycle' });
    const parsed = parseJsonObject(raw);
    expect(parsed.level).toBe('debug');
    expect(parsed.account).toBe('ZG0790');
    expect(parsed.qty).toBe(5);
    expect(parsed.agent_kind).toBe('cycle');
  });

  it('returns {} for null/undefined/empty-string input', () => {
    expect(parseJsonObject(null)).toEqual({});
    expect(parseJsonObject(undefined)).toEqual({});
    expect(parseJsonObject('')).toEqual({});
  });

  it('returns {} for a non-JSON plain-sentence string (older rows, or a free-text detail)', () => {
    expect(parseJsonObject('preflight blocked')).toEqual({});
  });

  it('returns {} for truncated/malformed JSON (AlertEvent.detail is truncated to 240 chars server-side)', () => {
    const truncated = JSON.stringify({ account: 'ZG0790', qty: 5, reason: 'a very long reason string' }).slice(0, 20);
    expect(parseJsonObject(truncated)).toEqual({});
  });

  it('passes a real object straight through unchanged', () => {
    const obj = { level: 'info', account: 'DH3747' };
    expect(parseJsonObject(obj)).toBe(obj);
  });

  it('returns {} for a JSON array (not an object)', () => {
    expect(parseJsonObject('[1,2,3]')).toEqual({});
  });
});

// ── chipsHtml exclude option ─────────────────────────────────────────────

describe('chipsHtml — exclude option', () => {
  it('drops excluded keys regardless of value', () => {
    const html = chipsHtml({ account: 'ZG0790', agent_kind: 'cycle', qty: 5 }, { exclude: ['agent_kind'] });
    expect(html).toContain('account');
    expect(html).toContain('qty');
    expect(html).not.toContain('agent_kind');
  });

  it('with no exclude option, behaves exactly as before (backward compatible)', () => {
    const html = chipsHtml({ status: 'FILLED', chase: '#2' });
    expect(html).toBe(chipHtml('status', 'FILLED') + ' ' + chipHtml('chase', '#2'));
  });

  it('skips nested plain-object values (pre-existing guard — avoids [object Object])', () => {
    const html = chipsHtml({ account: 'ZG0790', legs: { a: 1 } });
    expect(html).toContain('account');
    expect(html).not.toContain('[object Object]');
  });
});

// ── chipsFromJson — Agents/Alerts tab detail rendering ───────────────────
// This is the exact call LogPanel.svelte now makes for both tabs
// (`chipsFromJson(e.detail, { exclude: ['agent_kind'] })`), replacing a
// raw-unparsed-JSON-blob render (Alerts) and a bare-event-name-with-no-
// context render (Agents, item 10/11).

describe('chipsFromJson — detail rendering with exclude', () => {
  const opts = { exclude: ['agent_kind'] };

  it('renders a real action detail payload as chips, excluding agent_kind', () => {
    const raw = JSON.stringify({ symbol: 'NIFTY26MAY22000CE', account: 'ZG0790', qty: 50, agent_kind: 'bracket' });
    const html = chipsFromJson(raw, opts);
    expect(html).toContain('symbol');
    expect(html).toContain('NIFTY26MAY22000CE');
    expect(html).toContain('account');
    expect(html).toContain('ZG0790');
    expect(html).not.toContain('agent_kind');
  });

  it('falls back to the escaped raw string on parse failure instead of disappearing (truncated detail)', () => {
    const truncated = '{"account":"ZG0790","qty":5,"reason":"a very long reason that gets cut';
    const html = chipsFromJson(truncated, opts);
    expect(html).not.toBe('');
    expect(html).toContain('ZG0790');
  });

  it('returns empty string for null/empty detail (schedule-only fires with no params)', () => {
    expect(chipsFromJson(null, opts)).toBe('');
    expect(chipsFromJson('', opts)).toBe('');
  });

  it('falls through unchanged for a plain-sentence (non-JSON) detail string', () => {
    const html = chipsFromJson('preflight blocked', opts);
    expect(html).toBe('preflight blocked');
  });
});

// ── chipsAsTextFromJson — untouched by this fix, locked in for regression ──

describe('chipsAsTextFromJson (unchanged sibling helper)', () => {
  it('still renders square-bracket text form', () => {
    expect(chipsAsTextFromJson(JSON.stringify({ a: 1, b: 2 }))).toBe('[a:1, b:2]');
  });
});

// ── chipsHtml — array-of-objects vs array-of-primitives (2026-10-18) ────
// Covers a real screenshot: a margin/funds-threshold agent's `matches`/
// `latched_matches` arrays (each element a per-account broker payload
// object) rendered as useless "[object Object],[object Object]..." junk.

describe('chipsHtml — array value rendering', () => {
  it('renders a non-empty array of plain objects as "N items", not [object Object]', () => {
    const html = chipsHtml({ matches: [{ account: 'ZG0790', value: 1 }, { account: 'DH3747', value: 2 }] });
    expect(html).toContain('2 items');
    expect(html).not.toContain('[object Object]');
  });

  it('still joins an array of primitives (strings/numbers) with commas', () => {
    const html = chipsHtml({ accounts: ['ZG0790', 'DH3747'], qtys: [1, 2, 3] });
    expect(html).toContain('ZG0790,DH3747');
    expect(html).toContain('1,2,3');
  });

  it('empty array does not render as "0 items" (falls through to join -> empty string)', () => {
    const html = chipsHtml({ matches: [] });
    expect(html).not.toContain('items');
  });
});

// ── chipsFromJson — oversized/malformed detail payload is capped ───────
// A real screenshot showed a multi-line wall of raw nested JSON (deeply
// nested per-account broker margin payloads) flooding one LogPanel row.
// Both parse-failure fallback branches must cap to ~200 chars + ellipsis.

describe('chipsFromJson — oversized/malformed payload is capped', () => {
  it('caps a non-object JSON string (array literal) fallback to ~200 chars + ellipsis', () => {
    const bigArray = JSON.stringify(Array.from({ length: 100 }, (_, i) => ({ account: `ACC${i}`, margin: i * 1000 })));
    expect(bigArray.length).toBeGreaterThan(200);
    const html = chipsFromJson(bigArray);
    expect(html).toContain('…');
    // escaped output should be close to the 200-char cap plus the ellipsis
    // (HTML entity expansion for quotes/braces may grow it somewhat, but it
    // must never approach the full multi-KB raw length)
    expect(html.length).toBeLessThan(bigArray.length);
  });

  it('caps a non-"{"-prefixed oversized string fallback (the `!trimmed.startsWith(\'{\')` branch)', () => {
    const bigSentence = 'preflight blocked: '.repeat(30);
    expect(bigSentence.length).toBeGreaterThan(200);
    const html = chipsFromJson(bigSentence);
    expect(html).toContain('…');
    expect(html.length).toBeLessThan(bigSentence.length);
  });

  it('caps a malformed/truncated JSON object string (the catch-block branch) to ~200 chars + ellipsis', () => {
    const bigObjPrefix = `{"account":"ZG0790","detail":"${'x'.repeat(250)}`; // no closing brace -> JSON.parse throws
    expect(bigObjPrefix.length).toBeGreaterThan(200);
    const html = chipsFromJson(bigObjPrefix);
    expect(html).toContain('…');
    expect(html.length).toBeLessThan(bigObjPrefix.length);
  });

  it('a normal small valid JSON object still renders full chips, unaffected by the cap (no false-positive capping)', () => {
    const raw = JSON.stringify({ symbol: 'NIFTY26MAY22000CE', account: 'ZG0790', qty: 50 });
    const html = chipsFromJson(raw);
    expect(html).not.toContain('…');
    expect(html).toContain('NIFTY26MAY22000CE');
    expect(html).toContain('ZG0790');
    expect(html).toContain('50');
  });
});
