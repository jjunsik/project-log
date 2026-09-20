import { defineConfig } from '@playwright/test';
import { mkdirSync, mkdtempSync } from 'node:fs';
import { join } from 'node:path';

const resultsRoot = join(import.meta.dirname, 'test-results');
mkdirSync(resultsRoot, {recursive: true});
const results = mkdtempSync(join(resultsRoot, 'run-'));
export default defineConfig({
  testDir: './e2e', workers: 1, retries: 0,
  outputDir: results,
  reporter: [['list'], ['json', {outputFile: join(results, 'results.json')}]],
  use: {baseURL: 'http://127.0.0.1:8000', headless: true, viewport: {width: 1360, height: 1000},
    screenshot: 'only-on-failure', trace: 'retain-on-failure'},
});
