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
import { writeFileSync, mkdirSync } from 'fs';
import { dirname } from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

export default async function globalSetup() {
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
    const stateFile = 'e2e/.auth/state.json';
    const state = {
      token,
      user: USER,
      role: body.role,
      display_name: body.display_name,
      cached_at: new Date().toISOString(),
    };
    writeFileSync(stateFile, JSON.stringify(state, null, 2));
    console.log(`[global-setup] Cached auth token to ${stateFile}`);
  } finally {
    await ctx.dispose();
  }
}
