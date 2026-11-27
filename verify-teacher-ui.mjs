import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.16-analytics');
const token = process.env.QA_TOKEN || '';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const errors = [];
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, token);
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await sleep(2500);
  await page.screenshot({ path: path.join(outDir, '00_teacher_workbench.png'), fullPage: false });
  await page.locator('.nav-item').filter({ hasText: '判题中心' }).click();
  await sleep(5000);
  await page.screenshot({ path: path.join(outDir, '12_grading_center.png'), fullPage: false });
  const list = page.locator('.batch-list, .batch-section');
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement;
    return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth };
  });
  await page.locator('.nav-item').filter({ hasText: '数据管理' }).click();
  await sleep(2500);
  await page.screenshot({ path: path.join(outDir, '13_data_manager.png'), fullPage: false });
  console.log(JSON.stringify({ errors, overflow, hasList: await list.count() }, null, 2));
} finally {
  await browser.close();
}
