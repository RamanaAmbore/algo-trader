# tools/hooks — git hook templates

## pre-commit

Auto-regenerates `DESIGN_GUIDE.pdf` whenever `docs/DESIGN_GUIDE.md` is staged for commit. The PDF is added to the same commit automatically — both files always land together.

### Install (one-time per clone)

```bash
cp tools/hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

### Prerequisites

```bash
cd tools/pdf-gen
npm install
npx playwright install chromium
```

### Behaviour

- **DESIGN_GUIDE.md not staged** → hook exits immediately, zero cost.
- **DESIGN_GUIDE.md staged** → regenerates PDF, stages it, continues commit.
- **Generator fails** → commit is aborted so a stale PDF never lands with a fresh .md.
