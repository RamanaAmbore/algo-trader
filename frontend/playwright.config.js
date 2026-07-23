import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  // globalSetup runs once before all tests — fetches auth token via API,
  // caches it to e2e/.auth/state.json. loginAsAdmin reuses this cached token
  // to bypass the /signin form, eliminating N×3 concurrent rate-limit hits
  // when fullyParallel=true + 3 viewport projects.
  globalSetup: './e2e/global-setup.js',
  // Default per-test timeout. beforeAll hooks with loginAsAdmin + slow
  // dev-server pages (SSE keeps networkidle busy) need headroom beyond 30 s.
  timeout: 90_000,

  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174',
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  // Three viewport projects — every spec runs against all three by
  // default. Override via `--project=mobile-portrait` etc.
  // Names use the short `chromium-*` prefix to match the existing
  // smoke spec convention.
  projects: [
    {
      name: 'chromium-desktop',
      use: { viewport: { width: 1400, height: 900 }, browserName: 'chromium' },
    },
    {
      name: 'mobile-portrait',
      use: { viewport: { width: 360,  height: 800 }, browserName: 'chromium', isMobile: true, hasTouch: true },
    },
    {
      name: 'mobile-landscape',
      use: { viewport: { width: 800,  height: 360 }, browserName: 'chromium', isMobile: true, hasTouch: true },
    },
  ],

  // Skip the local dev server when running against an external URL
  // (e.g. PLAYWRIGHT_BASE_URL=https://dev.ramboq.com for cloud diagnostics).
  webServer: process.env.PLAYWRIGHT_SKIP_WEBSERVER || process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command: 'npm run dev -- --port 5174 --strictPort',
        url: 'http://localhost:5174',
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
});
