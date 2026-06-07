import { redirect } from '@sveltejs/kit';

export function load({ url }) {
  throw redirect(308, '/automation/activity' + url.search);
}
