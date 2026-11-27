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
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1560, height: 1040 } });
  await context.addInitScript(value => { window.localStorage.setItem('sf_xueban_session', value); }, token);
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-shell').waitFor({ timeout: 30000 });
  await page.locator('.ta-task-bar').waitFor({ timeout: 30000 });
  // 等待确定性分析完成（首次打开会自动生成）
  for (let i = 0; i < 40; i += 1) {
    const text = await page.locator('.ta-task-bar').innerText();
    if (text.includes('已完成')) break;
    await sleep(1500);
  }
  await sleep(1200);
  checks.taskBar = (await page.locator('.ta-task-bar').innerText()).replace(/\s+/g, ' ');
  checks.overviewStats = await page.locator('.ta-stats').first().innerText();
  await page.screenshot({ path: path.join(outDir, '00_overview.png'), fullPage: true });

  await page.setViewportSize({ width: 1560, height: 1500 });
  await page.locator('.ta-nav-item').filter({ hasText: '班级学情' }).click();
  await page.locator('.ta-dist-row').first().waitFor({ timeout: 30000 });
  await sleep(2500);
  checks.distRows = await page.locator('.ta-dist-row').count();
  checks.distFirst = (await page.locator('.ta-dist-row').first().innerText()).replace(/\s+/g, ' ');
  checks.stackSegs = await page.locator('.ta-dist-row').first().locator('.ta-stack-seg').count();
  checks.questionHeaders = await page.locator('.ta-card').filter({ hasText: '题目表现' }).locator('thead').innerText();
  checks.compareRows = await page.locator('.ta-compare-row').count();
  checks.compareBadges = await page.locator('.ta-badges').innerText();
  await page.screenshot({ path: path.join(outDir, '01_class.png'), fullPage: false });
  await page.locator('.ta-card').filter({ hasText: '班级比较' }).scrollIntoViewIfNeeded();
  await sleep(600);
  await page.screenshot({ path: path.join(outDir, '01b_compare.png'), fullPage: false });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator('.ta-nav').scrollIntoViewIfNeeded();
  await sleep(400);

  // 点击分布区段 → 学生名单
  await page.locator('.ta-dist-row').first().locator('.ta-stack-seg').nth(1).click();
  await page.locator('.ta-drawer').waitFor({ timeout: 10000 });
  await sleep(500);
  checks.ladder = (await page.locator('.ta-drawer-head').innerText()).replace(/\s+/g, ' ');
  checks.ladderRows = await page.locator('.ta-drawer tbody tr').count();
  await page.screenshot({ path: path.join(outDir, '02_status_students.png'), fullPage: false });
  await page.locator('.ta-drawer-head button').click();
  await sleep(300);

  // 点击知识点名称 → 知识点详情
  await page.locator('.ta-dist-name').first().click();
  await page.locator('.ta-drawer').waitFor({ timeout: 20000 });
  await page.locator('.ta-drawer h3').first().waitFor({ timeout: 30000 });
  await sleep(600);
  checks.kpDrawer = (await page.locator('.ta-drawer-head').innerText()).replace(/\s+/g, ' ');
  checks.kpDrawerRows = await page.locator('.ta-drawer tbody tr').count();
  checks.kpDrawerBox = await page.locator('.ta-drawer').boundingBox();
  checks.backdrop = await page.locator('.ta-backdrop').count();
  await page.screenshot({ path: path.join(outDir, '03_kp_detail.png'), fullPage: false });
  await page.locator('.ta-drawer-head button').click();
  await sleep(300);

  // 点击题目行 → 单题详情
  await page.locator('tr.ta-clickable').first().click();
  await page.locator('.ta-drawer').waitFor({ timeout: 20000 });
  await sleep(1200);
  checks.questionDrawer = (await page.locator('.ta-drawer-head').innerText()).replace(/\s+/g, ' ');
  checks.questionPartition = await page.locator('.ta-drawer .ta-stats').first().innerText();
  await page.screenshot({ path: path.join(outDir, '04_question_detail.png'), fullPage: false });
  await page.locator('.ta-drawer-head button').click();
  await sleep(300);

  // 学生画像：跟随全局数据范围
  await page.locator('.ta-nav-item').filter({ hasText: '学生画像' }).click();
  const studentSelect = page.locator('.ta-scope select').last();
  const options = await studentSelect.locator('option').count();
  if (options > 1) await studentSelect.selectOption({ index: 1 });
  await sleep(2500);
  checks.studentSingle = (await page.locator('.ta-main').innerText()).slice(0, 120).replace(/\s+/g, ' ');
  await page.evaluate(() => window.scrollTo(0, 0));
  await sleep(300);
  await page.screenshot({ path: path.join(outDir, '05_student_single.png'), fullPage: false });

  await page.locator('.ta-scope select').first().selectOption('all');
  await sleep(4000);
  checks.studentAll = (await page.locator('.ta-main').innerText()).slice(0, 200).replace(/\s+/g, ' ');
  await page.evaluate(() => window.scrollTo(0, 0));
  await sleep(300);
  await page.screenshot({ path: path.join(outDir, '06_student_all.png'), fullPage: false });

  await page.locator('.ta-nav-item').filter({ hasText: '学习趋势' }).click();
  await sleep(3000);
  await page.screenshot({ path: path.join(outDir, '07_trend.png'), fullPage: true });
  checks.trend = (await page.locator('.ta-main').innerText()).slice(0, 200).replace(/\s+/g, ' ');

  await page.locator('.ta-nav-item').filter({ hasText: '错误分析' }).click();
  await sleep(2500);
  await page.screenshot({ path: path.join(outDir, '08_errors.png'), fullPage: true });

  await page.locator('.ta-nav-item').filter({ hasText: '知识点分析' }).click();
  await sleep(2500);
  await page.screenshot({ path: path.join(outDir, '09_matrix.png'), fullPage: true });

  // 触发 LLM 语义分析（真实调用），等待完成
  await page.locator('.ta-nav-item').filter({ hasText: '总体概览' }).click();
  await sleep(1500);
  const llmItem = page.locator('.ta-task-item').nth(1);
  const llmBefore = await llmItem.innerText();
  if (!llmBefore.includes('已完成')) {
    const llmButton = page.locator('.ta-task-bar button').filter({ hasText: /语义分析|生成语义/ });
    if (await llmButton.count()) {
      await llmButton.first().click();
      for (let i = 0; i < 60; i += 1) {
        await sleep(2000);
        const text = await llmItem.innerText();
        if (text.includes('已完成') || text.includes('部分完成') || text.includes('失败')) break;
      }
    }
  }
  checks.llmBar = (await llmItem.innerText()).replace(/\s+/g, ' ');
  checks.llmDetail = (await page.locator('.ta-task-bar').innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '10b_llm_status.png'), fullPage: false });

  await page.locator('.ta-nav-item').filter({ hasText: '教学建议' }).click();
  await sleep(3000);
  checks.suggest = (await page.locator('.ta-main').innerText()).slice(0, 200).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '10_suggest.png'), fullPage: true });

  checks.overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  checks.sessionReload = null;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-task-bar').waitFor({ timeout: 30000 });
  await sleep(2500);
  checks.sessionReload = (await page.locator('.ta-task-bar').innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '11_after_reload.png'), fullPage: true });
  await page.setViewportSize({ width: 1560, height: 1040 });
  checks.errors = errors;
  await fs.writeFile(path.join(outDir, 'result.json'), JSON.stringify(checks, null, 2), 'utf8');
  console.log(JSON.stringify(checks, null, 2));
} finally {
  await browser.close();
}
