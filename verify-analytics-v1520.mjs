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
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-class-card').first().waitFor({ timeout: 30000 });
  await page.locator('.ta-class-card').filter({ hasText: 'DC01' }).first().click();
  await page.locator('.ta-scope-metrics').waitFor({ timeout: 30000 });
  await sleep(800);
  await page.screenshot({ path: path.join(outDir, '01_class_scope_metrics.png'), fullPage: false });
  await page.locator('.ta-segmented button').filter({ hasText: '全部任务' }).click();
  await sleep(700);
  await page.screenshot({ path: path.join(outDir, '03_scope_switch.png'), fullPage: false });
  console.log(JSON.stringify({ cards: await page.locator('.ta-class-card').count(), scopeSelect: await page.locator('.ta-scope select').count() }));
} finally {
  await browser.close();
}
