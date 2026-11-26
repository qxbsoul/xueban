import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, '..', 'verify-artifacts', 'v1.5.13');
const results = [];
const errors = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function shot(page, name) {
  await page.screenshot({ path: path.join(outDir, `${name}.png`), fullPage: false });
  results.push(name);
}

const browser = await chromium.launch({
  executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
});
try {
  await fs.mkdir(outDir, { recursive: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  page.on('console', message => { if (message.type() === 'error') errors.push('console: ' + message.text()); });
  page.on('pageerror', error => errors.push('page: ' + error.message));
  page.on('requestfailed', request => { if (errors.length < 40) errors.push(`requestfailed: ${request.url()} ${request.failure()?.errorText || ''}`); });

  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'networkidle' });
  await shot(page, '01_login');

  await page.fill('#auth-email', 'demo-teacher@test.local');
  await page.fill('#auth-password', 'DemoPassw0rd!');
  await page.getByRole('button', { name: '教师端', exact: true }).click();
  await page.getByRole('button', { name: /^登录/ }).first().click();
  await page.getByText('教师工作台').first().waitFor({ timeout: 30000 });
  await sleep(1200);
  await shot(page, '02_teacher_workbench');

  await page.locator('.nav-item').filter({ hasText: '数据管理' }).click();
  await sleep(1200);
  await shot(page, '03_data_manager');

  await page.locator('.nav-item').filter({ hasText: '判题中心' }).click();
  await sleep(1200);
  await shot(page, '04_task_list');
  const enter = page.getByRole('button', { name: '进入任务' }).first();
  if (await enter.count()) {
    await enter.click();
    await sleep(1800);
    await shot(page, '05_task_detail_and_gate');
  }

  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await sleep(1500);
  await shot(page, '06_analytics_classes');
  const classButton = page.locator('.analytics-class').first();
  if (await classButton.count()) {
    await classButton.click();
    await sleep(2500);
    await shot(page, '07_class_overview');
    const taskRow = page.locator('.analytics-compare table tbody tr').first();
    if (await taskRow.count()) {
      await taskRow.click();
      await sleep(1800);
      await shot(page, '08_question_detail');
    }
  }

  // 回到班级列表，进入评分点映射 / 知识依赖 / 错误类型
  await page.getByRole('button', { name: '班级学情' }).first().click().catch(() => null);
  await sleep(800);
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await sleep(800);
  const mappingTab = page.getByRole('button', { name: '评分点映射' });
  if (await mappingTab.count()) {
    await mappingTab.click();
    await sleep(1600);
    await shot(page, '09_mapping_management');
    await page.getByRole('button', { name: '知识依赖' }).click();
    await sleep(1200);
    await shot(page, '10_knowledge_graph');
    await page.getByRole('button', { name: '错误类型' }).click();
    await sleep(1000);
    const classify = page.getByRole('button', { name: '为已判题作答生成错误标签' });
    if (await classify.count()) { await classify.click(); await sleep(2500); }
    await shot(page, '11_error_labels');
  }

  // 学生端
  await page.getByRole('button', { name: '退出当前账号' }).click();
  await sleep(1200);
  await page.fill('#auth-email', 'd01@xueban.local');
  await page.fill('#auth-password', 'Student123!');
  await page.getByRole('button', { name: '学生端', exact: true }).click();
  await page.getByRole('button', { name: /^登录/ }).first().click();
  await page.getByText('学情工作台').first().waitFor({ timeout: 30000 });
  await sleep(1500);
  const skip = page.getByRole('button', { name: '今天先跳过' });
  if (await skip.count()) { await skip.click().catch(() => null); await sleep(1200); }
  const learning = page.locator('.nav-item').filter({ hasText: '学习中心' });
  if (await learning.count()) {
    await learning.click();
    await sleep(1800);
    const analysisTab = page.getByRole('button', { name: /我的学情/ }).first();
    if (await analysisTab.count()) { await analysisTab.click(); await sleep(3000); }
    await shot(page, '12_student_analytics');
    const kp = page.locator('.analytics-kp').first();
    if (await kp.count()) { await kp.click(); await sleep(800); }
    await page.screenshot({ path: path.join(outDir, '13_student_knowledge_detail.png'), fullPage: true });
    results.push('13_student_knowledge_detail');
  }

  const body = await page.locator('body').innerText();
  results.push('body_chars=' + body.length);
} finally {
  await browser.close();
  await fs.writeFile(path.join(outDir, 'result.json'), JSON.stringify({ screenshots: results, errors }, null, 2), 'utf8');
  console.log(JSON.stringify({ screenshots: results, errors }, null, 2));
}
