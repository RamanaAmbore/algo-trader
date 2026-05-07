import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  onwarn: (warning, handler) => {
    // suppress a11y and state_referenced_locally warnings — cosmetic, not functional
    if (warning.code.startsWith('a11y_') || warning.code === 'state_referenced_locally') return;
    handler(warning);
  },
  kit: {
    adapter: adapter({
      pages: 'build',
      assets: 'build',
      fallback: 'index.html', // SPA mode for (algo) routes; public routes are prerendered
      precompress: false,
    }),
    prerender: {
      // Warn (don't fail) when a prerendered page links to a dynamic
      // (algo) route — those are SPA-only and served via index.html.
      handleHttpError: 'warn',
      // Discover all public routes automatically.
      entries: ['*'],
    },
    alias: {
      $lib: './src/lib',
    },
  },
};

export default config;
