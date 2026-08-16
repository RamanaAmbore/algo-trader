# Plan: Fix navbar text visibility — algo pages + public Rambo Terminal button

## Context

Two separate visibility issues on desktop:

### Issue 1 — Algo pages navbar items (`.algo-nav-btn`)
File: `frontend/src/routes/(algo)/+layout.svelte` (lines ~1552–1590)

`color: rgba(180, 200, 230, 0.75)` — only 75% opacity muted blue-gray. On the dark
algo app background this is noticeably dim. Font-size is `var(--fs-md)` (compact).

Fix: raise opacity to ~92%, slightly brighter base color. **Desktop only** — `.algo-nav-btn`
has no separate mobile CSS class, so the change must be wrapped in `@media (min-width: 768px)`
to avoid affecting the mobile navbar.

### Issue 2 — Public page "Rambo Terminal ↗" button (`.pub-nav-algo-btn`)
File: `frontend/src/routes/(public)/+layout.svelte` (lines ~432–451)

`color: #b27908` = dark amber-brown, ~2.5:1 contrast against `#0c1830` navy — WCAG
fail, barely readable. `border: rgba(200,168,75,0.32)` = invisible. `font-size: 0.88rem`
= small.

Fix: bright gold text, stronger border, slightly larger font. **Already desktop-only** —
mobile uses a separate class `.pub-mobile-algo` with separate HTML element (Tailwind
`hidden md:flex` controls visibility). Changes to `.pub-nav-algo-btn` never affect mobile.

---

## Files to change

- `frontend/src/routes/(algo)/+layout.svelte` — `.algo-nav-btn` CSS
- `frontend/src/routes/(public)/+layout.svelte` — `.pub-nav-algo-btn` CSS

---

## Detailed changes

### algo/+layout.svelte — `.algo-nav-btn` (desktop only via media query)

Leave the existing top-level rule untouched (mobile keeps `rgba(180, 200, 230, 0.75)`).
Add a desktop override after the existing rule block:

```css
/* ADDED — desktop-only brightness boost */
@media (min-width: 768px) {
  :global(.algo-nav-btn) {
    color: rgba(200, 218, 242, 0.92);
  }
}
```

That single change — from 75% to 92% opacity and a slightly cooler/brighter blue-white — will significantly improve readability without changing the visual design language. Mobile is unaffected.

### public/+layout.svelte — `.pub-nav-algo-btn` default state

```css
/* BEFORE */
font-size: 0.88rem;
font-weight: 500;
color: #b27908;
border: 1px solid rgba(200,168,75,0.32);
background: rgba(200,168,75,0.10);

/* AFTER */
font-size: 0.95rem;
font-weight: 600;
color: #e8c03a;              /* bright gold — clearly visible on dark navy */
border: 1px solid rgba(200,168,75,0.60);   /* visible border */
background: rgba(200,168,75,0.15);          /* slightly more filled */
```

### public/+layout.svelte — `.pub-nav-algo-btn:hover`

```css
/* BEFORE */
color: #b27908;   /* same dark amber — no change on hover */

/* AFTER */
color: #f0d070;   /* brighter gold on hover */
border-color: rgba(200,168,75,0.75);
background: rgba(200,168,75,0.22);
```

---

## Agents
- frontend: apply both CSS changes above
- backend: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(public-algo-nav): improve navbar text opacity and Rambo Terminal button contrast on desktop

## Done when
- Algo pages nav items clearly readable (0.92 opacity blue-white vs 0.75 before)
- Rambo Terminal button shows bright gold (#e8c03a) not dark amber (#b27908)
- svelte-check 0 errors
