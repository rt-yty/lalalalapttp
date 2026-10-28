import {defineConfig} from '@playwright/test';

// This config owns a disposable backend. It never reuses the app on port 8780.
export default defineConfig({
 testDir:'./e2e-real',fullyParallel:false,workers:1,timeout:60000,
 reporter:[['list'],['html',{open:'never',outputFolder:'playwright-report-real'}]],
 use:{baseURL:'http://127.0.0.1:8792',viewport:{width:1440,height:1000},trace:'retain-on-failure'},
 webServer:{command:'../.venv/bin/python e2e-real/server.py',url:'http://127.0.0.1:8792/api/health',reuseExistingServer:false,timeout:30000},
 outputDir:'test-results-real',
});
