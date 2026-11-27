import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.17-analytics');
const token = process.env.QA_TOKEN || '';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const errors = [];
const checks = {};
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1560, height: 1200 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, token);
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-class-card').first().waitFor({ timeout: 60000 });
  await sleep(2500);
  checks.cards = await page.locator('.ta-class-card').count();
  checks.firstCard = (await page.locator('.ta-class-card').first().innerText()).replace(/\s+/g, ' ');
  checks.forbiddenText = [];
  for (const label of ['确定性分析', '刷新分析', '重新生成语义分析', '语义分析（LLM）']) {
    if (await page.locator(`text=${label}`).count()) checks.forbiddenText.push(label);
  }
  checks.classSelects = await page.locator('.ta-scope select').count();
  checks.semantic = (await page.locator('.ta-card').filter({ hasText: '班级语义分析' }).innerText()).slice(0, 160).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '00_class_cards.png'), fullPage: false });

  await page.locator('.ta-class-card').nth(1).click();
  await sleep(2600);
  checks.secondSelected = await page.locator('.ta-class-card.on').innerText();
  checks.classStats = (await page.locator('.ta-stats').first().innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '01_class_selected.png'), fullPage: false });

  await page.locator('.ta-nav-item').filter({ hasText: '班级比较' }).click();
  await sleep(3200);
  checks.compareRows = await page.locator('.ta-compare-row').count();
  await page.screenshot({ path: path.join(outDir, '02_compare.png'), fullPage: false });

  await page.locator('.ta-nav-item').filter({ hasText: '学生画像' }).click();
  await page.locator('.ta-student-row').first().waitFor({ timeout: 30000 });
  await sleep(1200);
  checks.studentRows = await page.locator('.ta-student-row').count();
  checks.groups = await page.locator('.ta-group').count();
  checks.duplicatePrompt = await page.locator('text=请选择学生').count();
  await page.screenshot({ path: path.join(outDir, '03_student_list.png'), fullPage: false });

  await page.locator('.ta-student-row').first().click();
  await page.locator('.ta-back').waitFor({ timeout: 30000 });
  await sleep(2600);
  checks.studentDetail = (await page.locator('.ta-page-head h1').innerText()).trim();
  checks.backLabel = await page.locator('.ta-back').innerText();
  await page.screenshot({ path: path.join(outDir, '04_student_detail.png'), fullPage: false });

  await page.locator('.ta-scope select').first().selectOption('all');
  await sleep(3200);
  await page.screenshot({ path: path.join(outDir, '05_student_all.png'), fullPage: false });
  await page.locator('.ta-back').click();
  await sleep(1000);

  await page.locator('.ta-nav-item').filter({ hasText: '知识点分析' }).click();
  await sleep(2600);
  await page.screenshot({ path: path.join(outDir, '06_knowledge.png'), fullPage: false });

  await page.locator('.ta-nav-item').filter({ hasText: '错误类型分析' }).click();
  await sleep(2600);
  await page.screenshot({ path: path.join(outDir, '07_errors.png'), fullPage: false });

  await page.locator('.ta-nav-item').filter({ hasText: '学习趋势' }).click();
  await sleep(2600);
  await page.screenshot({ path: path.join(outDir, '08_trend.png'), fullPage: false });

  await page.locator('.ta-nav-item').filter({ hasText: '知识依赖' }).click();
  await sleep(2200);
  checks.dependency = (await page.locator('.ta-main').innerText()).slice(-200).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '09_dependency.png'), fullPage: false });

  checks.overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  checks.errors = errors;
  await fs.writeFile(path.join(outDir, 'result.json'), JSON.stringify(checks, null, 2), 'utf8');
  console.log(JSON.stringify(checks, null, 2));
} finally {
  await browser.close();
}
