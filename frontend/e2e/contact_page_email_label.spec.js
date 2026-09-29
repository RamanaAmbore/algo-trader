/**
 * contact_page_email_label.spec.js
 *
 * Guards the 2026-09-29 fix: the contact page's direct-email line
 * showed just the mailto link with no label ("contact@ramboquant.com"),
 * unlike the neighboring "Registered Office:" line. Added an "Email:"
 * label to match.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const CONTACT_PAGE_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/routes/(public)/contact/+page.svelte',
);

test('Stale-code: direct-email line has an "Email:" label before the mailto link', () => {
  const src = readFileSync(CONTACT_PAGE_PATH, 'utf8');
  const line = src.match(/Email:\s*<a href="mailto:contact@ramboquant\.com">/);
  expect(line).not.toBeNull();
});
