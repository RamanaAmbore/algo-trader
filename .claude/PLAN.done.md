# Plan: Payoff overlay — spot % change row

## Context

The `OptionsPayoff.svelte` overlay already has:
- `SPOT` row (line 732–739): color-coded green/red via `ps-spot-{spotDir}` — derived from
  `spot > prevClose` / `spot < prevClose` logic (lines 144–149). Already correct.
- `CLOSE` row (line 741–749): shows `prevClose` in neutral cyan.
- `prevClose` is already a prop (line 92), passed from page as `strategy?.spot_prev_close`.

Missing: a `CHG%` row showing `(spot − prevClose) / prevClose × 100`, color-coded with
the same `spotDir` palette (green positive, red negative, cyan flat/unavailable).

## Task

Add one derived variable and one template row to `OptionsPayoff.svelte`:

1. **Derived `spotPct`** — compute % change from prevClose. Place after `spotDir` (line 149):
```javascript
const spotPct = $derived.by(() => {
  if (spot == null || prevClose == null || prevClose <= 0) return null;
  return ((spot - prevClose) / prevClose) * 100;
});
```

2. **Template row** — insert between the SPOT row (line 739) and the CLOSE row (line 741):
```svelte
{#if spotPct != null}
  <div class="ps-row" title="Spot % change from previous session close">
    <span class="ps-k">CHG%</span>
    <span class={'ps-v ps-spot-' + spotDir}>
      {spotPct >= 0 ? '+' : ''}{spotPct.toFixed(2)}%
    </span>
  </div>
{/if}
```

Color reuses `ps-spot-{spotDir}` — no new CSS needed. The `+` prefix on positive values
matches Bloomberg/Kite convention. `toFixed(2)` gives two decimal places.

## Agents

- frontend: make the two edits above in `frontend/src/lib/OptionsPayoff.svelte`
- backend: skip
- backend-test: skip
- playwright: skip
- doc: skip

## Files touched

- `frontend/src/lib/OptionsPayoff.svelte`

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

feat(derivatives): payoff overlay spot % change from prev close with color coding

## Done when

1. `svelte-check` — 0 errors
2. Overlay shows CHG% row between SPOT and CLOSE, green when positive, red when negative,
   cyan when prevClose unavailable
