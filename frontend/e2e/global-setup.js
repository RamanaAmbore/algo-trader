/**
 * Global Playwright setup — runs once before all tests.
 *
 * Fetches an auth token via /api/auth/login (direct API call, no browser)
 * and caches it to e2e/.auth/state.json. This eliminates N×3 concurrent
 * /signin form submissions when fullyParallel=true + 3 viewport projects.
 *
 * The auth fixture (loginAsAdmin) reads this cached token and injects it
 * into the page's sessionStorage + Authorization header, bypassing the
 * form entirely. If the cached token fails or is missing, it falls back
 * to the slow-path form login.
 */

import { request } from '@playwright/test';
import { writeFileSync, mkdirSync, readFileSync, existsSync } from 'fs';
import { dirname } from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const STATE_FILE = 'e2e/.auth/state.json';
// JWT is 24h; re-use if cached within 20h to stay well within expiry window
const CACHE_TTL_MS = 20 * 60 * 60 * 1000;

export default async function globalSetup() {
  // Fast path: reuse cached token if it's fresh enough
  if (existsSync(STATE_FILE)) {
    try {
      const saved = JSON.parse(readFileSync(STATE_FILE, 'utf-8'));
      if (saved?.token && saved?.user === USER && saved?.cached_at) {
        const age = Date.now() - new Date(saved.cached_at).getTime();
        if (age < CACHE_TTL_MS) {
          console.log(`[global-setup] Reusing cached token for ${USER} (age ${Math.round(age / 60000)}m)`);
          return; // Skip re-fetch — cached token still valid
        }
      }
    } catch (_) { /* corrupt cache — fall through to re-fetch */ }
  }

  console.log(`[global-setup] Fetching auth token for ${USER} from ${BASE}`);

  const ctx = await request.newContext({ baseURL: BASE });
  try {
    const res = await ctx.post('/api/auth/login', {
      data: { username: USER, password: PASS },
    });

    if (!res.ok()) {
      const text = await res.text();
      throw new Error(`Auth failed ${res.status()}: ${text}`);
    }

    const body = await res.json();
    // Backend returns LoginResponse with access_token field
    const token = body.access_token;
    if (!token) {
      throw new Error(
        `No access_token in auth response: ${JSON.stringify(body)}`
      );
    }

    // Cache token + user to e2e/.auth/state.json
    const stateDir = 'e2e/.auth';
    mkdirSync(stateDir, { recursive: true });
    const state = {
      token,
      user: USER,
      role: body.role,
      display_name: body.display_name,
      cached_at: new Date().toISOString(),
    };
    writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
    console.log(`[global-setup] Cached auth token to ${STATE_FILE}`);
  } finally {
    await ctx.dispose();
  }
}
