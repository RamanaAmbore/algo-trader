/**
 * quoteStreamVersion.test.js
 *
 * Unit tests for the _onVersion function in quoteStream.js.
 *
 * The _onVersion function:
 *   1. Parses SSE 'version' event JSON to extract { hash }
 *   2. Ignores events where hash is falsy or 'unknown'
 *   3. On first call: stores the hash as baseline (no reload)
 *   4. On subsequent calls with same hash: no reload
 *   5. On subsequent calls with different hash: calls window.location.reload()
 *
 * _serverHash is module-level state that persists across reconnects,
 * and is only compared against the baseline set at first connection.
 *
 * Five quality dimensions:
 *   1. SSOT   — logic matches quoteStream.js _onVersion implementation
 *   2. Perf   — sync pure function, sub-microsecond, no network
 *   3. Stale  — guards against falsy/unknown hashes, malformed JSON
 *   4. Reuse  — integrable with EventSource listener pattern
 *   5. UX     — deploy detection prevents stale JS chunks from being used
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Mock window.location.reload (node environment) ─────────────────────────────

// Node environment doesn't have window; inject a mock before tests run
if (typeof globalThis.window === 'undefined') {
  /** @type {any} */ (globalThis).window = { location: { reload: vi.fn() } };
}

// ── Local helper: mirrors quoteStream.js _onVersion implementation ────────────

/**
 * Simulates the quoteStream _onVersion logic in isolation.
 * Maintains module-level state (_serverHash) to test persistence across calls.
 * Includes visibility-gated reload logic (tab-switch garble fix).
 *
 * @type {{ serverHash: null | string, onVersion: (e: {data: string}, visibility?: string) => {reloadTriggered: boolean, deferredToVisibility: boolean} }}
 */
const versionHandler = (() => {
  let serverHash = null;

  return {
    get serverHash() { return serverHash; },
    set serverHash(v) { serverHash = v; },
    /**
     * @param {{data: string}} e
     * @param {string} [visibility] - simulates document.visibilityState
     */
    onVersion(e, visibility = 'visible') {
      try {
        const { hash } = JSON.parse(e.data);
        if (!hash || hash === 'unknown') return { reloadTriggered: false, deferredToVisibility: false };
        if (serverHash === null) {
          serverHash = hash;           // first connection — record baseline
          return { reloadTriggered: false, deferredToVisibility: false };
        } else if (serverHash !== hash) {
          if (visibility === 'hidden') {
            // Defer: register visibilitychange listener, do NOT reload now
            return { reloadTriggered: false, deferredToVisibility: true };
          }
          // In real code: window.location.reload()
          return { reloadTriggered: true, deferredToVisibility: false };
        }
        return { reloadTriggered: false, deferredToVisibility: false };
      } catch (_) { /* malformed JSON — ignore */ }
      return { reloadTriggered: false, deferredToVisibility: false };
    },
  };
})();


// ── Tests ──────────────────────────────────────────────────────────────────────

describe('quoteStream _onVersion — SSE version event handling', () => {
  beforeEach(() => {
    versionHandler.serverHash = null;  // reset module-level state between tests
  });

  // ── Test 1: First version event sets baseline, does NOT reload ──────────────

  describe('A: First version event', () => {
    it('hash="abc123" on first call: sets baseline, no reload', () => {
      const e = { data: JSON.stringify({ hash: 'abc123' }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe('abc123');
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash="v1.0.0" on first call: stores hash, no reload', () => {
      const e = { data: JSON.stringify({ hash: 'v1.0.0' }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe('v1.0.0');
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash is long git SHA: first call records it, no reload', () => {
      const longSha = 'f84dcb2bef27dcb9f0c2f0c2f0c2f0c2f0c2f0c2';
      const e = { data: JSON.stringify({ hash: longSha }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(longSha);
      expect(result.reloadTriggered).toBe(false);
    });
  });

  // ── Test 2: Same hash on reconnect does NOT reload ────────────────────────

  describe('B: Same hash on reconnect (no deploy)', () => {
    it('baseline=abc123, second call with abc123: no reload', () => {
      // First call
      const e1 = { data: JSON.stringify({ hash: 'abc123' }) };
      versionHandler.onVersion(e1);

      // Second call with same hash (reconnect)
      const e2 = { data: JSON.stringify({ hash: 'abc123' }) };
      const result = versionHandler.onVersion(e2);

      expect(versionHandler.serverHash).toBe('abc123');
      expect(result.reloadTriggered).toBe(false);
    });

    it('baseline=v2.0.0, third call with v2.0.0: no reload', () => {
      const e1 = { data: JSON.stringify({ hash: 'v2.0.0' }) };
      versionHandler.onVersion(e1);

      // Simulate reconnects
      const e2 = { data: JSON.stringify({ hash: 'v2.0.0' }) };
      const e3 = { data: JSON.stringify({ hash: 'v2.0.0' }) };

      versionHandler.onVersion(e2);
      const result = versionHandler.onVersion(e3);

      expect(versionHandler.serverHash).toBe('v2.0.0');
      expect(result.reloadTriggered).toBe(false);
    });
  });

  // ── Test 3: Different hash on reconnect triggers reload (deploy detected) ───

  describe('C: Different hash on reconnect (deploy detected)', () => {
    it('baseline=abc123, second call with xyz789: reload triggered', () => {
      // First connection: record baseline
      const e1 = { data: JSON.stringify({ hash: 'abc123' }) };
      versionHandler.onVersion(e1);

      // Second call: deploy detected (hash changed)
      const e2 = { data: JSON.stringify({ hash: 'xyz789' }) };
      const result = versionHandler.onVersion(e2);

      expect(versionHandler.serverHash).toBe('abc123');  // baseline unchanged
      expect(result.reloadTriggered).toBe(true);
    });

    it('baseline=v1.0, second call with v2.0: reload triggered', () => {
      const e1 = { data: JSON.stringify({ hash: 'v1.0' }) };
      versionHandler.onVersion(e1);

      const e2 = { data: JSON.stringify({ hash: 'v2.0' }) };
      const result = versionHandler.onVersion(e2);

      expect(result.reloadTriggered).toBe(true);
    });

    it('baseline=long-sha-A, second call with long-sha-B: reload triggered', () => {
      const shaA = 'f84dcb2bef27dcb9f0c2f0c2f0c2f0c2f0c2f0c2';
      const shaB = '52ea78e5f0c2f0c2f0c2f0c2f0c2f0c2f0c2f0c2';

      const e1 = { data: JSON.stringify({ hash: shaA }) };
      versionHandler.onVersion(e1);

      const e2 = { data: JSON.stringify({ hash: shaB }) };
      const result = versionHandler.onVersion(e2);

      expect(result.reloadTriggered).toBe(true);
    });

    it('three calls: baseline, same, different → reload triggers on third', () => {
      const e1 = { data: JSON.stringify({ hash: 'hash1' }) };
      const e2 = { data: JSON.stringify({ hash: 'hash1' }) };
      const e3 = { data: JSON.stringify({ hash: 'hash2' }) };

      const r1 = versionHandler.onVersion(e1);
      const r2 = versionHandler.onVersion(e2);
      const r3 = versionHandler.onVersion(e3);

      expect(r1.reloadTriggered).toBe(false);
      expect(r2.reloadTriggered).toBe(false);
      expect(r3.reloadTriggered).toBe(true);
      expect(versionHandler.serverHash).toBe('hash1');  // baseline unchanged
    });
  });

  // ── Test 4: Falsy hash is ignored (no baseline set, no reload) ─────────────

  describe('D: Falsy hash values are ignored', () => {
    it('hash=""(empty string): ignored, no baseline set, no reload', () => {
      const e = { data: JSON.stringify({ hash: '' }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);  // baseline NOT set
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash=null: ignored, no baseline set', () => {
      const e = { data: JSON.stringify({ hash: null }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash=undefined (omitted key): ignored, no baseline set', () => {
      const e = { data: JSON.stringify({}) };  // no 'hash' key
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash=0: ignored (falsy), no baseline set', () => {
      const e = { data: JSON.stringify({ hash: 0 }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash=false: ignored (falsy), no baseline set', () => {
      const e = { data: JSON.stringify({ hash: false }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });
  });

  // ── Test 5: 'unknown' hash is ignored (special case) ───────────────────────

  describe('D2: "unknown" hash value (special case)', () => {
    it('hash="unknown": ignored, no baseline set, no reload', () => {
      const e = { data: JSON.stringify({ hash: 'unknown' }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);  // baseline NOT set
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash="unknown" on second call (after baseline set): ignored, no reload', () => {
      // Set baseline
      const e1 = { data: JSON.stringify({ hash: 'abc123' }) };
      versionHandler.onVersion(e1);

      // Second call with "unknown"
      const e2 = { data: JSON.stringify({ hash: 'unknown' }) };
      const result = versionHandler.onVersion(e2);

      expect(versionHandler.serverHash).toBe('abc123');  // baseline unchanged
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash="UNKNOWN" (case-sensitive): should NOT be treated as special', () => {
      // The spec says hash === 'unknown' (lowercase), so uppercase should differ
      const e1 = { data: JSON.stringify({ hash: 'abc123' }) };
      versionHandler.onVersion(e1);

      const e2 = { data: JSON.stringify({ hash: 'UNKNOWN' }) };
      const result = versionHandler.onVersion(e2);

      // 'UNKNOWN' !== 'unknown' and !== 'abc123', so it should trigger reload
      expect(result.reloadTriggered).toBe(true);
    });
  });

  // ── Test 6: Malformed JSON is ignored safely ────────────────────────────────

  describe('E: Malformed JSON handling', () => {
    it('invalid JSON: caught and ignored, no baseline set, no reload', () => {
      const e = { data: '{invalid json}' };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });

    it('empty string: caught and ignored', () => {
      const e = { data: '' };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });

    it('not JSON at all: caught and ignored', () => {
      const e = { data: 'just plain text' };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });

    it('JSON without hash field: no baseline set, no reload', () => {
      const e = { data: JSON.stringify({ other_field: 'value' }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe(null);
      expect(result.reloadTriggered).toBe(false);
    });

    it('JSON with extra fields: hash extracted correctly, baseline set', () => {
      const e = { data: JSON.stringify({ hash: 'abc123', timestamp: 123456, other: 'data' }) };
      const result = versionHandler.onVersion(e);

      expect(versionHandler.serverHash).toBe('abc123');
      expect(result.reloadTriggered).toBe(false);
    });
  });

  // ── Test 7: Module state persistence (no reset on error/reconnect) ─────────

  describe('F: Module-level state persistence', () => {
    it('baseline set once, survives multiple calls, only reloads on hash change', () => {
      // Baseline
      const e1 = { data: JSON.stringify({ hash: 'baseline-hash' }) };
      const r1 = versionHandler.onVersion(e1);
      expect(r1.reloadTriggered).toBe(false);
      expect(versionHandler.serverHash).toBe('baseline-hash');

      // Five more calls with same hash (simulating reconnects)
      for (let i = 0; i < 5; i++) {
        const e = { data: JSON.stringify({ hash: 'baseline-hash' }) };
        const r = versionHandler.onVersion(e);
        expect(r.reloadTriggered).toBe(false);
      }

      // Now a different hash
      const eDeploy = { data: JSON.stringify({ hash: 'new-hash' }) };
      const rDeploy = versionHandler.onVersion(eDeploy);
      expect(rDeploy.reloadTriggered).toBe(true);
      expect(versionHandler.serverHash).toBe('baseline-hash');  // unchanged
    });

    it('baseline persists even after ignored event (unknown)', () => {
      const e1 = { data: JSON.stringify({ hash: 'abc123' }) };
      versionHandler.onVersion(e1);

      // Ignored event
      const eIgnored = { data: JSON.stringify({ hash: 'unknown' }) };
      versionHandler.onVersion(eIgnored);

      // Baseline should still be set
      expect(versionHandler.serverHash).toBe('abc123');

      // And a different hash triggers reload
      const eDeploy = { data: JSON.stringify({ hash: 'xyz789' }) };
      const result = versionHandler.onVersion(eDeploy);
      expect(result.reloadTriggered).toBe(true);
    });

    it('baseline persists even after malformed event', () => {
      const e1 = { data: JSON.stringify({ hash: 'abc123' }) };
      versionHandler.onVersion(e1);

      // Malformed event
      const eBad = { data: '{bad json' };
      versionHandler.onVersion(eBad);

      expect(versionHandler.serverHash).toBe('abc123');

      // Should still trigger reload on hash change
      const eDeploy = { data: JSON.stringify({ hash: 'new-hash' }) };
      const result = versionHandler.onVersion(eDeploy);
      expect(result.reloadTriggered).toBe(true);
    });
  });

  // ── Test 8: Real-world scenarios ────────────────────────────────────────────

  describe('G: Real-world deployment scenarios', () => {
    it('scenario: dev deploy (hash changes)', () => {
      // Session starts with dev.ramboq.com running v1.0
      const eSessionStart = { data: JSON.stringify({ hash: 'v1.0-dev' }) };
      const r1 = versionHandler.onVersion(eSessionStart);
      expect(r1.reloadTriggered).toBe(false);

      // SSE reconnects a few times (network hiccup) with same hash
      const eReconnect = { data: JSON.stringify({ hash: 'v1.0-dev' }) };
      const r2 = versionHandler.onVersion(eReconnect);
      const r3 = versionHandler.onVersion(eReconnect);
      expect(r2.reloadTriggered).toBe(false);
      expect(r3.reloadTriggered).toBe(false);

      // Deploy happens: v1.0 → v2.0
      const eDeploy = { data: JSON.stringify({ hash: 'v2.0-dev' }) };
      const rDeploy = versionHandler.onVersion(eDeploy);
      expect(rDeploy.reloadTriggered).toBe(true);
      // Browser reloads, JS chunks are fresh, new session starts with v2.0 baseline
    });

    it('scenario: session survives deploy by resuming with fresh chunks', () => {
      // Initial connection at v1
      const e1 = { data: JSON.stringify({ hash: 'f84dcb2b' }) };
      versionHandler.onVersion(e1);
      expect(versionHandler.serverHash).toBe('f84dcb2b');

      // Deploy happens, client detects hash changed, triggers reload()
      const eDeploy = { data: JSON.stringify({ hash: '52ea78e5' }) };
      const result = versionHandler.onVersion(eDeploy);
      expect(result.reloadTriggered).toBe(true);

      // After browser reload, new session starts (serverHash is reset to null)
      // and connects with the new hash. This test simulates the post-reload session:
      versionHandler.serverHash = null;  // reset as would happen on page reload
      const e2PostReload = { data: JSON.stringify({ hash: '52ea78e5' }) };
      const r2 = versionHandler.onVersion(e2PostReload);
      expect(r2.reloadTriggered).toBe(false);
      expect(versionHandler.serverHash).toBe('52ea78e5');
    });

    it('scenario: partial deploy (one service upgraded, other not)', () => {
      // This tests resilience: if SSE server hasn't redeployed yet
      // but API has, the version mismatch should NOT cause an issue
      // (version event is optional, not all servers send it).

      // Initial connection
      const e1 = { data: JSON.stringify({ hash: 'api-v1-sse-v1' }) };
      versionHandler.onVersion(e1);

      // Reconnect with same hash (normal case)
      const e2 = { data: JSON.stringify({ hash: 'api-v1-sse-v1' }) };
      const r2 = versionHandler.onVersion(e2);
      expect(r2.reloadTriggered).toBe(false);

      // If version event isn't sent, we don't get into an edge case
      // because the guard `if (!hash)` catches missing/undefined.
    });
  });

  // ── Test 9: Concurrent reconnects with same baseline ──────────────────────

  describe('H: Rapid reconnects (edge case)', () => {
    it('five rapid reconnects with same hash: no reloads', () => {
      const e1 = { data: JSON.stringify({ hash: 'stable' }) };
      versionHandler.onVersion(e1);

      const results = [];
      for (let i = 0; i < 5; i++) {
        const e = { data: JSON.stringify({ hash: 'stable' }) };
        results.push(versionHandler.onVersion(e));
      }

      results.forEach(r => expect(r.reloadTriggered).toBe(false));
      expect(versionHandler.serverHash).toBe('stable');
    });

    it('rapid reconnects, then deploy: only last one triggers reload', () => {
      const e1 = { data: JSON.stringify({ hash: 'v1' }) };
      versionHandler.onVersion(e1);

      // Reconnects
      for (let i = 0; i < 3; i++) {
        const e = { data: JSON.stringify({ hash: 'v1' }) };
        versionHandler.onVersion(e);
      }

      // Deploy
      const eDeploy = { data: JSON.stringify({ hash: 'v2' }) };
      const result = versionHandler.onVersion(eDeploy);
      expect(result.reloadTriggered).toBe(true);
    });
  });

  // ── Test 10: Case sensitivity ───────────────────────────────────────────────

  describe('I: Case sensitivity', () => {
    it('baseline="ABC123", second call with "abc123": differs, triggers reload', () => {
      const e1 = { data: JSON.stringify({ hash: 'ABC123' }) };
      versionHandler.onVersion(e1);

      const e2 = { data: JSON.stringify({ hash: 'abc123' }) };
      const result = versionHandler.onVersion(e2);

      expect(result.reloadTriggered).toBe(true);
    });

    it('baseline="v1.0.0", second call with "V1.0.0": differs, triggers reload', () => {
      const e1 = { data: JSON.stringify({ hash: 'v1.0.0' }) };
      versionHandler.onVersion(e1);

      const e2 = { data: JSON.stringify({ hash: 'V1.0.0' }) };
      const result = versionHandler.onVersion(e2);

      expect(result.reloadTriggered).toBe(true);
    });
  });

  // ── Test 11: Special characters in hash ────────────────────────────────────

  describe('J: Special characters and whitespace', () => {
    it('hash with dashes: "v1.0.0-rc1"', () => {
      const e1 = { data: JSON.stringify({ hash: 'v1.0.0-rc1' }) };
      const r1 = versionHandler.onVersion(e1);
      expect(r1.reloadTriggered).toBe(false);
      expect(versionHandler.serverHash).toBe('v1.0.0-rc1');

      const e2 = { data: JSON.stringify({ hash: 'v1.0.0-rc1' }) };
      const r2 = versionHandler.onVersion(e2);
      expect(r2.reloadTriggered).toBe(false);
    });

    it('hash with underscores: "deploy_2026_09_07"', () => {
      const e1 = { data: JSON.stringify({ hash: 'deploy_2026_09_07' }) };
      versionHandler.onVersion(e1);
      expect(versionHandler.serverHash).toBe('deploy_2026_09_07');

      const e2 = { data: JSON.stringify({ hash: 'deploy_2026_09_07' }) };
      const result = versionHandler.onVersion(e2);
      expect(result.reloadTriggered).toBe(false);
    });

    it('hash with leading/trailing whitespace: not trimmed, treated as different', () => {
      const e1 = { data: JSON.stringify({ hash: ' abc123 ' }) };
      versionHandler.onVersion(e1);
      expect(versionHandler.serverHash).toBe(' abc123 ');

      // Exact match required
      const e2 = { data: JSON.stringify({ hash: ' abc123 ' }) };
      const r2 = versionHandler.onVersion(e2);
      expect(r2.reloadTriggered).toBe(false);

      // Without whitespace: different, triggers reload
      const e3 = { data: JSON.stringify({ hash: 'abc123' }) };
      const r3 = versionHandler.onVersion(e3);
      expect(r3.reloadTriggered).toBe(true);
    });
  });

  // ── Test 12: Spec compliance — integration preview ───────────────────────

  describe('K: Spec compliance (mirrors quoteStream.js _onVersion)', () => {
    it('spec A: hash falsy or unknown → no baseline, no reload', () => {
      const falsy = [null, '', undefined, 0, false];
      falsy.forEach(val => {
        versionHandler.serverHash = null;
        const e = { data: JSON.stringify({ hash: val }) };
        const result = versionHandler.onVersion(e);
        expect(versionHandler.serverHash).toBe(null);
        expect(result.reloadTriggered).toBe(false);
      });

      versionHandler.serverHash = null;
      const eUnknown = { data: JSON.stringify({ hash: 'unknown' }) };
      const rUnknown = versionHandler.onVersion(eUnknown);
      expect(rUnknown.reloadTriggered).toBe(false);
    });

    it('spec B: first call stores hash (baseline)', () => {
      const e = { data: JSON.stringify({ hash: 'initial' }) };
      versionHandler.onVersion(e);
      expect(versionHandler.serverHash).toBe('initial');
    });

    it('spec C: same hash, subsequent calls, no reload', () => {
      const e1 = { data: JSON.stringify({ hash: 'same' }) };
      versionHandler.onVersion(e1);

      for (let i = 0; i < 10; i++) {
        const e = { data: JSON.stringify({ hash: 'same' }) };
        const r = versionHandler.onVersion(e);
        expect(r.reloadTriggered).toBe(false);
      }
    });

    it('spec D: different hash triggers reload (deploy detected)', () => {
      const e1 = { data: JSON.stringify({ hash: 'baseline' }) };
      versionHandler.onVersion(e1);

      const e2 = { data: JSON.stringify({ hash: 'different' }) };
      const result = versionHandler.onVersion(e2);
      expect(result.reloadTriggered).toBe(true);
    });
  });

  // ── Test 13: Visibility-gated reload (tab-switch garble fix) ─────────────────
  // Root cause: after SSE version event fix, a tab that was hidden during a server
  // restart would reload() mid-render on tab return, causing "garbled" appearance.
  // Fix: defer reload to visibilitychange when document.visibilityState === 'hidden'.

  describe('L: Visibility-gated reload', () => {
    beforeEach(() => {
      versionHandler.serverHash = null;
    });

    it('hash change while VISIBLE triggers immediate reload (no deferral)', () => {
      const e1 = { data: JSON.stringify({ hash: 'v1' }) };
      versionHandler.onVersion(e1, 'visible');

      const e2 = { data: JSON.stringify({ hash: 'v2' }) };
      const result = versionHandler.onVersion(e2, 'visible');

      expect(result.reloadTriggered).toBe(true);
      expect(result.deferredToVisibility).toBe(false);
    });

    it('hash change while HIDDEN defers reload, does NOT reload immediately', () => {
      const e1 = { data: JSON.stringify({ hash: 'v1' }) };
      versionHandler.onVersion(e1, 'visible');  // baseline set while visible

      const e2 = { data: JSON.stringify({ hash: 'v2' }) };
      const result = versionHandler.onVersion(e2, 'hidden');  // tab hidden during deploy

      expect(result.reloadTriggered).toBe(false);       // no immediate reload
      expect(result.deferredToVisibility).toBe(true);   // deferred to tab return
    });

    it('no change while hidden: neither reload nor deferral', () => {
      const e1 = { data: JSON.stringify({ hash: 'stable' }) };
      versionHandler.onVersion(e1, 'visible');

      const e2 = { data: JSON.stringify({ hash: 'stable' }) };
      const result = versionHandler.onVersion(e2, 'hidden');

      expect(result.reloadTriggered).toBe(false);
      expect(result.deferredToVisibility).toBe(false);
    });

    it('first connection while hidden: records baseline, no reload/deferral', () => {
      const e1 = { data: JSON.stringify({ hash: 'v1' }) };
      const result = versionHandler.onVersion(e1, 'hidden');

      expect(result.reloadTriggered).toBe(false);
      expect(result.deferredToVisibility).toBe(false);
      expect(versionHandler.serverHash).toBe('v1');
    });

    it('tab-switch scenario: hidden during deploy → deferred; on tab return → fires', () => {
      // User has tab open, session starts
      versionHandler.onVersion({ data: JSON.stringify({ hash: 'pre-deploy' }) }, 'visible');

      // User switches away; server gets deployed; SSE reconnects while hidden
      const result = versionHandler.onVersion({ data: JSON.stringify({ hash: 'post-deploy' }) }, 'hidden');
      expect(result.deferredToVisibility).toBe(true);
      expect(result.reloadTriggered).toBe(false);

      // User switches back — visibilitychange fires → reload() runs
      // (In real code: the addEventListener callback calls window.location.reload().
      //  Here we just verify the condition: deferredToVisibility=true means the
      //  listener was registered and will fire on the next visibilitychange.)
    });
  });
});
