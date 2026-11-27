import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.20');
const token = process.env.QA_TOKEN || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, token);
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '学案课程' }).click();
  await page.locator('.teacher-upload-card').first().waitFor({ timeout: 30000 });
  await page.locator('.teacher-field-grid input').nth(0).fill('v1520 教材上传验收');
  const inputs = page.locator('.teacher-upload-card').first().locator('input[type=file]');
  await inputs.nth(0).setInputFiles({ name: 'textbook.txt', mimeType: 'text/plain', buffer: Buffer.from('第一章 有理数\n正数和负数。数轴、相反数、绝对值。\n第二章 整式\n单项式与多项式。\n', 'utf8') });
  await inputs.nth(1).setInputFiles({ name: 'plan.txt', mimeType: 'text/plain', buffer: Buffer.from('课程标准：理解有理数和整式的基本概念，能进行简单运算。\n', 'utf8') });
  await sleep(500);
  const fileRows = await page.locator('.teacher-upload-card').first().locator('.teacher-file-list li').count();
  await page.locator('.teacher-primary').click();
  await page.waitForFunction(() => {
    const card = document.querySelector('.teacher-upload-card');
    const nodes = Array.from(card?.querySelectorAll('.teacher-progress, .teacher-error') || []);
    return nodes.some(node => (node.textContent || '').trim().length > 0);
  }, { timeout: 120000 });
  const status = (await page.locator('.teacher-upload-card').first().innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '05_textbook_upload.png'), fullPage: false });
  console.log(JSON.stringify({ fileRows, status }, null, 2));
} finally {
  await browser.close();
}
