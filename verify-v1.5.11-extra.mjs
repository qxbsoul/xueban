import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.11');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const results = [];
const errors = [];

const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  await fs.mkdir(outDir, { recursive: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  page.on('pageerror', error => errors.push('page: ' + error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push('console: ' + message.text()); });
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'networkidle' });
  await page.fill('#auth-email', 'demo-teacher@test.local');
  await page.fill('#auth-password', 'DemoPassw0rd!');
  await page.getByRole('button', { name: '教师端', exact: true }).click();
  await page.getByRole('button', { name: /^登录/ }).first().click();
  await page.getByText('教师工作台').first().waitFor({ timeout: 30000 });
  await sleep(1000);
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await sleep(1200);
  const classButton = page.locator('.analytics-class').first();
  if (await classButton.count()) {
    await classButton.click();
    await sleep(2400);
    const enroll = page.getByText('任务内学生身份').first();
    if (await enroll.count()) {
      await enroll.scrollIntoViewIfNeeded();
      await sleep(400);
      await page.screenshot({ path: path.join(outDir, '14_class_enrollments.png'), fullPage: false });
      results.push('14_class_enrollments');
    }
    const trend = page.getByText('测验趋势').first();
    if (await trend.count()) {
      await trend.scrollIntoViewIfNeeded();
      await sleep(500);
      await page.screenshot({ path: path.join(outDir, '15_trend_chart.png'), fullPage: false });
      results.push('15_trend_chart');
    }
  }
  await fs.writeFile(path.join(outDir, 'extra_result.json'), JSON.stringify({ results, errors }, null, 2), 'utf8');
  console.log(JSON.stringify({ results, errors }, null, 2));
} finally {
  await browser.close();
}
