import { redirect } from '@sveltejs/kit';

// /automation/fragments was the old URL before agent-templates was
// established as the canonical route. 308 preserves method + bookmarks.
export function load() {
  throw redirect(308, '/automation/agent-templates');
}
