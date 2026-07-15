# Plan: Showcase hint centering + demo strip color + Pulse padding alignment

## Context

Three small UX/layout fixes across the frontend:

1. **Showcase tour hint**: After making `.show-cta-tour` `width: 100%`, the keyboard hint (`←/→ Space Esc`) wraps below the button but is left-aligned. User wants it centered.

2. **Demo mode strip color**: Current background is `#1e0a3c` (deep purple) with `rgba(168,85,247,0.35)` border — purple is jarring vs the app's amber/slate palette. User wants to assess if it should change. Proposing amber-aligned alternative: `rgba(30, 18, 0, 0.97)` background + `rgba(251, 191, 36, 0.40)` border + amber text `#fbbf24` / `#fcd34d` — consistent with `var(--c-action)`.

3. **Pulse vs Dashboard horizontal padding mismatch**: `.algo-content` gives 0.5rem side padding to all pages. But `.mp-flat-wrap` (the pulse page wrapper) adds another `0 0.4rem 0.4rem` — making pulse content 0.4rem more inset than dashboard. The demo strip renders at layout level (inside `.algo-content` directly), so it sits at 0.5rem from edges while the pulse grid sits at 0.9rem. Fix: remove the side padding from `.mp-flat-wrap`.

---

## Files to Modify

### 1. `frontend/src/routes/(algo)/showcase/+page.svelte`

`.show-cta-row` (line 540–545) — change to column layout so button fills width and hint centers below:

```css
.show-cta-row {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
}
```

### 2. `frontend/src/routes/(algo)/+layout.svelte`

`.demo-banner` (line 2572–2585) — change from purple to amber:
- `background: #1e0a3c` → `background: rgba(30, 18, 0, 0.97)`
- `border-bottom: 1px solid rgba(168,85,247,0.35)` → `border-bottom: 1px solid rgba(251, 191, 36, 0.40)`
- `.demo-banner-text` `color: #d8b4fe` → `color: #fbbf24`
- `.demo-banner-text strong` `color: #e9d5ff` → `color: #fcd34d`
- `.demo-banner-close` `color: rgba(168,85,247,0.6)` → `color: rgba(251, 191, 36, 0.55)`
- `.demo-banner-close:hover` `color: #c084fc` → `color: #fbbf24`

### 3. `frontend/src/lib/MarketPulse.svelte`

`.mp-flat-wrap` (line 4861–4878) — remove side padding:
- `padding: 0 0.4rem 0.4rem` → `padding: 0 0 0.4rem`

(Mobile override `padding: 0 0 0.3rem` already has zero sides — no change needed there.)

---

## Agents
- frontend: Make all three CSS changes in showcase/+page.svelte, +layout.svelte, and MarketPulse.svelte. Run svelte-check after.
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
fix(ui): center tour hint; amber demo strip; remove extra pulse side padding

## Done when
- `←/→ Space Esc` hint is centered below the tour button on showcase page.
- Demo strip shows amber tones (consistent with app palette) instead of purple.
- Pulse page content left/right edge aligns with dashboard (0.5rem, not 0.9rem).
- svelte-check 0 errors.
