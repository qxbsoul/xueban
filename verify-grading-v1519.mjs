import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.19-grading');
const token = process.env.QA_TOKEN || '';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const errors = [];
const checks = {};
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
try {
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await context.addInitScript(v => { window.localStorage.setItem('sf_xueban_session', v); }, token);
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  const openList = async () => {
    await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
    await page.locator('.teacher-app').waitFor({ timeout: 30000 });
    await page.locator('.nav-item').filter({ hasText: '判题中心' }).click();
    await page.locator('.batch-row').first().waitFor({ timeout: 30000 });
    await sleep(600);
  };
  const openBatch = async (name) => {
    await page.locator('.batch-row').filter({ hasText: name }).first().locator('button').filter({ hasText: '进入任务' }).click();
    await page.locator('.batch-progress-card').waitFor({ timeout: 30000 });
    await sleep(800);
  };
  await openList();
  checks.listRows = await page.locator('.batch-row').count();
  await page.screenshot({ path: path.join(outDir, '00_grading_list.png') });

  await openBatch('UI生命周期验收');
  checks.preprocess = (await page.locator('.preprocess-stats').innerText()).replace(/\s+/g, ' ');
  await page.locator('.batch-actions button').filter({ hasText: '开始判题' }).click();
  const progress = [];
  let paused = false;
  for (let i = 0; i < 120; i += 1) {
    await sleep(500);
    const top = (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' ');
    const metrics = (await page.locator('.batch-metrics').first().innerText()).replace(/\s+/g, ' ');
    const label = `${top} | ${metrics}`;
    if (!progress.includes(label)) progress.push(label);
    if (!paused && metrics.includes('成功 1')) {
      const btn = page.locator('.batch-actions button').filter({ hasText: '暂停判题' });
      if (await btn.count()) { await btn.click(); paused = true; }
    }
    if (paused && top.includes('已暂停')) break;
    if (top.includes('已完成') || top.includes('任务失败')) break;
  }
  checks.progressStates = progress.slice(0, 8);
  checks.afterPause = (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' ');
  checks.pauseButtons = await page.locator('.batch-actions button').allInnerTexts();
  await page.screenshot({ path: path.join(outDir, '01_paused_job.png') });

  await openList();
  await openBatch('UI生命周期验收');
  checks.afterReload = (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '02_paused_after_reload.png') });

  await page.locator('.batch-actions button').filter({ hasText: '继续判题' }).click();
  const resumed = [];
  for (let i = 0; i < 150; i += 1) {
    await sleep(600);
    const top = (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' ');
    const metrics = (await page.locator('.batch-metrics').first().innerText()).replace(/\s+/g, ' ');
    const label = `${top} | ${metrics}`;
    if (!resumed.includes(label)) resumed.push(label);
    if (top.includes('已完成') || top.includes('任务失败')) break;
  }
  checks.resumeStates = resumed.slice(0, 8);
  checks.finalStatus = (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' ');
  checks.finalStats = (await page.locator('.batch-metrics').first().innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '03_completed_after_resume.png') });

  await openList();
  await openBatch('测验节点');
  checks.bigJobStatus = (await page.locator('.batch-progress-top').innerText()).replace(/\s+/g, ' ');
  checks.bigJobStats = (await page.locator('.batch-metrics').first().innerText()).replace(/\s+/g, ' ');
  checks.bigButtons = await page.locator('.batch-actions button').allInnerTexts();
  await page.screenshot({ path: path.join(outDir, '04_completed_with_failures.png') });
  checks.overflow = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  checks.errors = errors;
  await fs.writeFile(path.join(outDir, 'result.json'), JSON.stringify(checks, null, 2), 'utf8');
  console.log(JSON.stringify(checks, null, 2));
} finally {
  await browser.close();
}
