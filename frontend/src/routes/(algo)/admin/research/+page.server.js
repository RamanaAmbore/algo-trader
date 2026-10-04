import { redirect } from '@sveltejs/kit';

// /admin/lab was the old Lab URL before the rename to /admin/lab.
// 308 preserves method + bookmarks.
export function load() {
  throw redirect(308, '/admin/lab');
}
