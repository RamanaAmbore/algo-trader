# Plan: UI consistency sweep — card headers, pulse timestamp, activity buttons, ntfy deploy

## Context

Several inconsistencies surfaced after the single-row card header refactor: (1) Gainers/Losers
card headers have timestamp text in the LEFT zone polluting the label|sep|tabs pattern.
(2) Activity panel button group is missing Search in the new label-based path. (3) ActivityHeaderFilters
is right-aligned via margin-left:auto instead of sitting naturally left of the button group.
(4) Payoff and Snapshot sections use bespoke header divs instead of CardHeader — no separator.
(5) Snapshot AccountMultiSelect options are empty. (6) Showcase bio still uses wrong colors.
(7) ntfy deploy alert is gated inside Telegram's if resp.ok: block — should fire independently.

---

## Task

Fix 7 groups of issues across 7 frontend files and 1 backend file.

---

## Agents

### frontend
Files: MarketPulse.svelte, LogPanel.svelte, ActivityHeaderFilters.svelte,
       pulse/+page.svelte, dashboard/+page.svelte,
       derivatives/+page.svelte, showcase/+page.svelte

**A — MarketPulse.svelte (`frontend/src/lib/MarketPulse.svelte`)**

A1 — Remove timestamp from Gainers and Losers LEFT snippets.
Currently (Gainers, lines 3993-3997):
```svelte
{#snippet left()}
  <span class="mp-bucket-label mp-bucket-label-winners">Gainers</span>
  {#if _moversAsOf}
    <span class="mp-movers-as-of">Last updated: {_moversAsOf}</span>
  {/if}
{/snippet}
```
After: just `<span class="mp-bucket-label mp-bucket-label-winners">Gainers</span>`.
Same for Losers (lines 4029-4033).

A2 — Add `moversAsOf = $bindable(null)` prop so parent pages can receive the derived value.
Add a `$effect(() => { moversAsOf = _moversAsOf; })` so the parent gets updates reactively.

**B — pulse/+page.svelte (`frontend/src/routes/(algo)/pulse/+page.svelte`)**

B1 — Add `let _moversAsOf = $state(null)` state.
Add `bind:moversAsOf={_moversAsOf}` to the `<MarketPulse>` call.

B2 — In the page-header, replace the bare `<span class="algo-ts">{$nowStamp}</span>` with a
combined timestamp display that shows both the data snapshot time and the live clock:

```svelte
<span class="algo-ts-group">
  {#if _moversAsOf}
    <span class="algo-ts algo-ts-data" class:algo-ts-hidden={_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Data as-of — tap to switch">
      {_moversAsOf}
    </span>
    <span class="algo-ts-vsep" aria-hidden="true">|</span>
  {/if}
  <span class="algo-ts" class:algo-ts-hidden={_moversAsOf && !_showLiveTs}
        onclick={() => { if (_moversAsOf) _showLiveTs = !_showLiveTs; }}
        title={_moversAsOf ? 'Live clock — tap to switch' : undefined}>
    {$nowStamp}
  </span>
</span>
```

On desktop both show (data-ts | nowStamp). On mobile (<480px) only ONE shows at a time
(`algo-ts-hidden` hides the other); `_showLiveTs = $state(false)` defaults to data timestamp.
Add to `<style>`:
```css
.algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
.algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
.algo-ts-data  { cursor: pointer; }
@media (max-width: 480px) {
  .algo-ts-hidden { display: none; }
  .algo-ts-data   { cursor: pointer; }
}
```
Also add `let _showLiveTs = $state(false)` to script.

B3 — Format `_moversAsOf` consistently: The MarketPulse derived value already uses
`"DD Mon HH:MM IST"` format. Keep that format — it's distinct enough from nowStamp to
be readable without being identical.

**C — dashboard/+page.svelte (`frontend/src/routes/(algo)/dashboard/+page.svelte`)**

C1 — Find the `<MarketPulse>` call. Add `bind:moversAsOf={_moversAsOf}` (create
`let _moversAsOf = $state(null)` in script).

C2 — Find the dashboard page-header's `<span class="algo-ts">` and apply the same
`algo-ts-group` pattern as B2 above (same CSS, same toggle logic).

**D — LogPanel.svelte (`frontend/src/lib/LogPanel.svelte`)**

D1 — Add Search toggle button to `.lp-card-btns` (the new label-based button section).
The `_searchOpen` and search SVG already exist in the legacy `.lp-card-btns-legacy` block.
Add the SAME search button to the NEW `.lp-card-btns` block, BEFORE the CollapseButton.
Button template (copy from legacy block lines 1443-1453):
```svelte
<button type="button"
  class="lp-card-btn {_searchOpen ? 'lp-card-btn-on' : ''}"
  title={_searchOpen ? 'Close search' : 'Search rows'}
  aria-label="Search rows"
  aria-pressed={_searchOpen}
  onclick={() => { _searchOpen = !_searchOpen; if (!_searchOpen) _searchQuery = ''; }}>
  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.6"/>
    <path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
  </svg>
</button>
```
Place it as the FIRST item inside `.lp-card-btns` (before the context-conditional block).
The `_searchOpen` and `_searchQuery` states already exist in LogPanel — this just exposes
the toggle in the new card button group.

D2 — Tighten `.lp-sep` to match ch-sep: change the margin from
`margin: 0.15rem 0.25rem 0.15rem 0.5rem` to `margin: 0.15rem 0.35rem`.
(Keeps small horizontal breathing room without the overly large 0.5rem left gap.)

**E — ActivityHeaderFilters.svelte (`frontend/src/lib/ActivityHeaderFilters.svelte`)**

E1 — Remove `margin-left: auto` from `.act-filters` rule (line 91).
With `lp-tab-strip-wrap` taking `flex: 1 1 0`, the filters sit naturally after the tab strip
without needing the auto-margin push. The auto-margin was left over from when filters lived
in a parent card header without a flex:1 middle zone.

**F — derivatives/+page.svelte (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`)**

F1 — Payoff section: Replace the bespoke `<div class="opt-section-h opt-section-h-grid">`
header with `<CardHeader>`. The chips block becomes the `middle` snippet; CardControls
props are forwarded via CardHeader's built-in `showControls`.

Replace (lines 4053-4125):
```svelte
<div class="opt-section-h opt-section-h-grid">
  <div class="opt-section-row">
    <span class="opt-section-title">Payoff</span>
    <div class="opt-section-chips">...</div>
    <span class="payoff-card-controls"><CardControls .../></span>
  </div>
</div>
```
With:
```svelte
<CardHeader
  title="Payoff"
  bind:isCollapsed={_colPayoff}
  bind:isFullscreen={_fsPayoff}
  cardId="optPayoff"
  label="Payoff"
  onRefresh={_refreshAll}
  bind:refreshLoading={_refreshing}
  showSearch={false}
>
  {#snippet middle()}
    <div class="opt-section-chips">
      <!-- the EV + Greeks chips — copy exact chip markup here unchanged -->
    </div>
  {/snippet}
</CardHeader>
```
Add `import CardHeader from '$lib/CardHeader.svelte';` if not already imported.
The `.ch-sep` will automatically appear between "Payoff" and the chips because `middle`
snippet is provided. Remove the `.opt-section-h`, `.opt-section-row`, `.opt-section-title`,
and `.payoff-card-controls` CSS rules that are now unused for Payoff.

F2 — Snapshot section: Replace the bespoke `<div class="bucket-header">` with `<CardHeader>`.
The AccountMultiSelect + StrategyPicker become the `middle` snippet.

Replace (lines 4446-4518):
```svelte
<div class="bucket-header">
  <span class="opt-section-h" style="padding-bottom:0">
    <span class="algo-card-title" style="margin-bottom:0">Snapshot</span>
    <AccountMultiSelect .../>
    <StrategyPicker .../>
  </span>
  <span class="payoff-card-controls"><CardControls .../></span>
</div>
```
With:
```svelte
<CardHeader
  title="Snapshot"
  bind:isCollapsed={_colByund}
  bind:isFullscreen={_fsByund}
  bind:filter={_filterByund}
  cardId="optByund"
  label="Snapshot"
  onRefresh={_refreshAll}
  bind:refreshLoading={_refreshing}
  onDownload={() => { ...existing download logic... }}
>
  {#snippet middle()}
    <AccountMultiSelect
      bind:value={selectedAccounts}
      options={accountChoices.map(a => ({ value: a, label: a }))}
      placeholder="All accounts"
      ariaLabel="Filter Snapshot by broker account" />
    <StrategyPicker label="Strategy" />
  {/snippet}
</CardHeader>
```

F3 — Fix Snapshot account list: The `accountChoices` derivation already includes
`realAccounts` as a fallback. If it still shows empty, the issue is that `AccountMultiSelect`
may cache options at mount time. Wrap with `{#key accountChoices.length}` to force
re-mount when options populate:
```svelte
{#snippet middle()}
  {#key accountChoices.length}
    <AccountMultiSelect ... options={accountChoices.map(...)} />
  {/key}
  <StrategyPicker label="Strategy" />
{/snippet}
```

F4 — ag-grid row stripe consistency: Snapshot uses `.byund-row:nth-of-type(odd)` and
Legs uses `.cand-grid > :nth-child(even)`. Both effectively stripe data rows 1,3,5.
Verify visually during svelte-check — if they look different, standardize the Legs
selector to match Snapshot's `:nth-of-type` approach.

**G — showcase/+page.svelte (`frontend/src/routes/(algo)/showcase/+page.svelte`)**

G1 — Change `.show-attribution` background and border to match the risk engine button colors.
The risk engine card uses `--accent: #7dd3fc` (sky blue) with `.show-card-link` at
`color-mix(in srgb, var(--accent) 45%, transparent)` border and
`color-mix(in srgb, var(--accent) 14%, transparent)` background on hover.

Change `.show-attribution` (lines 449-450) from amber to sky blue:
```css
/* BEFORE */
background: color-mix(in srgb, var(--c-action, #fbbf24) 12%, transparent);
border: 1px solid color-mix(in srgb, var(--c-action, #fbbf24) 35%, transparent);

/* AFTER */
background: color-mix(in srgb, #7dd3fc 14%, transparent);
border: 1px solid color-mix(in srgb, #7dd3fc 45%, transparent);
```

---

### backend
File: `webhook/notify_deploy.py`

H1 — ntfy deploy alert: Currently the ntfy send is nested inside `if resp.ok:` (only fires
when Telegram succeeds). Move it to run independently, AFTER the Telegram block.

Current structure (lines 119-150):
```python
if token and chat_id:
    try:
        resp = requests.post(...)
        if resp.ok:
            print("notify_deploy: Telegram sent")
            ntfy_topic = sec.get("ntfy_topic")   # ← NESTED — only runs on Telegram success
            if ntfy_topic:
                ...send ntfy...
        else:
            errors.append(...)
    except Exception as e:
        errors.append(...)
```

After (ntfy at same indentation level as Telegram block):
```python
if token and chat_id:
    try:
        resp = requests.post(...)
        if resp.ok:
            print("notify_deploy: Telegram sent")
        else:
            errors.append(...)
    except Exception as e:
        errors.append(...)

# ntfy — runs independently of Telegram
ntfy_topic = sec.get("ntfy_topic")
if ntfy_topic:
    ntfy_url = sec.get("ntfy_url", "https://ntfy.sh")
    try:
        import urllib.request as _urlreq
        req = _urlreq.Request(
            f"{ntfy_url.rstrip('/')}/{ntfy_topic}",
            data=detail_line.encode(),
            headers={"Title": event_label, "Tags": "rocket", "Content-Type": "text/plain"},
            method="POST",
        )
        _urlreq.urlopen(req, timeout=5)
        print("notify_deploy: ntfy sent")
    except Exception as e:
        errors.append(f"ntfy: {e}")
```
Note: use urllib.request (not requests/httpx) for IPv6 compatibility on prod server.

---

### playwright
File: `frontend/e2e/activity-panel.spec.ts`

Update the activity panel spec to assert the new Search button is present in the card
button group when the panel is in card mode (has label). The button has `aria-label="Search rows"`.
Update `assertButtonGroupCard()` helper to include a check for this button.

---

### broker: skip
### doc: skip
### backend-test: skip

---

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

---

## Commit message
```
fix(ui): card header consistency, pulse timestamp lift, activity search btn, ntfy deploy
```

---

## Done when
1. Gainers/Losers card headers show `[label] | [sep] | [tabs] | [controls]` — no timestamp inside card header
2. Pulse + Dashboard page-headers show data-as-of timestamp next to nowStamp with `|` separator; on mobile one shows at a time, clicking toggles
3. Activity panel (all surfaces) shows 4 buttons in card mode: Search + Collapse + Fullscreen + Download
4. ActivityHeaderFilters dropdowns sit left-aligned after the tab strip (no right-push from margin-left:auto)
5. Payoff section header: `[Payoff] | [sep] | [EV Δ Γ Θ 𝒱 ρ chips] | [controls]`
6. Snapshot section header: `[Snapshot] | [sep] | [AccountMultiSelect + StrategyPicker] | [controls]`
7. Snapshot AccountMultiSelect dropdown populates with broker accounts on page load
8. Showcase bio panel: sky-blue tint (`#7dd3fc` at 14% bg, 45% border) not amber
9. notify_deploy.py sends ntfy independently of Telegram success/failure
10. svelte-check 0 errors; Playwright activity-panel spec passes
