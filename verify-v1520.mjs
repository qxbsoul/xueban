import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.20');
const token = process.env.QA_TOKEN || '';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const errors = [];
const checks = {};
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, token);
  const page = await context.newPage();
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', error => errors.push(error.message));
  const gotoApp = async () => {
    await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
    await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  };
  await gotoApp();
  await page.locator('.nav-item').filter({ hasText: '判题中心' }).click();
  await page.locator('.batch-row').first().waitFor({ timeout: 30000 });
  await sleep(700);
  await page.locator('.batch-row').filter({ hasText: '测验节点' }).first().locator('button').filter({ hasText: '进入任务' }).click();
  await page.locator('.preprocess-card').waitFor({ timeout: 30000 });
  await sleep(1200);
  const preprocessText = await page.locator('.preprocess-card').innerText();
  checks.preprocessHasStateSum = preprocessText.includes('状态合计');
  checks.preprocessHasReconcile = preprocessText.includes('总数对账') || preprocessText.includes('一致');
  checks.preprocessStats = (await page.locator('.preprocess-stats').innerText()).replace(/\s+/g, ' ');
  await page.locator('.preprocess-record').first().waitFor({ timeout: 30000 });
  checks.anomalyRows = await page.locator('.preprocess-record').count();
  await page.locator('.preprocess-record-head').first().click();
  await page.locator('.preprocess-issue').first().waitFor({ timeout: 15000 });
  checks.issueDetail = (await page.locator('.preprocess-issue').first().innerText()).replace(/\s+/g, ' ');
  const more = page.locator('.preprocess-records .issue-more');
  if (await more.count()) { await more.click(); await sleep(800); }
  checks.anomalyRowsAll = await page.locator('.preprocess-record').count();
  await page.screenshot({ path: path.join(outDir, '00_preprocess_anomaly_drilldown.png'), fullPage: false });

  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-class-card').first().waitFor({ timeout: 30000 });
  await sleep(800);
  await page.locator('.ta-class-card').filter({ hasText: 'DC01' }).first().click();
  await page.locator('.ta-scope-metrics').waitFor({ timeout: 30000 });
  await sleep(700);
  checks.scopeMetrics = (await page.locator('.ta-scope-metrics').innerText()).replace(/\s+/g, ' ');
  checks.classStats = (await page.locator('.ta-stats').first().innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '01_class_scope_metrics.png'), fullPage: false });

  // 切换班级后旧详情不能残留
  await page.locator('.ta-class-card').filter({ hasText: 'DC02' }).first().click();
  await sleep(900);
  checks.classAfterSwitch = await page.locator('.ta-page-head h1').innerText();
  await page.locator('.ta-class-card').filter({ hasText: 'DC01' }).first().click();
  await sleep(500);

  await page.locator('.ta-nav-item').filter({ hasText: '学生画像' }).click();
  await page.locator('.ta-student-row').first().waitFor({ timeout: 30000 });
  await page.locator('.ta-student-row').filter({ hasText: 'EV60' }).first().click();
  await page.locator('.ta-evidence-filters').waitFor({ timeout: 30000 });
  await sleep(1200);
  checks.evidencePager = (await page.locator('.ta-pager').innerText()).replace(/\s+/g, ' ');
  checks.evidenceRows = await page.locator('.ta-evidence-filters').locator('xpath=following::table[1]//tbody/tr').count();
  checks.evidenceHasCorrect = (await page.locator('.ta-evidence-filters').locator('xpath=following::table[1]').innerText()).includes('正确');
  await page.locator('.ta-evidence-filters select').nth(3).selectOption('50');
  await sleep(900);
  checks.evidencePager50 = (await page.locator('.ta-pager').innerText()).replace(/\s+/g, ' ');
  await page.locator('.ta-pager button').filter({ hasText: '下一页' }).click();
  await sleep(700);
  checks.evidencePage2 = (await page.locator('.ta-pager').innerText()).replace(/\s+/g, ' ');
  checks.evidencePage2Rows = await page.locator('.ta-evidence-filters').locator('xpath=following::table[1]//tbody/tr').count();
  await page.screenshot({ path: path.join(outDir, '02_student_evidence_pagination.png'), fullPage: false });

  // 数据范围合并组件：全部任务隐藏具体任务选择器
  await page.locator('.ta-segmented button').filter({ hasText: '全部任务' }).click();
  await sleep(700);
  checks.scopeAllSelectCount = await page.locator('.ta-scope select').count();
  await page.locator('.ta-segmented button').filter({ hasText: '本次任务' }).click();
  await sleep(500);
  checks.scopeCurrentSelectCount = await page.locator('.ta-scope select').count();
  await page.screenshot({ path: path.join(outDir, '03_scope_switch.png'), fullPage: false });

  // 学案课程：两个独立入口 + Markdown 自动导入
  await page.locator('.nav-item').filter({ hasText: '学案课程' }).click();
  await page.locator('.teacher-upload-card').first().waitFor({ timeout: 30000 });
  checks.uploadCards = await page.locator('.teacher-upload-card').count();
  const lessonInput = page.locator('.teacher-upload-card').nth(1).locator('input[type=file]');
  await lessonInput.setInputFiles({ name: 'v1520-验收学案.md', mimeType: 'text/markdown', buffer: Buffer.from('# v1520 验收学案\n> 数学 · 七年级\n\n## 正文\n\n## 第一章 验收\n\n' + '这是 v1.5.20 自动导入验收内容，用于验证教师端学案课程入口可以读取学生端导出的 Markdown。'.repeat(6) + '\n', 'utf8') });
  await page.waitForFunction(() => {
    const nodes = Array.from(document.querySelectorAll('.teacher-upload-card:nth-child(2) .teacher-progress, .teacher-upload-card:nth-child(2) .teacher-error'));
    return nodes.some(node => (node.textContent || '').trim().length > 0);
  }, { timeout: 30000 });
  checks.lessonImport = (await page.locator('.teacher-upload-card').nth(1).innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '04_textbook_two_entries.png'), fullPage: false });
  checks.overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  checks.errors = errors;
  await fs.writeFile(path.join(outDir, 'result.json'), JSON.stringify(checks, null, 2), 'utf8');
  console.log(JSON.stringify(checks, null, 2));
} finally {
  await browser.close();
}
