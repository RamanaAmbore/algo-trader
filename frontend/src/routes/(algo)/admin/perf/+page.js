import { redirect } from '@sveltejs/kit';

// /admin/perf merged into /admin/metrics as the "Runtime" tab.
// Universal load (not +page.server.js) because the app is SPA-only
// (ssr = false, adapter-static). Incoming search params are kept; tab is
// forced to 'runtime' so the redirect always lands on the Runtime view.
export function load({ url }) {
  const params = new URLSearchParams(url.search);
  params.set('tab', 'runtime');
  throw redirect(308, '/admin/metrics?' + params.toString());
}
