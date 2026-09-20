import { defineConfig } from 'vitest/config';

export default defineConfig({
  server: { host: '127.0.0.1', proxy: { '/api': 'http://127.0.0.1:8000' } },
  test: { include: ['src/**/*.test.{ts,tsx}'] },
});
