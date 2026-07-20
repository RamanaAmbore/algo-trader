# Plan: Frontend CSS SSOT — fs-lg token fix + weak border cleanup

## Context
After Round 8, two systematic drift patterns remain across the frontend:
1. `--fs-lg: 0.7rem` (defined in app.css) is used in 20+ components for body text. Grid SSOT is 0.72rem. Fix the token itself — one line, all consumers align automatically.
2. Eight locations still use `rgba(255,255,255,0.05)` / Tailwind `border-white/5` as section-divider borders, weaker than the canonical `rgba(126,151,184,0.10)`.

Component extraction from SymbolPanel (1,753 scoped CSS lines) and derivatives (1,674) is deferred — high risk, needs its own focused plan.

---

## Agents

- backend: skip

- frontend: Two targeted changes only.

  **1. Fix `--fs-lg` token — `frontend/src/app.css`**
  Find line 202: `--fs-lg:  0.7rem;`
  Change to: `--fs-lg:  0.72rem;`
  This single change propagates to all 20+ consumers (ConfirmModal, EmptyState, PnlPanel, NavTab, UnifiedLog, RefreshButton, AgentToast, PerformancePage ×4, PositionStrip, NavCard, CommandBar, ChartModal, DayPnlBreakup, LogPanel before Round 8, agent-templates before Round 8) without touching individual files.

  **2. Fix weak border dividers — 5 files**
  Replace `rgba(255,255,255,0.05)` / `border-white/5` → `rgba(126,151,184,0.10)` in:

  - `frontend/src/routes/(algo)/+layout.svelte` line ~1806: `.settings-row { border-bottom: 1px solid rgba(255,255,255,0.05); }` → `rgba(126,151,184,0.10)`
  - `frontend/src/routes/(algo)/admin/settings/+page.svelte`:
    - Line ~414: `border-t border-white/5` → change Tailwind class to `border-t` and add inline `style="border-color: rgba(126,151,184,0.10)"` or replace with a scoped CSS rule `.section-divider { border-top: 1px solid rgba(126,151,184,0.10); }`
    - Line ~461: `<tr class="border-t border-white/5">` → same treatment
    - Line ~509: `border-bottom: 1px solid rgba(255,255,255,0.05);` in scoped CSS → `rgba(126,151,184,0.10)`
  - `frontend/src/routes/(algo)/admin/health/+page.svelte`:
    - Line ~598 and ~659: `border-top: 1px solid rgba(255,255,255,0.05)` → `rgba(126,151,184,0.10)`
  - `frontend/src/routes/(algo)/admin/tokens/+page.svelte` line ~466: `border-t border-white/5` → same treatment as settings
  - `frontend/src/routes/(algo)/automation/+page.svelte` lines ~764, ~1031, ~1102: `border-t border-white/5` → same treatment

  For Tailwind `border-white/5` replacements: prefer removing the Tailwind class and adding a scoped CSS rule with the canonical value rather than inline styles. If the element already has a scoped class, add border to that rule. If not, add a utility class or use the existing parent rule.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): fs-lg token 0.7→0.72rem + weak border dividers → canonical rgba(126,151,184,0.10)

## Done when
svelte-check 0 errors. `--fs-lg` resolves to 0.72rem. All 8 weak-border locations use `rgba(126,151,184,0.10)`. No `rgba(255,255,255,0.05)` or `border-white/5` remain as structural dividers.
