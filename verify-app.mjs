import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(root, 'verify-artifacts');
const errors = [];
const results = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));

const browser = await chromium.launch({
  executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
});
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', e => errors.push('page: ' + e.message));

  await page.goto('http://localhost:3007/app', { waitUntil: 'networkidle' });
  await fs.mkdir(outDir, { recursive: true });
  await page.screenshot({ path: path.join(outDir, 'login.png') });

  await page.getByRole('button', { name: /游客一键体验/ }).first().click();
  await page.getByText('学情工作台', { exact: true }).first().waitFor({ timeout: 25000 });
  results.push('游客进入学情工作台');

  const navLabels = await page.locator('.nav-item .nav-text').allInnerTexts();
  results.push('侧栏：' + navLabels.join(' / '));
  const groupLabels = await page.locator('.nav-label').allInnerTexts();
  results.push('分组：' + groupLabels.join(' / '));

  const pages = ['人工审批', '运行日志', '学习计划', '学情成果', '教研团队', '学情看板', '课程资源库'];
  for (const name of pages) {
    await page.locator('.nav-item').filter({ hasText: name }).first().click();
    await sleep(900);
    const text = (await page.locator('.view.active').innerText()).trim();
    if (!text) errors.push(`${name} 页面为空`);
    results.push(`${name}：${text.split('\n')[0]}`);
    await page.screenshot({ path: path.join(outDir, `view-${name}.png`) });
  }

  // 回到工作台检查空态与新建对话入口
  await page.locator('.nav-item').filter({ hasText: '学情工作台' }).first().click();
  await sleep(600);
  await page.screenshot({ path: path.join(outDir, 'workbench.png') });
  const wbText = (await page.locator('.view.active').innerText()).trim();
  results.push('工作台首屏：' + wbText.split('\n').slice(0, 4).join(' | '));

  // 设置页
  await page.getByRole('button', { name: '打开设置' }).click();
  await sleep(700);
  await page.screenshot({ path: path.join(outDir, 'settings.png') });
  results.push('设置可打开');

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  if (overflow) errors.push('桌面页面出现横向溢出');
} catch (error) {
  errors.push('FATAL: ' + error.message);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ results, errors }, null, 2));
await fs.mkdir(outDir, { recursive: true });
await fs.writeFile(path.join(outDir, 'round1-smoke.json'), JSON.stringify({ results, errors }, null, 2));
