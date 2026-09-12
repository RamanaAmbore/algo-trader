import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'https://dev.ramboq.com',
        changeOrigin: true,
        secure: true,
      },
      '/ws': {
        target: 'wss://dev.ramboq.com',
        ws: true,
        changeOrigin: true,
        secure: true,
      },
    },
  },
});
