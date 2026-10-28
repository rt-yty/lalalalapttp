import {defineConfig} from '@playwright/test';
// Independent server and output prevent separate audits from clearing each other's traces.
export default defineConfig({
 testDir:'./e2e',fullyParallel:true,workers:3,
 reporter:[['list'],['html',{outputFolder:'artifacts/comprehensive-audit/report',open:'never'}],['json',{outputFile:'artifacts/comprehensive-audit/results.json'}]],
 use:{baseURL:'http://127.0.0.1:5188',viewport:{width:1440,height:1000},trace:'retain-on-failure'},
 webServer:{command:'npm run dev -- --port 5188',url:'http://127.0.0.1:5188',reuseExistingServer:false},
 outputDir:'artifacts/comprehensive-audit/test-results',
});
