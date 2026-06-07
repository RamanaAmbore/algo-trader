import { redirect } from '@sveltejs/kit';

// The /agents/* workspace was renamed to /automation/* in v2.1 — the
// workspace now hosts both Agents (event-driven rules) and Templates
// (per-order configuration). 308 preserves method + bookmarks.
export function load({ url }) {
  throw redirect(308, '/automation' + url.search);
}
