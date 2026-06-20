# tools/pdf-gen — markdown → PDF

Self-contained generator that turns any repo `.md` file into a print-quality PDF with mermaid diagrams rendered to vector SVG.

## One-time setup

```bash
cd tools/pdf-gen
npm install
npx playwright install chromium
```

`npm install` pulls `marked` + `playwright`; `npx playwright install chromium` downloads the browser binary that Playwright drives to print the PDF.

## Generate the design guide PDF

```bash
cd tools/pdf-gen
npm run build:design
```

Writes `DESIGN_GUIDE.pdf` to the repo root.

## Generate any markdown

```bash
cd tools/pdf-gen
node build.mjs <input.md> <output.pdf>
```

## What it does

1. Reads the markdown file.
2. Parses with `marked` — passes through GFM tables, code blocks, lists.
3. Wraps the HTML in a print-grade A4 stylesheet (cream + slate + amber accent — same family as the algo theme but tuned for paper).
4. Loads `mermaid@10` from a CDN inside the headless Chromium so every ` ```mermaid ` block gets rendered to inline SVG before snapshotting.
5. Headless Chromium prints to A4 with header/footer page numbers.

## Why this stack

- **Marked** — smallest viable markdown parser; no dependencies, GFM out of the box.
- **Playwright** — already in the frontend `node_modules` for e2e tests. The browser binary is reusable; we don't add a fresh stack.
- **Mermaid via CDN** — single `<script src>` in the rendered HTML. Diagrams come through as SVG, scale cleanly on any zoom level.

## Outputs aren't committed

`*.pdf` is excluded from git (see repo `.gitignore`). Regenerate before each share — keeps the doc + PDF in sync.
