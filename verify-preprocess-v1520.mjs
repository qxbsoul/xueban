import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.20');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, process.env.QA_TOKEN || '');
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '判题中心' }).click();
  await page.locator('.batch-row').filter({ hasText: '测验节点' }).first().locator('button').filter({ hasText: '进入任务' }).click();
  await page.locator('.preprocess-card').waitFor({ timeout: 30000 });
  await page.locator('.preprocess-record-head').first().waitFor({ timeout: 30000 });
  const more = page.locator('.preprocess-records .issue-more');
  if (await more.count()) { await more.click(); await sleep(700); }
  await page.locator('.preprocess-record-head').first().click();
  await page.locator('.preprocess-issue').first().waitFor({ timeout: 15000 });
  await page.evaluate(() => { const card = document.querySelector('.preprocess-card'); card?.scrollIntoView({ block: 'start' }); });
  await sleep(400);
  await page.screenshot({ path: path.join(outDir, '00_preprocess_anomaly_drilldown.png'), fullPage: false });
  console.log(JSON.stringify({ rows: await page.locator('.preprocess-record').count(), overflow: await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth) }));
} finally {
  await browser.close();
}
