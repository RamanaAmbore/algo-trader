# Plan: AlgoTimestamp component + layout TODO comment

## Task
Create `frontend/src/lib/AlgoTimestamp.svelte` using the exact Svelte 5 code provided.
Add a `/* TODO: remove after AlgoTimestamp migration (Pass C) … */` comment above the
`.algo-ts` / `.algo-ts-group` / `.algo-ts-hidden` / `.algo-ts-data` / `.algo-ts-vsep`
CSS rules in `frontend/src/routes/(algo)/+layout.svelte`.
Leave `formatIstOnly` in `stores.js` untouched.

## Agents
- frontend: Create `AlgoTimestamp.svelte` with exact code supplied. Add TODO comment in layout CSS.
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
feat(ui): add AlgoTimestamp component + layout TODO migration marker

## Done when
- `frontend/src/lib/AlgoTimestamp.svelte` exists with the exact Svelte 5 store-bridge code
- `+layout.svelte` has the TODO comment above the `.algo-ts` block (line ~2083)
- `formatIstOnly` in `stores.js` is untouched
- `svelte-check` passes
