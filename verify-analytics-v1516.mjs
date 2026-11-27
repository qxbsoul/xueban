import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.16-analytics');
const token = process.env.QA_TOKEN || '';
const results = [];
const errors = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function shot(page, name) {
  await page.screenshot({ path: path.join(outDir, `${name}.png`), fullPage: true });
  results.push(name);
}

async function openSection(page, label, name, waitFor) {
  await page.locator('.ta-nav-item').filter({ hasText: label }).click();
  if (waitFor) {
    await page.locator(waitFor).first().waitFor({ timeout: 25000 }).catch(() => {});
  }
  await sleep(6000);
  await shot(page, name);
}

const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, token);
  const page = await context.newPage();
  page.on('console', message => { if (message.type() === 'error') errors.push('console: ' + message.text()); });
  page.on('pageerror', error => errors.push('page: ' + error.message));

  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await sleep(2500);
  await shot(page, '00_teacher_workbench');

  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-nav').waitFor({ timeout: 20000 });
  await sleep(9000);
  await shot(page, '01_overview');

  await openSection(page, '班级学情', '02_class', '.ta-table');
  const firstRow = page.locator('.ta-table tbody tr').first();
  if (await firstRow.count()) {
    await firstRow.click();
    await sleep(7000);
    await shot(page, '03_question_detail');
    const close = page.getByRole('button', { name: '关闭' }).first();
    if (await close.count()) { await close.click(); await sleep(800); }
  }
  const compare = page.getByRole('button', { name: '对比所选班级' });
  if (await compare.count()) {
    const boxes = page.locator('.ta-picker input[type=checkbox]');
    const count = await boxes.count();
    if (count >= 2) {
      await boxes.nth(0).check();
      await boxes.nth(1).check();
      await compare.click();
      await sleep(6000);
      await shot(page, '04_class_compare');
    }
  }

  await openSection(page, '学生画像', '05_student', '.ta-stats');
  const studentSelect = page.locator('.ta-scope select').last();
  if (await studentSelect.count()) {
    const options = await studentSelect.locator('option').all();
    if (options.length > 1) {
      await studentSelect.selectOption({ index: 1 });
      await sleep(7000);
      await shot(page, '06_student_profile_single');
      const allTab = page.getByRole('button', { name: '全部任务' });
      if (await allTab.count()) {
        await allTab.click();
        await sleep(7000);
        await shot(page, '07_student_profile_all');
      }
    }
  }

  await openSection(page, '知识点分析', '08_knowledge_matrix', '.ta-matrix');
  await openSection(page, '错误分析', '09_errors', '.ta-table');
  await openSection(page, '学习趋势', '10_trend', '.ta-table');
  await openSection(page, '教学建议', '11_suggestions', '.ta-suggest, .ta-empty');

  for (const label of ['总体概览', '班级学情', '知识点分析', '错误分析', '总体概览']) {
    await page.locator('.ta-nav-item').filter({ hasText: label }).click();
    await sleep(120);
  }
  await sleep(3000);
  const abortNotice = await page.locator('.ta-error').count();

  await fs.writeFile(path.join(outDir, 'result.json'), JSON.stringify({ results, errors, abortNotice }, null, 2), 'utf8');
  console.log(JSON.stringify({ shots: results.length, errors, abortNotice }, null, 2));
} finally {
  await browser.close();
}
