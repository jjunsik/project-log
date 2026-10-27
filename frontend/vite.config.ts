import { defineConfig } from 'vitest/config';

export default defineConfig({
  build: {outDir: process.env.PROJECT_LOG_FRONTEND_DIST ?? 'dist'},
  server: { host: '127.0.0.1', proxy: process.env.PROJECT_LOG_UI_PREVIEW==='1' ? undefined : { '/api': 'http://127.0.0.1:8000' } },
  test: { include: ['src/**/*.test.ts'] },
});
