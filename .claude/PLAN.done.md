# Plan: Prevent state_unsafe_mutation — detection + enforcement framework

## Context

`state_unsafe_mutation` is Svelte 5's runtime error when reactive state (SvelteMap,
`$state`, writable stores) is written while a `$derived` computation is evaluating.
The codebase has 195 `untrack()` fixes and a `liveSnap()` helper, but `svelte.config.js`
globally suppresses all `state_referenced_locally` compiler warnings — meaning new
violations are INVISIBLE at build time and only surface as runtime crashes.

Three things are needed:
1. A canonical `safeRead()` utility that makes the correct pattern trivially easy
2. Detection in CI that surfaces dangerous patterns before runtime
3. Remove the global suppression so `svelte-check` catches new violations at build time

---

## Root cause pattern

```js
// DANGEROUS — triggers state_unsafe_mutation at runtime:
const total = $derived(get(positionsStore).reduce(...));
//                     ^^^ store read inside $derived, no untrack

// SAFE:
const total = $derived(untrack(() => get(positionsStore)).reduce(...));
// OR use safeRead():
const total = $derived(safeRead(positionsStore).reduce(...));
```

---

## Changes

### 1. `frontend/src/lib/utils/safeRead.js` (new file)
Single canonical utility that wraps `get()` in `untrack()`:

```js
import { untrack } from 'svelte';
import { get } from 'svelte/store';

/**
 * Read a Svelte store safely inside $derived / $derived.by().
 * Wraps get() in untrack() — the store's reactive write path
 * cannot trigger state_unsafe_mutation during derivation.
 * @template T
 * @param {import('svelte/store').Readable<T>} store
 * @returns {T}
 */
export function safeRead(store) {
  return untrack(() => get(store));
}
```

Use this anywhere `get(someStore)` appears inside a `$derived` expression.
Existing `untrack(() => get(...))` calls can be migrated to `safeRead()` over time.

### 2. `frontend/svelte.config.js` — remove global suppression

Current (masks ALL state_referenced_locally warnings):
```js
onwarn: (warning, handler) => {
  if (warning.code.startsWith('a11y_') || warning.code === 'state_referenced_locally') return;
  handler(warning);
},
```

New (a11y still suppressed, but reactive warnings now surface):
```js
onwarn: (warning, handler) => {
  if (warning.code.startsWith('a11y_')) return;
  handler(warning);
},
```

After this change, `svelte-check` will surface all 24 existing `state_referenced_locally`
sites. The frontend agent must add per-line `// svelte-ignore state_referenced_locally`
with a short justification comment at each of the 24 known-safe sites before committing.

Known sites (24 total):
- OrderTicket.svelte: lines 541, 559, 572, 585, 591, 603, 698, 757, 767, 769, 973, 980
- SymbolPanel.svelte: lines 249, 416, 584, 595, 835, 1141, 1246, 1649
- CommandBar.svelte: line 69
- LogPanel.svelte: line 201
- OptionChainTab.svelte: line 184
- InfoHint.svelte: line 62

Pattern for each suppression comment:
```js
// svelte-ignore state_referenced_locally -- captures snapshot at init for change detection
```

### 3. `scripts/check-unsafe-reactive.sh` (new file)
Pre-commit detection script to catch `get(` inside `$derived` without `untrack`:

```bash
#!/usr/bin/env bash
# Detect bare get() calls inside $derived blocks without untrack wrapping.
# Exits non-zero if any violations found.
set -euo pipefail

VIOLATIONS=$(grep -rn \
  --include="*.svelte" --include="*.svelte.js" \
  -E '\$derived[^;{]*get\(' \
  frontend/src/ | grep -v 'untrack\|safeRead\|// safe' || true)

if [[ -n "$VIOLATIONS" ]]; then
  echo "❌ state_unsafe_mutation risk: bare get() inside \$derived without untrack/safeRead:"
  echo "$VIOLATIONS"
  exit 1
fi
echo "✓ no unsafe reactive reads detected"
```

Wire into `/ddev` gate (add before push step, non-blocking first run until codebase is clean).

### 4. CLAUDE.md — add to Key Patterns section

```markdown
**Reactive safety (state_unsafe_mutation prevention)** — Never call `get(store)` directly
inside `$derived(...)`. Always wrap in `untrack()` or use `safeRead(store)` from
`frontend/src/lib/utils/safeRead.js`. For symbol data, use `liveSnap(sym)` from
`symbolStore.svelte.js`. The `state_referenced_locally` compiler warning surfaces these
at build time — do NOT suppress it globally; add per-line `svelte-ignore` with a justification.
```

---

## Agents

- frontend: create `safeRead.js`, update `svelte.config.js`, add 24 per-line svelte-ignore comments
- doc: add reactive safety pattern to CLAUDE.md Key Patterns
- backend: skip
- backend-test: skip

## Tests

- svelte-check: yes — must exit 0 after svelte.config.js change (all 24 sites annotated)
- vitest: yes — 968 still pass
- Check: `bash scripts/check-unsafe-reactive.sh` exits 0

## Commit message

feat(frontend): safeRead() utility + svelte.config state_referenced_locally enforcement

## Done when

- `safeRead.js` exists and is documented
- svelte.config.js no longer globally suppresses `state_referenced_locally`
- All 24 existing suppressions have per-line `svelte-ignore` with justification comments
- `npx svelte-check --output machine` exits 0 errors
- Detection script passes (0 violations found)
