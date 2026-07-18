import { defineConfig } from 'vitest/config';
import path from 'path';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/lib/__tests__/**/*.test.js'],
    setupFiles: ['./vitest.setup.js'],
  },
  resolve: {
    alias: {
      $lib: path.resolve('./src/lib'),
    },
  },
});
