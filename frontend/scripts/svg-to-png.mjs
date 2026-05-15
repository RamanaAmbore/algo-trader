#!/usr/bin/env node
/**
 * Rasterize the brand SVGs to PNGs using Playwright's bundled Chromium.
 * One-shot tool — produces:
 *   - og-image.png    (1200 × 630)   social-share preview
 *   - app-icon-192.png (192 × 192)   PWA / Android home-screen
 *   - app-icon-512.png (512 × 512)   PWA install splash
 *
 * Run from the frontend/ directory:
 *   node scripts/svg-to-png.mjs
 *
 * Used when the SVG palette changes and the rasterized assets need to
 * be regenerated. Commit the resulting PNGs.
 */
import { chromium } from 'playwright';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const STATIC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'static');

const TARGETS = [
  { svg: 'og-image.svg',  png: 'og-image.png',     width: 1200, height: 630 },
  { svg: 'app-icon.svg',  png: 'app-icon-192.png', width: 192,  height: 192 },
  { svg: 'app-icon.svg',  png: 'app-icon-512.png', width: 512,  height: 512 },
];

const browser = await chromium.launch();

for (const t of TARGETS) {
  const svgPath = path.join(STATIC_DIR, t.svg);
  const pngPath = path.join(STATIC_DIR, t.png);
  const svgRaw = await readFile(svgPath, 'utf8');

  // Wrap the SVG in a bare HTML doc so the browser renders it at the
  // requested viewport size. transparent: false because the SVG itself
  // paints its own background.
  const html = `<!doctype html><html><head><style>
    html,body{margin:0;padding:0;background:transparent;}
    svg{display:block;width:${t.width}px;height:${t.height}px;}
  </style></head><body>${svgRaw}</body></html>`;

  const page = await browser.newPage({ viewport: { width: t.width, height: t.height } });
  await page.setContent(html, { waitUntil: 'load' });
  // Filter effects may need a tick to finish painting.
  await page.waitForTimeout(50);
  await page.screenshot({
    path: pngPath,
    clip: { x: 0, y: 0, width: t.width, height: t.height },
    omitBackground: false,
    type: 'png',
  });
  await page.close();
  console.log(`✓ ${t.svg} → ${t.png} (${t.width}×${t.height})`);
}

await browser.close();
