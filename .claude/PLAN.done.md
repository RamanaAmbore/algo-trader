# Plan: AlgoTimestamp — toggle reliability, animation, pointer detection

## Context

Audit of `AlgoTimestamp.svelte` (page-header dual-timezone clock) found three bugs confirmed by Playwright tests and user reports ("toggle sometimes works, sometimes not; no animation; desktop takes time"):

| Bug | Root cause |
|---|---|
| Mobile toggle intermittent | `ontouchend` + `onclick` both wired → double-fire on Android Chrome 120+; `e.preventDefault()` on touchend is inconsistent with `touch-action: manipulation` → toggle fires twice → net no-op |
| Toggle dead on iPad / hybrid devices | `@media (hover: none) and (pointer: coarse)` fails for devices reporting `pointer: fine` (iPads, Samsung DeX, hybrid laptops, desktop-mode browsers) → `pointer-events: none` stays on button → no events ever reach it |
| No animation | `display: none` is instant; no CSS transition on toggle |

Three test/config files were already fixed in this session (not yet committed):
- `playwright.config.js` — `hasTouch: true` added to both mobile projects
- `e2e/global-setup.js` — skip re-fetch if cached token < 20h old (avoids 429 on rapid reruns)
- `e2e/algo-timestamp.spec.js` — full rewrite: correct selector `.ats-group`, 15 tests, all 8 mobile tests passing

## Agents

- frontend: Fix `frontend/src/lib/AlgoTimestamp.svelte`:
  1. **Remove double-fire**: Delete `ontouchend={_handleTap}` attribute and the `_handleTap` function. Keep only `onclick={_toggle}`. The existing `touch-action: manipulation` inline style already gives click-without-delay on all modern mobile browsers — the touchend handler is redundant and harmful.
  2. **Fix pointer detection**: Replace `@media (hover: none) and (pointer: coarse)` with `@media (max-width: 640px)`. Move ALL mobile overrides (pointer-events: auto, cursor: pointer, font-size: 0.6rem, min-height: 1.8rem, .ats-sep hide, .ats-mobile-hide) under the width query. Width-based detection is unconditionally reliable across all device types.
  3. **Add toggle animation**: Replace `display: none` in `.ats-mobile-hide` with `opacity: 0; pointer-events: none; transition: opacity 0.15s ease`. Add `opacity: 1; transition: opacity 0.15s ease` to the visible state (`.ats-now` and `.ats-refresh` inside the media query).
  4. **Remove `lang="ts"`** from `<script>` — it was added for `_handleTap(e: TouchEvent)` typing; after removing that function there is no TS syntax remaining.

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Run `e2e/algo-timestamp.spec.js --project=mobile-portrait` to confirm 8 passed / 7 skipped / 0 failed.

## Files to change

| File | Change |
|---|---|
| `frontend/src/lib/AlgoTimestamp.svelte` | Remove ontouchend, swap media query, add opacity transition |
| `frontend/playwright.config.js` | Already done — `hasTouch: true` on mobile projects |
| `frontend/e2e/global-setup.js` | Already done — token cache reuse |
| `frontend/e2e/algo-timestamp.spec.js` | Already done — full rewrite with 15 tests |

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes (algo-timestamp.spec.js, mobile-portrait)

## Commit message

fix(ui): AlgoTimestamp — remove ontouchend double-fire, width-based pointer detection, opacity toggle transition

## Done when

1. Single tap on mobile always toggles once (no net no-op from double-fire)
2. Toggle works on iPad / Samsung DeX / hybrid laptops (not blocked by `pointer: fine`)
3. Toggle shows a smooth 150ms fade instead of a hard blink
4. svelte-check 0 errors
5. Playwright: 8 passed / 7 skipped / 0 failed on mobile-portrait
