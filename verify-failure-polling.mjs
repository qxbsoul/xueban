import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
let failures = 0;
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addInitScript(value => localStorage.setItem('sf_xueban_session', value), process.env.QA_TOKEN || '');
  const page = await context.newPage();
  page.on('request', request => { if (request.url().includes('/failures')) failures += 1; });
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '判题中心' }).click();
  await page.locator('.batch-row').filter({ hasText: '测验节点' }).first().locator('button').filter({ hasText: '进入任务' }).click();
  await page.locator('.job-failures').waitFor({ timeout: 30000 });
  await page.waitForTimeout(7000);
  console.log(JSON.stringify({ failureRequests: failures, status: (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' ') }));
} finally {
  await browser.close();
}
