import { redirect } from '@sveltejs/kit';

// /admin/research was an earlier URL for the MCP page (formerly Lab).
// Universal load for the same reason as admin/lab/+page.js (SPA-only).
// Points straight at /admin/mcp to avoid a redirect chain; the query
// string (e.g. the ?audit_request= deep link built by
// backend/api/routes/lab.py) is carried over.
export function load({ url }) {
  throw redirect(308, '/admin/mcp' + url.search);
}
