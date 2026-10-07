// Automation page: event agents (seeded by the alert pipeline) are field-level
// editable in the inline editor — renderer / condition / slug stay fixed,
// but channels (incl. per-channel priority + capability gate) are editable.
// Source-level guard: reads the real page source.
//
// Superseded design (kept here for history): an earlier revision disabled the
// ENTIRE form via one <fieldset disabled={agent.kind === 'event'}> wrapper.
// That blanket disable was relaxed to field-level per the automation-agents
// plan — event agents now branch into their own eventEditorBody snippet
// instead of sharing the threshold form at all, so there is no shared
// fieldset to disable in the first place.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('automation editor — event agents are field-level editable', () => {
  test('existing event agents branch into a dedicated editor body', () => {
    // The per-agent editor dispatches on kind instead of sharing one
    // fieldset-disabled form with threshold agents.
    // Read-side note: _agent_to_info() always returns the DB's real
    // vocabulary ('cycle' or 'event') — 'threshold' is a write-side-only
    // synonym normalised away before persistence — so the read-side
    // check is a direct equality against 'event', not an OR-fallback.
    expect(src).toMatch(/\{#if agent\.kind === 'event'\}\s*\n\s*\{@render eventEditorBody\(agent\)\}/);
    expect(src).toMatch(/\{@render thresholdEditorBody\(agent\)\}/);
  });

  test('a notice explains renderer/condition/slug are fixed, not the whole form', () => {
    expect(src).toMatch(/\{#snippet eventEditorBody\(/);
    expect(src).toMatch(/renderer, condition, and slug are fixed at creation/);
    expect(src).toMatch(/Channels, priority, and the capability gate stay editable below/);
  });

  test('slug and renderer render as disabled inputs; condition renders read-only', () => {
    expect(src).toMatch(/Slug <span class="opacity-50">\(fixed\)<\/span>/);
    expect(src).toMatch(/value=\{agent\.slug\} disabled/);
    expect(src).toMatch(/Renderer <span class="opacity-50">\(fixed\)<\/span>/);
    expect(src).toMatch(/\{@render renderCondNode\(agent\.conditions\)\}/);
  });

  test('channel checkboxes, priority, and gate stay editable (no disabled attr)', () => {
    // Inside eventEditorBody, the channel checkbox binds to the generic
    // toggleChannel() helper — the same one threshold agents use — with
    // no `disabled` guard (unlike the slug/renderer fields just above it,
    // which ARE intentionally disabled).
    const bodyStart = src.indexOf("{#snippet eventEditorBody(");
    const bodyEnd = src.indexOf('{/snippet}', bodyStart);
    const body = src.slice(bodyStart, bodyEnd);
    const channelGridStart = body.indexOf('Alert channels');
    const channelGrid = body.slice(channelGridStart);
    expect(channelGrid).toMatch(/onchange=\{\(e\) => toggleChannel\(ch\.id,/);
    expect(channelGrid).not.toMatch(/\bdisabled\b/);
    expect(channelGrid).toMatch(/setChannelPriority\(ch\.id,/);
    expect(channelGrid).toMatch(/setChannelGate\(ch\.id,/);
  });

  test('EVENT_CHANNELS is a distinct set from ALERT_CHANNELS', () => {
    expect(src).toMatch(/const EVENT_CHANNELS = \[/);
    expect(src).toMatch(/\{ id: 'ntfy',/);
    expect(src).toMatch(/\{ id: 'telegram_info',/);
    // Deliberate scoping comment explaining the two sets must not merge.
    expect(src).toMatch(/Deliberately separate from ALERT_CHANNELS/);
  });

  test('Save for an existing event agent uses the minimal events-only payload', () => {
    expect(src).toMatch(/async function saveEventEdit\(/);
    expect(src).toMatch(/await updateAgent\(editing, \{ events \}\);/);
  });
});
