// Verify the Phase 2b GenAI helpers:
//   - Auto-title: POST /api/research/threads with title='' produces
//     a non-empty title that's NOT the bare stub "{SYM} research".
//   - Sentiment: GET /api/news/?sentiment=true responds with each item
//     carrying a {bull, bear, neutral} tag. Base path (no query) has
//     sentiment=null. Both paths tolerate empty feed gracefully.
//
// Run:
//   BASE_URL=https://dev.ramboq.com npx playwright test research_genai_helpers_verify.spec.js --workers=1 --project=chromium-desktop

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

async function login(page) {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error(`login failed against ${BASE}`);
  return tok;
}

test(`auto-title fills title when blank [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  const thesis = 'RELIANCE looks oversold near 2800 — Q2 EBITDA beat consensus by 8 percent and FII positioning has flipped net long. Expect a 3-5 percent bounce over the next week, with stop at 2750.';

  // Title intentionally blank — server should auto-fill via Gemini Flash
  // (when enabled) or the deterministic stub.
  const r = await page.request.post(`${BASE}/api/research/threads`, {
    data: { symbol: 'PWGENAI', title: '', thesis_text: thesis, confidence: 'bull' },
    headers,
  });
  expect(r.ok(), `create: ${r.status()}`).toBe(true);
  const thread = await r.json();
  console.log(`auto-title result: "${thread.title}"`);

  // Title must be non-empty + non-blank.
  expect(thread.title.length).toBeGreaterThan(3);
  // Reject the bare placeholder "{SYM} research" when we DID give a
  // thesis — that's the empty-thesis fallback, not the empty-title one.
  // The stub when thesis IS provided takes the first sentence; LLM
  // produces something custom. Either is acceptable; just confirm we
  // didn't fall all the way through to "{SYM} research" with a
  // non-empty thesis.
  expect(thread.title.toLowerCase()).not.toBe('pwgenai research');

  // Cleanup
  await page.request.delete(`${BASE}/api/research/threads/${thread.id}`, { headers });
});

test(`auto-title returns symbol-only when thesis is blank [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };

  const r = await page.request.post(`${BASE}/api/research/threads`, {
    data: { symbol: 'PWEMPTY', title: '', confidence: 'unsure' },
    headers,
  });
  expect(r.ok()).toBe(true);
  const thread = await r.json();
  console.log(`empty-thesis title: "${thread.title}"`);

  // With no thesis, both LLM + stub paths produce something — just verify
  // it's not the literal empty string.
  expect(thread.title.trim().length).toBeGreaterThan(0);
  await page.request.delete(`${BASE}/api/research/threads/${thread.id}`, { headers });
});

test(`news sentiment endpoint contract [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  // Base path — items either empty or carry sentiment:null
  const base = await page.request.get(`${BASE}/api/news/`, { headers });
  expect(base.ok()).toBe(true);
  const baseJ = await base.json();
  console.log(`base items: ${baseJ.items.length}`);
  for (const it of baseJ.items.slice(0, 3)) {
    // sentiment field exists (msgspec emits it) — should be null.
    expect(it.sentiment).toBeFalsy();
  }

  // Sentiment path
  const scored = await page.request.get(`${BASE}/api/news/?sentiment=true`, { headers });
  expect(scored.ok()).toBe(true);
  const scoredJ = await scored.json();
  console.log(`scored items: ${scoredJ.items.length}`);
  const valid = new Set(['bull', 'bear', 'neutral']);
  for (const it of scoredJ.items.slice(0, 10)) {
    expect(valid.has(it.sentiment),
      `bad sentiment value: ${JSON.stringify(it.sentiment)} for ${it.title.slice(0,50)}`).toBe(true);
  }
  // Both paths must return same item count (sentiment is layered on top).
  expect(scoredJ.items.length).toBe(baseJ.items.length);
});

test(`news sentiment stub correctness via inline probe [${BASE}]`, async ({ page }) => {
  // Direct stub correctness — not server-side. Confirms our regex
  // handles inflected forms like "jumps" / "plunges". This protects
  // the fallback path that fires when GenAI is off / quota hit.
  // (Can't easily import Python into Playwright, so we just sanity-
  // check that scored items, if present, exercise both bull AND bear
  // tags over a sample — heuristic but catches "all-neutral" regressions.)
  const tok = await login(page);
  const headers = { Authorization: `Bearer ${tok}` };

  const r = await page.request.get(`${BASE}/api/news/?sentiment=true`, { headers });
  const j = await r.json();
  if (j.items.length < 5) {
    console.log(`(soft-skip — only ${j.items.length} headlines, can't probe distribution)`);
    return;
  }
  const counts = j.items.reduce((a, it) => { a[it.sentiment] = (a[it.sentiment] || 0) + 1; return a; }, {});
  console.log(`sentiment distribution:`, counts);
  // Should have at least 2 distinct categories across a reasonable
  // sample (catches the "everything neutral" regression).
  const nonNullKeys = Object.keys(counts).filter(k => k && k !== 'null');
  expect(nonNullKeys.length, `expected ≥2 sentiment classes; got ${nonNullKeys.join(',')}`).toBeGreaterThanOrEqual(2);
});
