---
name: DESIGN_GUIDE Sync
description: Update docs/DESIGN_GUIDE.md from commits in the last 24 hours then regenerate the PDF. Use after any architectural, behavioral, or API-surface change.
allowed-tools: Bash, Read, Edit, Write
---

## Commits since yesterday

!`cd /Users/ramanambore/projects/ramboq && git log --since="24 hours ago" --oneline --no-merges -- backend/ frontend/`

1. Read `docs/DESIGN_GUIDE.md` in full first.
2. For each commit above that touches route signatures, background task behavior, broker guards, F&O math, agent engine logic, UI rendering, sparklines, grids, or new modules — make targeted **additive** edits to `docs/DESIGN_GUIDE.md`. Never remove existing content. Never alter numbers, paths, or formulas unless the commit explicitly changed them.
3. Run `git diff docs/DESIGN_GUIDE.md | grep "^-" | grep -v "^---"` — confirm zero unexpected deletions.
4. Regenerate PDF: `cd /Users/ramanambore/projects/ramboq && python3 generate_pdf.py`
5. Stage and commit both: `git add docs/DESIGN_GUIDE.md DESIGN_GUIDE.pdf && git commit -m "docs(design): sync DESIGN_GUIDE with recent changes $(date +%Y-%m-%d)"`
