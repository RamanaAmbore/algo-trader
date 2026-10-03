/**
 * nativeSelectToCanonicalSelect.test.js
 *
 * Regression guard for the 2026-10 6-dimension-audit sweep that replaced
 * four native <select> elements with the canonical $lib/Select.svelte
 * component (the project convention already used by 12+ other files —
 * see e.g. admin/alerts/+page.svelte's `bind:value` + `onValueChange`
 * pattern, and admin/settings/+page.svelte's own pre-existing `onEdit`
 * one-way `value` + `onValueChange` pattern for server-backed rows):
 *
 *   - admin/+page.svelte:1260       — Investor-portal "event type" picker
 *   - admin/settings/+page.svelte:769 — Exchange-schedule "gate" picker
 *   - admin/brokers/+page.svelte:905  — Connection-log "account" filter
 *   - admin/brokers/+page.svelte:916  — Connection-log "event type" filter
 *
 * Why a source-scan, not a component mount: vitest.config.js has no Svelte
 * compiler plugin (same documented constraint as fillWatchLayoutWiring.test.js
 * and InfoHint.sourceAudit.test.js) — these route files can't be imported
 * and executed directly in this harness. A `?raw` string-scan of the
 * shipped file is the established convention here for locking down markup
 * that can't be exercised as a real mounted component.
 *
 * Five quality dimensions:
 *  1. SSOT   — reads the real shipped route files, not copies.
 *  2. Perf   — pure string/regex checks, no I/O, no mount cost.
 *  3. Stale  — regression-guards the exact defect class (a native
 *              <select> silently reappearing) by asserting its absence,
 *              not just the new component's presence.
 *  4. Reuse  — confirms each site imports $lib/Select.svelte (the
 *              canonical component), not a bespoke dropdown.
 *  5. UX     — confirms each control still wires to the same bound value
 *              / change-handler it had before, so picking an option still
 *              updates the right piece of state (bind:value for the two
 *              locally-bindable controls; the pre-existing
 *              value+onValueChange one-way pattern for the schedule-gate
 *              control, which intentionally funnels every pick through
 *              onScheduleGateChange rather than mutating scheduleForm.gate
 *              directly — see Select.svelte's own `onValueChange` doc
 *              comment for why that pattern exists).
 */

import { describe, it, expect } from 'vitest';
import adminSrc from '../../routes/(algo)/admin/+page.svelte?raw';
import settingsSrc from '../../routes/(algo)/admin/settings/+page.svelte?raw';
import brokersSrc from '../../routes/(algo)/admin/brokers/+page.svelte?raw';

describe('admin/+page.svelte — investor event-type picker uses Select', () => {
  it('imports the canonical Select component', () => {
    expect(adminSrc).toMatch(/import\s+Select\s+from\s+'\$lib\/Select\.svelte'/);
  });

  it('no native <select> remains in the file', () => {
    expect(adminSrc).not.toMatch(/<select[\s>]/);
  });

  it('the event-type control is a <Select> bound to evForm.event_type', () => {
    expect(adminSrc).toMatch(/<Select[^>]*bind:value=\{evForm\.event_type\}/s);
  });

  it('preserves all three original options (subscription / redemption / bootstrap)', () => {
    const block = adminSrc.slice(
      adminSrc.indexOf('bind:value={evForm.event_type}'),
      adminSrc.indexOf('bind:value={evForm.event_type}') + 400
    );
    expect(block).toMatch(/value:\s*'subscription'/);
    expect(block).toMatch(/value:\s*'redemption'/);
    expect(block).toMatch(/value:\s*'bootstrap'/);
  });
});

describe('admin/settings/+page.svelte — exchange-schedule gate picker uses Select', () => {
  it('imports the canonical Select component', () => {
    expect(settingsSrc).toMatch(/import\s+Select\s+from\s+'\$lib\/Select\.svelte'/);
  });

  it('no native <select> remains in the file', () => {
    expect(settingsSrc).not.toMatch(/<select[\s>]/);
  });

  it('the gate control is a <Select> whose value tracks scheduleForm.gate and whose pick ' +
     'still routes through onScheduleGateChange (not a direct bind, matching the original ' +
     'one-way value+onchange wiring)', () => {
    const idx = settingsSrc.indexOf('<Select id="sched-gate"');
    expect(idx).toBeGreaterThan(-1);
    const block = settingsSrc.slice(idx, idx + 320);
    expect(block).toMatch(/<Select/);
    expect(block).toMatch(/value=\{scheduleForm\.gate\}/);
    expect(block).toMatch(/onValueChange=\{\(v\) => onScheduleGateChange\(String\(v\)\)\}/);
  });

  it('onScheduleGateChange still derives the gate-scoped exchange list (unchanged behavior)', () => {
    expect(settingsSrc).toMatch(
      /function onScheduleGateChange\([^)]*\)\s*\{\s*if \(!scheduleForm\) return;\s*scheduleForm = \{\s*\.\.\.scheduleForm,\s*gate,/
    );
  });
});

describe('admin/brokers/+page.svelte — connection-log filter pickers use Select', () => {
  it('imports the canonical Select component', () => {
    expect(brokersSrc).toMatch(/import\s+Select\s+from\s+'\$lib\/Select\.svelte'/);
  });

  it('no native <select> remains in the file', () => {
    expect(brokersSrc).not.toMatch(/<select[\s>]/);
  });

  it('the account filter is a <Select> bound to connFilterAccount, still reloading on pick', () => {
    const idx = brokersSrc.indexOf('<Select id="conn-acct"');
    expect(idx).toBeGreaterThan(-1);
    const block = brokersSrc.slice(idx, idx + 320);
    expect(block).toMatch(/<Select/);
    expect(block).toMatch(/bind:value=\{connFilterAccount\}/);
    expect(block).toMatch(/onValueChange=\{loadConnEvents\}/);
  });

  it('the account options still include the "All accounts" sentinel plus every account', () => {
    const idx = brokersSrc.indexOf('<Select id="conn-acct"');
    const block = brokersSrc.slice(idx, idx + 420);
    expect(block).toMatch(/value:\s*'',\s*label:\s*'All accounts'/);
    expect(block).toMatch(/accounts\.map\(a => \(\{ value: a\.account, label: a\.account \}\)\)/);
  });

  it('the event-type filter is a <Select> bound to connFilterEventType, still reloading on pick', () => {
    const idx = brokersSrc.indexOf('<Select id="conn-evtype"');
    expect(idx).toBeGreaterThan(-1);
    const block = brokersSrc.slice(idx, idx + 320);
    expect(block).toMatch(/<Select/);
    expect(block).toMatch(/bind:value=\{connFilterEventType\}/);
    expect(block).toMatch(/onValueChange=\{loadConnEvents\}/);
  });

  it('the event-type filter still sources its options from CONN_EVENT_TYPES', () => {
    const idx = brokersSrc.indexOf('<Select id="conn-evtype"');
    const block = brokersSrc.slice(idx, idx + 220);
    expect(block).toMatch(/options=\{CONN_EVENT_TYPES\}/);
  });
});
