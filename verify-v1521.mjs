import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.21');
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
const errors = [];
try {
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await context.addInitScript(value => localStorage.setItem('sf_xueban_session', value), process.env.QA_TOKEN || '');
  const page = await context.newPage();
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '判题中心' }).click();
  await page.locator('.batch-row').filter({ hasText: '测验节点' }).first().locator('button').filter({ hasText: '进入任务' }).click();
  await page.locator('.batch-progress-card').waitFor({ timeout: 30000 });
  await page.waitForTimeout(2000);
  const result = {
    status: (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' '),
    metrics: (await page.locator('.batch-metrics').first().innerText()).replace(/\s+/g, ' '),
    buttons: await page.locator('.batch-actions button').allInnerTexts(),
    failureTitle: await page.locator('.job-failures strong').first().innerText().catch(() => ''),
    failureSummary: (await page.locator('.job-failures p').first().innerText().catch(() => '')).replace(/\s+/g, ' '),
    overflow: await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth),
    errors,
  };
  await page.screenshot({ path: path.join(outDir, '00_job_reliability.png'), fullPage: false });
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
