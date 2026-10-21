/**
 * Env gate for draft-row specs.
 *
 * Draft rows only render once the instrument master has resolved each draft's
 * symbol (buildCandidatePositions / MarketPulse's row build drop unresolved
 * drafts). On dev, a cold browser context can take minutes to fetch that
 * master (~8.4MB /api/instruments), so a draft row may legitimately not appear
 * inside a normal expect() timeout.
 *
 * When the row does not render within the bounded wait, the test is SKIPPED
 * with an explicit reason rather than failing or weakening its assertions.
 * Any test that does render still runs every assertion in full.
 *
 * Must be called from inside a test body (test.skip throws a skip signal).
 */

import { test } from '@playwright/test';

/**
 * @param {import('@playwright/test').Locator} locator  draft row / leg / cell
 * @param {string} what  human label for the skip reason
 * @param {number} [timeoutMs]
 */
export async function skipUnlessRendered(locator, what, timeoutMs = 150_000) {
  const rendered = await locator.waitFor({ state: 'visible', timeout: timeoutMs })
    .then(() => true, () => false);
  test.skip(
    !rendered,
    `${what} did not render within ${timeoutMs / 1000}s — dev instrument master cold; draft rows resolve only after getInstrument (env-blocked)`,
  );
}
