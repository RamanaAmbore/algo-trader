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
// every OTHER search param (notably `?cli=`, BracketsTab's "Open in
// Console" prefill mechanism) is carried over unchanged — /automation's
// own `$effect` on `page.url` consumes `cli` the same way this page used
// to.
export function load({ url }) {
  const params = new URLSearchParams(url.search);
  params.set('tab', 'console');
  throw redirect(308, '/automation?' + params.toString());
}
