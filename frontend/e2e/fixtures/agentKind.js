// Shared helper for the Agents-tab "Agent kind" dropdown (Select.svelte).
// The three kinds are a single custom dropdown, not card buttons. Each
// option label carries a trailing count, e.g. "Action agent (3)", so
// options are matched by label substring, never by exact name. The selected
// option's accessible name carries a leading check mark ("✓ Action agent (1)"),
// so the match must NOT be anchored to the start.
import { expect } from '@playwright/test';

export const AGENT_KIND_TIMEOUT = 20_000;

/** cardId -> option label (without the trailing count). */
export const AGENT_KIND_LABEL = {
  agents: 'Action agent',
  alerts: 'Alert agent',
  brackets: 'Bracket',
};

/** cardId -> option accessible-name regex (count suffix tolerated). */
export const AGENT_KIND_OPTION = Object.fromEntries(
  Object.entries(AGENT_KIND_LABEL).map(([id, label]) => [id, new RegExp(`${label} \\(`)]),
);

/** Accessible name of the dropdown trigger (Select's aria-label). */
export function agentKindTrigger(page) {
  return page.getByRole('button', { name: 'Agent kind' });
}

/** Open the "Agent kind" dropdown and pick the option for `cardId`. */
export async function pickAgentKind(page, cardId) {
  const trigger = agentKindTrigger(page);
  await expect(trigger).toBeVisible({ timeout: AGENT_KIND_TIMEOUT });
  await trigger.click();
  await page.getByRole('option', { name: AGENT_KIND_OPTION[cardId] }).click();
  await expect(trigger).toContainText(AGENT_KIND_LABEL[cardId], { timeout: AGENT_KIND_TIMEOUT });
}
