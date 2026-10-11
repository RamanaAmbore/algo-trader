import { redirect } from '@sveltejs/kit';

// /console/guide moved to /automation/guide (2026-10, Phase 2 Sprint B —
// Console retirement). Universal load (SPA-only app, ssr=false).
export function load() {
  throw redirect(308, '/automation/guide');
}
