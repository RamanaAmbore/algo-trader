# Plan: fix(broker-badge): account cell background tint — match Pulse grid pattern

## Context
The BrokerHealthBadge popup account column shows text-only color coding (green/amber/red)
but no background tint. The nav/capital/equity Pulse grids use `color-mix(in srgb,
var(--mp-sym-acct-color, transparent) 14%, transparent)` on the account/symbol cell —
the hash color is injected as a CSS custom property via `cellStyle`, and the CSS rule
applies the 14% tint. The popup should use the same pattern.

## Files to modify
- `frontend/src/lib/BrokerHealthBadge.svelte`

## Changes

### 1. Import `acctColor` (line ~5, script section)
`acctColor` is not currently imported. Add:
```javascript
import { acctColor } from '$lib/account';
```

### 2. Set CSS custom property in account cellRenderer (line ~95)
After `wrap.className = ...`, add:
```javascript
const _hc = acctColor(p.value);
if (_hc) wrap.style.setProperty('--bh-acct-color', _hc);
```

### 3. Add background-color to `.bh-row-account` CSS rule (line ~259)
```css
:global(.bh-row-account) {
  color: #c8d8f0;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  width: 100%;
  background-color: color-mix(in srgb, var(--bh-acct-color, transparent) 14%, transparent);
}
```

## Agents
- frontend: apply all three changes above to `frontend/src/lib/BrokerHealthBadge.svelte`
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(broker-badge): account cell background tint — color-mix 14% from hash palette, matching Pulse grid pattern

## Done when
- BrokerHealthBadge popup account column shows faint hash-palette background tint per account
- Text color coding (green/amber/red) still applies on top
- svelte-check 0 errors
