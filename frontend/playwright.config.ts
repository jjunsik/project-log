import {defineConfig} from '@playwright/test';
import {mkdirSync, mkdtempSync} from 'node:fs';
import {join} from 'node:path';

const root = join(import.meta.dirname, 'test-results');
mkdirSync(root, {recursive: true});
const results = mkdtempSync(join(root, 'run-'));
export default defineConfig({
  testDir: './e2e', workers: 1, retries: 0, timeout: 45_000,
  outputDir: results,
  reporter: [['list'], ['json', {outputFile: join(results, 'results.json')}]],
  use: {baseURL: `http://127.0.0.1:${process.env.PROJECT_LOG_E2E_PORT ?? '8000'}`, headless: true,
    viewport: {width: 1360, height: 1080}, screenshot: 'only-on-failure', trace: 'retain-on-failure'},
});
