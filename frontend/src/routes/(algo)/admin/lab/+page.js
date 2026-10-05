import { redirect } from '@sveltejs/kit';

// /admin/lab was the old URL before the Lab page was renamed to MCP.
// Universal load (not +page.server.js) because the app is SPA-only
// (ssr = false, adapter-static): this runs in the browser on every
// visit, with no server round-trip. 308 is the status for server
// responses; the client redirect preserves the query string
// (e.g. the ?audit_request= deep link in Telegram pings).
export function load({ url }) {
  throw redirect(308, '/admin/mcp' + url.search);
}
