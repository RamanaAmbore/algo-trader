# Contrast Audit — 2026-06-30

WCAG 2.1 AA threshold: 4.5:1 for normal text, 3:1 for large/bold (≥18pt regular or ≥14pt bold).
All operator-facing numerics and labels treated as normal text (small size, no exemption).

Dark card bg reference: `linear-gradient(180deg, #1d2a44, #152033)` averaged to `#19253c`.
Algo row bg reference: `linear-gradient(180deg, #273552, #1d2a44)` averaged to `#22304b`.
Log panel bg: `#152033`.
Cream card bg: `#fffdf8`. Cream page bg: `#f0ece3`.

## Dark / Algo Theme

| pair | text | bg | ratio | WCAG AA | action |
|---|---|---|---|---|---|
| --algo-slate (primary) on dark card | `#c8d8f0` | `#19253c` | 10.60 | ✓ | — |
| --algo-muted (secondary) on dark card | `#7e97b8` | `#19253c` | 5.11 | ✓ | — |
| --algo-dim (#94a3b8, tertiary) on dark card | `#94a3b8` | `#19253c` | 5.97 | ✓ | — |
| --algo-amber on dark card | `#fbbf24` | `#19253c` | 9.17 | ✓ | — |
| --algo-green (pos) on dark card | `#4ade80` | `#19253c` | 8.79 | ✓ | — |
| --algo-red (neg) on dark card | `#f87171` | `#19253c` | 5.54 | ✓ | — |
| --algo-cyan on dark card | `#22d3ee` | `#19253c` | 8.47 | ✓ | — |
| --algo-sky on dark card | `#7dd3fc` | `#19253c` | 9.19 | ✓ | — |
| cell-pos (#4ade80) on algo row bg | `#4ade80` | `#22304b` | 7.57 | ✓ | — |
| cell-neg (#f87171) on algo row bg | `#f87171` | `#22304b` | 4.77 | ✓ | — |
| cell-flat (#94a3b8) on algo row bg | `#94a3b8` | `#22304b` | 5.14 | ✓ | — |
| pnl-gain (#4ade80) on odd row bg | `#4ade80` | `#162138` | 9.21 | ✓ | — |
| pnl-loss (#f87171) on odd row bg | `#f87171` | `#162138` | 5.80 | ✓ | — |
| log-info (#e2e8f0) on log panel bg | `#e2e8f0` | `#152033` | 13.24 | ✓ | — |
| log-debug (#94a3b8) on log panel bg | `#94a3b8` | `#152033` | 6.37 | ✓ | — |
| **log-cooldown (#64748b) on log panel bg** | `#64748b` | `#152033` | **3.43** | **✗** | raise to `#7d8fa6` (4.94) |
| log-agent-default (#9ca3af) on log panel bg | `#9ca3af` | `#152033` | 6.43 | ✓ | — |
| log-ts-ist (--algo-slate) on log panel bg | `#c8d8f0` | `#152033` | 11.30 | ✓ | — |
| log-ts-edt (--algo-muted) on log panel bg | `#7e97b8` | `#152033` | 5.45 | ✓ | — |
| amber chip text on amber chip bg | `#fbbf24` | blended | 6.77 | ✓ | — |
| green chip text on green chip bg | `#4ade80` | blended | 7.08 | ✓ | — |
| red chip text on red chip bg | `#f87171` | blended | 4.83 | ✓ | — |
| sky chip text on sky chip bg | `#7dd3fc` | blended | 6.96 | ✓ | — |
| log-agent-chip text on its bg | `#fbbf24` | blended | 6.66 | ✓ | — |
| **cmd-input placeholder (#64748b) on cmd bg** | `#64748b` | `#152033` | **3.43** | **✗** | raise to `#7d8fa6` (4.94) |
| algo-card-title (#94a3b8) on dark card | `#94a3b8` | `#19253c` | 5.97 | ✓ | — |
| algo-tab inactive (#94a3b8) on dark card | `#94a3b8` | `#19253c` | 5.97 | ✓ | — |
| **chase-label off-state (alpha 0.55 blend)** | blended `#79879f` | `#19253c` | **4.21** | **✗** | raise alpha to 0.70 → 5.93 |
| mp-section-label (amber 0.70 blend) on card | blended | `#19253c` | 5.18 | ✓ | — |
| **act-events-hint (alpha 0.55 blend)** | blended `#6e7f9a` | `#19253c` | **3.77** | **✗** | raise alpha to 0.65 → 4.67 |

## Cream / Public Theme

| pair | text | bg | ratio | WCAG AA | action |
|---|---|---|---|---|---|
| pnl-loss (#dc2626) on cream card | `#dc2626` | `#fffdf8` | 4.75 | ✓ | — |
| **pnl-gain (#059669) on cream card** | `#059669` | `#faf8f4` | **3.55** | **✗** | change to `#047a56` (5.05) |
| --card-cell-text (#0c1830) on cream bg | `#0c1830` | `#f0ece3` | 14.98 | ✓ | — |
| --card-currency-text (#4a5872) on cream bg | `#4a5872` | `#f0ece3` | 6.08 | ✓ | — |
| **--card-muted-text (#7a6b52) on cream bg** | `#7a6b52` | `#fffdf8` | **4.40** | **✗** | change to `#736448` (5.67) |
| **--card-label-text (#c8a84b) on cream bg** | `#c8a84b` | `#fffdf8` | **2.26** | **✗** | change to `#7a5e1e` (5.99) |
| **--card-as-of-text (#a89878) on cream bg** | `#a89878` | `#fffdf8` | **2.78** | **✗** | change to `#7a6650` (5.37) |
| --card-gain-text (#1a6b3a) on cream card | `#1a6b3a` | `#fffdf8` | 6.44 | ✓ | — |
| --card-loss-text (#9b1c1c) on cream card | `#9b1c1c` | `#fffdf8` | 8.02 | ✓ | — |
| --card-zero-text (#7a6b52) on cream card | `#7a6b52` | `#fffdf8` | 5.10 | ✓ | — |
| field-label (#5a7090) on white | `#5a7090` | `#ffffff` | 5.05 | ✓ | — |
| **section-heading (#9a7e38) on white** | `#9a7e38` | `#ffffff` | **3.88** | **✗** | change to `#8a6e28` (4.84) |

## Summary of changes

### Dark theme (app.css only — text colors, not card bg or chip bg)
1. `.log-panel .log-agent-cooldown` color: `#64748b` → `#7d8fa6`
2. `.cmd-input::placeholder` color: `#64748b` → `#7d8fa6`
3. `.oes-common-chase-label` off-state: `rgba(200,216,240,0.55)` → `rgba(200,216,240,0.70)`
4. `.act-events-hint` color: `rgba(180,200,230,0.55)` → `rgba(180,200,230,0.65)`

### Cream theme (app.css card-theme-cream token block + ag-theme-ramboq pnl)
5. `.card-theme-cream --card-label-text`: `#c8a84b` → `#7a5e1e`
6. `.card-theme-cream --card-as-of-text`: `#a89878` → `#7a6650`
7. `.card-theme-cream --card-muted-text`: `#7a6b52` → `#736448`
8. `.ag-theme-ramboq .pnl-gain` color: `#059669` → `#047a56`
9. `.section-heading` color (app.css @layer components): `#9a7e38` → `#8a6e28`
