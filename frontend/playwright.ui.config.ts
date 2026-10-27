import {defineConfig} from '@playwright/test';
import {mkdirSync,mkdtempSync} from 'node:fs';
import {join} from 'node:path';

const root=join(import.meta.dirname,'test-results');
mkdirSync(root,{recursive:true});
const output=mkdtempSync(join(root,'analysis-ui-'));
export default defineConfig({
  testDir:'./e2e',testMatch:'experience-ui.spec.ts',workers:1,retries:0,timeout:30_000,
  outputDir:output,reporter:[['list'],['json',{outputFile:join(output,'results.json')}]],
  use:{baseURL:'http://127.0.0.1:4179',headless:true,viewport:{width:1360,height:1080},screenshot:'only-on-failure'},
  webServer:{command:'npm run dev -- --port 4179 --strictPort',url:'http://127.0.0.1:4179/experience-preview.html',
    reuseExistingServer:false,env:{PROJECT_LOG_UI_PREVIEW:'1'}},
});
