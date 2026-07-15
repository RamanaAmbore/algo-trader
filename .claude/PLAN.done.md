# Plan: Demo strip — slate card color

## Context

Amber background on demo strip (`rgba(30, 18, 0, 0.97)`) is too heavy. User wants a within-palette alternative. Proposal: use the standard app card dark background with slate border and muted slate text — only the "Demo mode" strong label stays amber for attention. This makes the strip blend with the dark UI while still being readable.

---

## Files to Modify

### `frontend/src/routes/(algo)/+layout.svelte`

Change demo banner colors (around line 2572–2599):

| Selector | Property | Old | New |
|---|---|---|---|
| `.demo-banner` | `background` | `rgba(30, 18, 0, 0.97)` | `rgba(15, 23, 42, 0.97)` |
| `.demo-banner` | `border-bottom` | `1px solid rgba(251, 191, 36, 0.40)` | `1px solid rgba(126, 151, 184, 0.30)` |
| `.demo-banner-text` | `color` | `#fbbf24` | `rgba(148, 163, 184, 0.85)` |
| `.demo-banner-text strong` | `color` | `#fcd34d` | `#fbbf24` |
| `.demo-banner-close` | `color` | `rgba(251, 191, 36, 0.55)` | `rgba(148, 163, 184, 0.50)` |
| `.demo-banner-close:hover` | `color` | `#fbbf24` | `rgba(148, 163, 184, 0.90)` |

---

## Agents
- frontend: Apply the six color substitutions in +layout.svelte `.demo-banner` CSS block. Run svelte-check after.
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
fix(ui): demo strip — slate card color with amber Demo mode label

## Done when
- Demo strip uses dark slate background matching app card style.
- "Demo mode" label pops in amber; remaining text is muted slate.
- svelte-check 0 errors.
