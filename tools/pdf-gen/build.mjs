#!/usr/bin/env node
// Convert markdown → styled HTML → print-quality PDF.
// Mermaid blocks are rendered inside Playwright's Chromium via the
// upstream Mermaid CDN, so diagrams come through as proper vector SVG.

import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, basename } from 'node:path';
import { marked } from 'marked';
import { chromium } from 'playwright';

const [, , inPathArg, outPathArg] = process.argv;
if (!inPathArg || !outPathArg) {
    console.error('usage: node build.mjs <input.md> <output.pdf>');
    process.exit(1);
}

const inPath  = resolve(inPathArg);
const outPath = resolve(outPathArg);
const title   = basename(inPath, '.md').replace(/[_-]/g, ' ');

const md = readFileSync(inPath, 'utf8');

// Configure marked: leave ```mermaid blocks alone so the browser-side
// Mermaid library can pick them up. Other code blocks pass through with
// a class so the CSS can paint them.
const renderer = new marked.Renderer();
renderer.code = ({ text, lang }) => {
    if ((lang || '').toLowerCase() === 'mermaid') {
        return `<pre class="mermaid">${text}</pre>`;
    }
    const cls = lang ? ` class="lang-${lang}"` : '';
    return `<pre${cls}><code>${escape(text)}</code></pre>`;
};
function escape(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
marked.setOptions({ renderer, breaks: false, gfm: true });

const body = marked.parse(md);

// Print-grade CSS: A4, calm slate-on-cream palette, tight tables, amber
// accent for headings (matches the algo theme but in a print-friendly key).
const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${title}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  @page { size: A4; margin: 18mm 16mm 20mm 16mm; }
  :root {
    --fg: #1f2937;
    --muted: #4b5563;
    --accent: #b45309;
    --accent-soft: #d97706;
    --bg: #fdfcf7;
    --code-bg: #f5f1e8;
    --code-fg: #1f2937;
    --border: #d6cfbb;
  }
  * { box-sizing: border-box; }
  html, body {
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.45;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  body { margin: 0; padding: 0; }
  /* Cover */
  .cover {
    page-break-after: always;
    padding: 60mm 0 0 0;
    text-align: center;
  }
  .cover h1 {
    font-size: 32pt;
    color: var(--accent);
    border: none;
    margin: 0 0 6mm 0;
    padding: 0;
  }
  .cover .sub {
    font-size: 13pt;
    color: var(--muted);
    margin-bottom: 14mm;
  }
  .cover .meta {
    font-size: 9.5pt;
    color: var(--muted);
  }
  /* Headings */
  h1, h2, h3, h4 {
    color: var(--accent);
    margin-top: 1.2em;
    margin-bottom: 0.4em;
    page-break-after: avoid;
    break-after: avoid-page;
  }
  h1 {
    font-size: 20pt;
    border-bottom: 1.5pt solid var(--accent);
    padding-bottom: 0.2em;
    margin-top: 1em;
    page-break-before: auto;
  }
  h2 {
    font-size: 15pt;
    border-bottom: 0.5pt solid var(--border);
    padding-bottom: 0.15em;
  }
  h3 { font-size: 12.5pt; color: var(--accent-soft); }
  h4 { font-size: 11pt; color: var(--accent-soft); }
  /* Body */
  p, li { color: var(--fg); }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  strong { color: var(--accent); }
  /* Tables */
  table {
    border-collapse: collapse;
    width: 100%;
    margin: 0.8em 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  th, td {
    border: 0.5pt solid var(--border);
    padding: 4pt 6pt;
    vertical-align: top;
    text-align: left;
  }
  th {
    background: #f1ead7;
    color: var(--accent);
    font-weight: 600;
  }
  tr:nth-child(even) td { background: #faf6ec; }
  /* Code */
  pre, code {
    font-family: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
    font-size: 8.8pt;
  }
  pre {
    background: var(--code-bg);
    border: 0.5pt solid var(--border);
    border-radius: 3pt;
    padding: 8pt 10pt;
    overflow-x: auto;
    white-space: pre-wrap;
    word-wrap: break-word;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  pre code { background: transparent; padding: 0; font-size: inherit; }
  p code, li code, td code {
    background: var(--code-bg);
    border-radius: 2pt;
    padding: 0 3pt;
    font-size: 9pt;
  }
  /* Blockquotes */
  blockquote {
    margin: 0.6em 0;
    padding: 0.3em 0.9em;
    border-left: 2pt solid var(--accent-soft);
    color: var(--muted);
    background: #faf6ec;
  }
  /* Lists */
  ul, ol { margin: 0.3em 0 0.8em 0; padding-left: 1.6em; }
  li { margin: 0.15em 0; }
  /* HR */
  hr { border: none; border-top: 0.5pt solid var(--border); margin: 1.4em 0; }
  /* Mermaid */
  .mermaid {
    background: var(--bg);
    border: 0.5pt solid var(--border);
    border-radius: 3pt;
    padding: 8pt;
    margin: 0.8em 0;
    text-align: center;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  .mermaid svg { max-width: 100%; height: auto; }
  /* TOC list — tighter */
  h2 + ul li, h3 + ol li { line-height: 1.3; }
</style>
</head>
<body>
  <section class="cover">
    <h1>RamboQuant</h1>
    <div class="sub">Complete Design Guide</div>
    <div class="meta">${new Date().toISOString().slice(0, 10)}</div>
  </section>
  <article>${body}</article>
  <script>
    mermaid.initialize({
      startOnLoad: false,
      theme: 'neutral',
      themeVariables: {
        fontFamily: 'Helvetica, Arial, sans-serif',
        primaryColor: '#fdfcf7',
        primaryBorderColor: '#b45309',
        primaryTextColor: '#1f2937',
        lineColor: '#b45309',
        background: '#fdfcf7'
      },
      flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis' },
      sequence: { useMaxWidth: true, mirrorActors: false, showSequenceNumbers: false },
      gantt: { useMaxWidth: true }
    });
    window.__mermaidDone = (async () => {
      try { await mermaid.run({ querySelector: '.mermaid' }); } catch (e) { console.error('mermaid', e); }
    })();
  </script>
</body>
</html>`;

(async () => {
    const browser = await chromium.launch();
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: 'networkidle' });
    // Wait for Mermaid's IIFE promise to settle so every diagram has been
    // converted to inline SVG before we snapshot the PDF.
    await page.evaluate(() => window.__mermaidDone);
    await page.pdf({
        path: outPath,
        format: 'A4',
        printBackground: true,
        margin: { top: '18mm', right: '16mm', bottom: '20mm', left: '16mm' },
        displayHeaderFooter: true,
        headerTemplate: '<div></div>',
        footerTemplate: `
            <div style="font-size:8pt;color:#6b7280;width:100%;text-align:center;padding:0 16mm;">
                <span>RamboQuant — Design Guide</span>
                &nbsp;·&nbsp;
                <span class="pageNumber"></span> / <span class="totalPages"></span>
            </div>`
    });
    await browser.close();
    console.log(`Wrote ${outPath}`);
})();
