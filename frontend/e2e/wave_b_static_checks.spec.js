import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Static file content checks for Wave B frontend changes.
// These tests verify code-level changes without needing a running server.

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readFile = (relPath) => {
  const abs = path.resolve(__dirname, '..', relPath);
  return readFileSync(abs, 'utf-8');
};

test.describe('Wave B static file checks', () => {
  test('algo error banner palette - agents page uses new reduced-opacity red', () => {
    const content = readFile('src/routes/(algo)/agents/+page.svelte');
    expect(content).toContain('bg-red-500/15 text-red-300 text-xs border border-red-500/40');
    expect(content).not.toContain('bg-red-50 text-red-700 border-red-200');
  });

  test('algo error banner palette - settings page uses new reduced-opacity red', () => {
    const content = readFile('src/routes/(algo)/admin/settings/+page.svelte');
    expect(content).toContain('bg-red-500/15 text-red-300');
    expect(content).not.toContain('bg-red-50 text-red-700 border-red-200');
  });

  test('algo error banner palette - simulator page uses new reduced-opacity red', () => {
    const content = readFile('src/routes/(algo)/admin/simulator/+page.svelte');
    expect(content).toContain('bg-red-500/15');
    expect(content).not.toContain('bg-red-50 text-red-700 border-red-200');
  });

  test('simulator page imports and uses branchLabel helper', () => {
    const content = readFile('src/routes/(algo)/admin/simulator/+page.svelte');
    expect(content).toContain("import { clientTimestamp, visibleInterval, branchLabel }");
    expect(content).toContain('branchLabel(');
  });

  test('api.js exports new helper fetchAdminLogs', () => {
    const content = readFile('src/lib/api.js');
    expect(content).toContain('export const fetchAdminLogs');
  });

  test('api.js exports new helper submitContact', () => {
    const content = readFile('src/lib/api.js');
    expect(content).toContain('export const submitContact');
  });

  test('console page does NOT use raw fetch for /api/agents/interpret', () => {
    const content = readFile('src/routes/(algo)/console/+page.svelte');
    expect(content).not.toContain("fetch('/api/agents/interpret");
  });

  test('orders page does NOT use raw fetch for /api/agents/events/recent', () => {
    const content = readFile('src/routes/(algo)/orders/+page.svelte');
    expect(content).not.toContain("fetch('/api/agents/events/recent");
  });

  test('agents page does NOT use raw fetch for /api/admin/logs', () => {
    const content = readFile('src/routes/(algo)/agents/+page.svelte');
    expect(content).not.toContain("fetch('/api/admin/logs");
  });

  test('simulator page does NOT use raw fetch for /api/admin/logs', () => {
    const content = readFile('src/routes/(algo)/admin/simulator/+page.svelte');
    expect(content).not.toContain("fetch('/api/admin/logs");
  });

  test('contact page does NOT use raw fetch for /api/contact/', () => {
    const content = readFile('src/routes/(public)/contact/+page.svelte');
    expect(content).not.toContain("fetch('/api/contact/");
  });

  test('OptionsPayoff σ-axis uses amber for whole-sigma labels', () => {
    const content = readFile('src/lib/OptionsPayoff.svelte');
    expect(content).toContain("fill={wholeSigma ? '#fbbf24' : '#c8d8f0'}");
  });

  test('OptionsPayoff σ-axis uses correct font-size and font-weight', () => {
    const content = readFile('src/lib/OptionsPayoff.svelte');
    expect(content).toContain("font-size={wholeSigma ? 11 : 10}");
    expect(content).toContain("font-weight={wholeSigma ? 700 : 500}");
  });

  test('agents page does NOT contain long "Run-in-Simulator failed:" string', () => {
    const content = readFile('src/routes/(algo)/agents/+page.svelte');
    expect(content).not.toContain('Run-in-Simulator failed: ');
  });

  test('settings page does NOT contain template string "Save failed: ${" pattern', () => {
    const content = readFile('src/routes/(algo)/admin/settings/+page.svelte');
    expect(content).not.toContain('Save failed: ${');
    expect(content).not.toContain('Reset failed: ${');
  });

  test('api.js _friendlyError exists and is used', () => {
    const content = readFile('src/lib/api.js');
    expect(content).toContain('_friendlyError(');
    expect(content).toContain('friendly UI message');
  });

  test('api.js _logApiError exists for masked console logging', () => {
    const content = readFile('src/lib/api.js');
    expect(content).toContain('function _logApiError');
    expect(content).toContain('_maskForDemoLog');
  });
});
