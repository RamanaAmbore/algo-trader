import { redirect } from '@sveltejs/kit';

// /automation/activity merged into the shared /activity surface (Agent tab).
// Universal load (not +page.server.js) because the app is SPA-only
// (ssr = false, adapter-static). Incoming search params are kept; tab is
// forced to 'agent' so the redirect always lands on the Agent log.
export function load({ url }) {
  const params = new URLSearchParams(url.search);
  params.set('tab', 'agent');
  throw redirect(308, '/activity?' + params.toString());
}
