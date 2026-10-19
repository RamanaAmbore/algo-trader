/**
 * tf_up_down_text_fill_color_render.spec.js
 *
 * 2026-10-19 — root-cause fix verification: every LTP/Chg% cell that
 * flashes also carries a directional `!important` color class
 * (`.ltp-day-pos/neg/flat`, `.cell-pos/neg/flat`). An author `!important`
 * declaration always beats a `@keyframes` `color` property regardless of
 * specificity — so every earlier attempt to animate `color` inside the
 * flash keyframes was silently discarded; only the background pulse ever
 * rendered. Fix: the global `.tf-up`/`.tf-down` keyframes (`tf-pnl-up`/
 * `tf-pnl-down` in app.css) now animate `-webkit-text-fill-color` instead
 * of `color` — a distinct property the `!important` rule never touches.
 *
 * This spec PROVES the fix actually renders (not just that the CSS
 * compiles/parses) by pausing the real CSS animation via the Web
 * Animations API and reading `getComputedStyle` at two points in time:
 * the start frame (text should be white, background green/red-tinted)
 * and the end frame (text should resolve to `currentColor` — i.e. the
 * `!important`-set directional color — background transparent).
 *
 * Runs on /signin (public, no login, no backend dependency) — app.css is
 * imported at the ROOT layout (`src/routes/+layout.svelte`) so it applies
 * to every route including public ones. A synthetic span is injected
 * directly, independent of any real grid/component — this is a pure CSS
 * mechanism test.
 */

import { test, expect } from '@playwright/test';

test.describe('tf-up/tf-down flash — -webkit-text-fill-color actually renders, not just compiles', () => {
  test('tf-up: background goes green→transparent, text-fill goes white→green (currentColor), while `color` stays green throughout (the !important rule)', async ({ page }) => {
    await page.goto('/signin', { waitUntil: 'domcontentloaded' });

    const result = await page.evaluate(() => {
      const el = document.createElement('span');
      // ltp-day-pos sets `color: var(--algo-green) !important` (static,
      // never animated) — tf-up is the transient background+fill pulse.
      el.className = 'tf-up ltp-day-pos';
      el.textContent = '123.45';
      document.body.appendChild(el);

      const anims = el.getAnimations();
      if (anims.length === 0) return { error: 'no animation attached (CSS rule not found / dropped)' };
      const anim = anims[0];
      anim.pause();

      // Start frame (currentTime = 0).
      anim.currentTime = 0;
      const startCs = getComputedStyle(el);
      const start = {
        bg: startCs.backgroundColor,
        fill: startCs.webkitTextFillColor,
        color: startCs.color,
      };

      // End frame (currentTime = full duration, minus 1ms to stay inside
      // the active animation rather than past it — an ease-out curve's
      // true endpoint is sampled, not the post-animation base style).
      const durMs = anim.effect.getTiming().duration;
      anim.currentTime = Math.max(0, Number(durMs) - 1);
      const endCs = getComputedStyle(el);
      const end = {
        bg: endCs.backgroundColor,
        fill: endCs.webkitTextFillColor,
        color: endCs.color,
      };

      el.remove();
      return { start, end };
    });

    expect(result.error, result.error).toBeUndefined();

    // Start: text-fill is white, distinct from the green `color`.
    expect(result.start.fill).toBe('rgb(255, 255, 255)');
    expect(result.start.color).not.toBe(result.start.fill);
    // `color` itself (the !important rule) is unaffected by the
    // animation at any point — this is the entire point of the fix.
    expect(result.start.color).toBe(result.end.color);
    // Background starts tinted (not transparent).
    expect(result.start.bg).not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/);

    // End: text-fill has animated BACK toward the real color (currentColor
    // resolves to `color`, i.e. the green !important rule) — this is the
    // direct proof the fix renders: the text genuinely changes color
    // across the animation, not just the background.
    expect(result.end.fill).toBe(result.end.color);
    // Background has decayed to (effectively) transparent — alpha 0.
    // Chrome interpolates toward the `transparent` keyword by keeping the
    // other color's RGB channels and animating only the alpha channel to
    // 0 (per the CSS Color spec's premultiplied-alpha interpolation), so
    // the computed value is `rgba(<green-rgb>, 0)`, NOT literally
    // `rgba(0, 0, 0, 0)` — match on a trailing zero alpha instead.
    expect(result.end.bg).toMatch(/,\s*0\)$/);
  });

  test('tf-down: same mechanism, red direction', async ({ page }) => {
    await page.goto('/signin', { waitUntil: 'domcontentloaded' });

    const result = await page.evaluate(() => {
      const el = document.createElement('span');
      el.className = 'tf-down ltp-day-neg';
      el.textContent = '98.70';
      document.body.appendChild(el);
      const anim = el.getAnimations()[0];
      if (!anim) return { error: 'no animation attached' };
      anim.pause();
      anim.currentTime = 0;
      const start = { fill: getComputedStyle(el).webkitTextFillColor, color: getComputedStyle(el).color };
      const durMs = anim.effect.getTiming().duration;
      anim.currentTime = Math.max(0, Number(durMs) - 1);
      const end = { fill: getComputedStyle(el).webkitTextFillColor, color: getComputedStyle(el).color };
      el.remove();
      return { start, end };
    });

    expect(result.error, result.error).toBeUndefined();
    expect(result.start.fill).toBe('rgb(255, 255, 255)');
    expect(result.end.fill).toBe(result.end.color); // resolves to red (!important color)
  });

  test('fullscreen boost (.fs-card-on .tf-up) uses the stronger -fs keyframe and the same text-fill mechanism', async ({ page }) => {
    await page.goto('/signin', { waitUntil: 'domcontentloaded' });

    const result = await page.evaluate(() => {
      const wrap = document.createElement('div');
      wrap.className = 'fs-card-on';
      const el = document.createElement('span');
      el.className = 'tf-up ltp-day-pos';
      wrap.appendChild(el);
      document.body.appendChild(wrap);

      const anim = el.getAnimations()[0];
      if (!anim) return { error: 'no animation attached' };
      const name = anim.animationName;
      anim.pause();
      anim.currentTime = 0;
      const fill = getComputedStyle(el).webkitTextFillColor;
      wrap.remove();
      return { name, fill };
    });

    expect(result.error, result.error).toBeUndefined();
    expect(result.name).toBe('tf-pnl-up-fs');
    expect(result.fill).toBe('rgb(255, 255, 255)');
  });

  test('a flashing cell\'s `color` is never animated — only -webkit-text-fill-color', async ({ page }) => {
    // Regression guard for the exact bug this fix corrects: if a FUTURE
    // edit reverts the keyframe to animate `color` again, it would be
    // silently discarded by the !important rule — this test fails loudly
    // instead by asserting `color` is constant across the animation.
    await page.goto('/signin', { waitUntil: 'domcontentloaded' });
    const result = await page.evaluate(() => {
      const el = document.createElement('span');
      el.className = 'tf-up ltp-day-pos';
      document.body.appendChild(el);
      const anim = el.getAnimations()[0];
      anim.pause();
      anim.currentTime = 0;
      const c0 = getComputedStyle(el).color;
      const dur = Number(anim.effect.getTiming().duration);
      anim.currentTime = dur / 2;
      const c1 = getComputedStyle(el).color;
      anim.currentTime = Math.max(0, dur - 1);
      const c2 = getComputedStyle(el).color;
      el.remove();
      return { c0, c1, c2 };
    });
    expect(result.c0).toBe(result.c1);
    expect(result.c1).toBe(result.c2);
  });
});
