import { redirect } from '@sveltejs/kit';

// /automation/fragments was renamed to /automation/agent-templates in
// v2.1 — operator vocabulary unified under "templates" (notify
// templates · condition templates · order templates). 308 preserves
// method + bookmarks.
export function load({ url }) {
  throw redirect(308, '/automation/agent-templates' + url.search);
}
