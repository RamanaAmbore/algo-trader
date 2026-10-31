/**
 * orderCardAgentChip.test.js
 *
 * 2026-10-31 — operator: "order should have manual as agent, if triggered
 * manually or agent name if triggered by agent". New "Agent" chip on
 * OrderCard.svelte, reading `order.agent_origin` — a backend field
 * resolved via the same SSOT (`alert_utils.resolve_origin()`) every
 * alert's own "Agent: ..." origin line already uses, so this chip can
 * never disagree with a real alert about the same order.
 *
 * Deliberately a SEPARATE field/chip from the pre-existing "origin:" chip
 * (`order.source`), which means something unrelated (a cancel-source
 * label: mcp/operator/sim_stop) — this test asserts the two never
 * collide on the "agent" label.
 *
 * Follows orderCardOriginChip.test.js's own pattern: OrderCard.svelte has
 * no component-mount harness in this repo, so these are markup/regex
 * assertions against the raw source, same as that file.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT   — one chip, reuses the existing `.log-chip` family.
 *  2. Perf   — pure text/regex, no DOM, no network.
 *  3. Stale  — labeled "agent:", never "origin:" (the unrelated existing
 *              chip) or "source:".
 *  4. Reuse  — reuses the existing log-chip / log-chip-key classes.
 *  5. UX     — always renders (AlgoOrderInfo.agent_origin defaults to
 *              "manual", never null/empty), unlike the conditional
 *              "origin:" chip — every order shows an agent.
 */

import { describe, it, expect } from 'vitest';
import SRC from '../order/OrderCard.svelte?raw';

const markupStart = SRC.indexOf('</script>');
const markupEnd   = SRC.indexOf('<style>');
const markup = SRC.slice(markupStart, markupEnd);

describe('OrderCard.svelte — Agent chip (2026-10-31)', () => {
  it('renders an "Agent" chip gated on order.agent_origin', () => {
    expect(markup).toMatch(/\{#if order\.agent_origin\}/);
  });

  it('labels the chip "agent:" — distinct from the unrelated "origin:" (source) chip', () => {
    const idx = markup.indexOf('{#if order.agent_origin}');
    expect(idx).toBeGreaterThan(-1);
    const chipBlock = markup.slice(idx, idx + 200);
    expect(chipBlock).toMatch(/log-chip-key">agent:</);
    expect(chipBlock).not.toMatch(/log-chip-key">origin:</);
    expect(chipBlock).not.toMatch(/log-chip-key">source:</);
  });

  it('reuses the existing log-chip / log-chip-key classes (no new chip style)', () => {
    const idx = markup.indexOf('{#if order.agent_origin}');
    const chipBlock = markup.slice(idx, idx + 200);
    expect(chipBlock).toMatch(/class="log-chip"/);
  });

  it('is placed before the unrelated "origin:" (source) chip, after mode/engine', () => {
    const modeIdx   = markup.indexOf('{#if order.mode}');
    const engineIdx = markup.indexOf('{#if order.engine}');
    const agentIdx  = markup.indexOf('{#if order.agent_origin}');
    const originIdx = markup.indexOf('{#if order.source}');
    expect(modeIdx).toBeGreaterThan(-1);
    expect(engineIdx).toBeGreaterThan(modeIdx);
    expect(agentIdx).toBeGreaterThan(engineIdx);
    expect(originIdx).toBeGreaterThan(agentIdx);
  });
});
