import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  base: './',
  plugins: [svelte(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: process.env.WERFT_API_TARGET || 'http://127.0.0.1:8420',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
  // Vitest runs the transform pipeline in SSR mode by default, which would
  // otherwise make vite-plugin-svelte compile components server-side (no
  // `mount`). Forcing the `browser` resolve condition under test keeps
  // component tests exercising the real client output.
  resolve: process.env.VITEST ? { conditions: ['browser'] } : undefined,
  test: {
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
});
