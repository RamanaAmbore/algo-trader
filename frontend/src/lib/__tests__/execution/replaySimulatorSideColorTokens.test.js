/**
 * replaySimulatorSideColorTokens.test.js
 *
 * ReplayPanel.svelte and SimulatorPanel.svelte had four hardcoded
 * BUY/SELL hex colors instead of the shared `--c-long`/`--c-short`
 * palette tokens (app.css: BUY/long = #4ade80 green, SELL/short =
 * #f87171 red). Eight other sites in the codebase already reference
 * these tokens for the same purpose (e.g. SymbolPanel.svelte's
 * `.oes-side-buy`/`.oes-side-sell`). Source-audit guard — vitest
 * `node` environment, no component mounting required — asserts the
 * two fixed rules use the tokens and the old literal hexes are gone
 * from those specific rules.
 */

import { describe, it, expect } from 'vitest';
import REPLAY_SRC from '../../execution/ReplayPanel.svelte?raw';
import SIM_SRC from '../../execution/SimulatorPanel.svelte?raw';

describe('ReplayPanel.svelte — .sim-buy / .sim-sell use shared palette tokens', () => {
  it('.sim-buy uses var(--c-long), not a hardcoded hex', () => {
    expect(REPLAY_SRC).toMatch(/\.sim-buy\s*{\s*color:\s*var\(--c-long\);\s*}/);
    expect(REPLAY_SRC).not.toMatch(/\.sim-buy\s*{\s*color:\s*#38bdf8/);
  });

  it('.sim-sell uses var(--c-short), not a hardcoded hex', () => {
    expect(REPLAY_SRC).toMatch(/\.sim-sell\s*{\s*color:\s*var\(--c-short\);\s*}/);
    expect(REPLAY_SRC).not.toMatch(/\.sim-sell\s*{\s*color:\s*#fb923c/);
  });
});

describe('SimulatorPanel.svelte — .sim-pill-side-buy / -sell use shared palette tokens', () => {
  it('.sim-pill-side-buy uses var(--c-long-22) / var(--c-long), not hardcoded hexes', () => {
    expect(SIM_SRC).toMatch(
      /\.sim-pill-side-buy\s*{\s*background:\s*var\(--c-long-22\);\s*color:\s*var\(--c-long\);\s*}/
    );
    expect(SIM_SRC).not.toMatch(/\.sim-pill-side-buy\s*{\s*background:\s*rgba\(110,231,183/);
    expect(SIM_SRC).not.toMatch(/\.sim-pill-side-buy[^}]*#6ee7b7/);
  });

  it('.sim-pill-side-sell uses var(--c-short-22) / var(--c-short), not a hardcoded hex', () => {
    expect(SIM_SRC).toMatch(
      /\.sim-pill-side-sell\s*{\s*background:\s*var\(--c-short-22\);\s*color:\s*var\(--c-short\);\s*}/
    );
    expect(SIM_SRC).not.toMatch(/\.sim-pill-side-sell[^}]*#fda4af/);
  });
});
