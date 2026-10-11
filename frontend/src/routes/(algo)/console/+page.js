import { redirect } from '@sveltejs/kit';

// /console retired as a standalone route (2026-10, Phase 2 Sprint B —
// "Console retirement, Agent/Bracket/Template unification"). Its content
// (plain orders bar + agent-family CLI) moved to /automation's new
// Console tab — see frontend/src/lib/automation/ConsoleTab.svelte.
//
// Universal load (not +page.server.js) because the app is SPA-only
// (ssr = false, adapter-static). Operator called /console "used
// extensively" — a hard 404 would break every existing bookmark, so this
// forwards to the Console tab instead. `tab` is forced to 'console';
// every OTHER search param is carried over unchanged. Sprint C (2026-10,
// Phase 2 "Automation redesign") removed BracketsTab's "Open in Console"
// `?cli=` prefill mechanism along with the Brackets tab itself — the
// Agents-tab row list's Edit action now writes straight into
// `/automation/+page.svelte`'s own `_consoleCliText`/`_consoleEditTarget`
// state without a URL round-trip, so `/automation`'s `$effect` on
// `page.url` no longer consumes a `?cli=` param. A pre-Sprint-C bookmark
// carrying `?cli=...` still redirects here without erroring; the prefill
// itself is simply a no-op now (low-value to preserve — nothing in the
// live app emits that link shape anymore).
export function load({ url }) {
  const params = new URLSearchParams(url.search);
  params.set('tab', 'console');
  throw redirect(308, '/automation?' + params.toString());
}
