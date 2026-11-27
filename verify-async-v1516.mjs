import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.16-analytics2');
const token = process.env.QA_TOKEN || '';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const errors = [];
const checks = {};
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1560, height: 1040 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, token);
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-task-bar').waitFor({ timeout: 30000 });
  for (let i = 0; i < 40; i += 1) {
    const text = await page.locator('.ta-task-bar').innerText();
    if (text.includes('确定性分析') && text.includes('已完成')) break;
    await sleep(1000);
  }
  const detItem = page.locator('.ta-task-item').nth(0);
  const button = page.locator('.ta-task-bar button').filter({ hasText: '刷新分析' });
  await button.click();
  await sleep(500);
  checks.acceptedBar = (await detItem.innerText()).replace(/\s+/g, ' ');
  checks.acceptedButton = (await page.locator('.ta-task-bar button').first().innerText()).replace(/\s+/g, ' ');
  const start = Date.now();
  const seen = new Set();
  for (let i = 0; i < 60; i += 1) {
    await sleep(500);
    const text = (await detItem.innerText()).replace(/\s+/g, ' ');
    seen.add(text);
    if (text.includes('已完成')) break;
  }
  checks.seenStates = Array.from(seen);
  checks.completedBar = (await detItem.innerText()).replace(/\s+/g, ' ');
  checks.elapsedMs = Date.now() - start;
  await page.screenshot({ path: path.join(outDir, '12_async_accepted.png'), fullPage: false });
  checks.errors = errors;
  await fs.writeFile(path.join(outDir, 'async-result.json'), JSON.stringify(checks, null, 2), 'utf8');
  console.log(JSON.stringify(checks, null, 2));
} finally {
  await browser.close();
}
