---
name: 6-Dimension Opus Audit
description: Dispatch an Opus audit agent to run a 6-dimension quality audit on recent commits. D1=Correctness D2=Performance D3=Dead Code D4=UX D5=Broker Compliance D6=Doc Alignment. Use weekly or after a sprint of changes.
agent: audit
model: opus
---

Run a 6-dimension audit of all RamboQuant changes since yesterday.

## Commits to audit

!`cd /Users/ramanambore/projects/ramboq && git log --since="24 hours ago" --oneline --no-merges`

For each commit, audit across all six dimensions:
- **D1 Correctness**: logic bugs, off-by-one, missing guards, unhandled exceptions, wrong math
- **D2 Performance**: sync calls in async context, N+1 queries, unbounded loops, missing caches
- **D3 Dead Code**: unused imports, dead branches, commented blocks, stale variables, unreachable paths
- **D4 UX Consistency**: palette violations (check DESIGN_GUIDE color codes), density inconsistencies, missing component reuse, wrong grid formatters
- **D5 Broker API Compliance**: lot-size math, G1/G2 guards, translate_qty, F&O qty conventions (lots vs contracts), GTT leg quantities
- **D6 Documentation Alignment**: DESIGN_GUIDE.md claims vs actual code behavior — outdated formulas, missing new routes, wrong file paths

Output a severity-ranked punch list (P1/P2/P3) with exact file:line citations. Be terse — one line per finding.
