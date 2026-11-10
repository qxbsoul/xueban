import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(root, 'verify-artifacts');
const results = [];
const errors = [];
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
const readElapsed = async (page) => {
  const nodes = await page.locator('.thinking-elapsed').allInnerTexts();
  return nodes.join(' ').replace(/\s+/g, ' ').trim();
};
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message.slice(0, 160)));
  // 让聊天请求一直挂着，模拟“回答没完成”
  await page.route('**/api/xueban-chat/**', () => new Promise(() => {}));
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'networkidle', timeout: 30000 });
  await page.getByRole('button', { name: /游客一键体验/ }).first().click();
  await page.getByText('学情工作台', { exact: true }).first().waitFor({ timeout: 25000 });
  await page.waitForTimeout(1200);
  if (await page.locator('.profile-modal').count()) {
    await page.getByRole('button', { name: '今天先跳过' }).click();
  }
  await page.locator('textarea').first().fill('帮我分析一下这周的学习情况');
  await page.getByRole('button', { name: '发送' }).first().click();
  await page.waitForTimeout(3500);
  results.push('切走前: ' + await readElapsed(page));
  await page.getByRole('button', { name: '群聊' }).first().click();
  await page.waitForTimeout(2200);
  results.push('群聊里: ' + (await readElapsed(page) || '无计时'));
  await page.getByRole('button', { name: '单聊' }).first().click();
  await page.waitForTimeout(1500);
  results.push('切回后: ' + await readElapsed(page));
  await page.waitForTimeout(2000);
  results.push('再等 2 秒: ' + await readElapsed(page));
  await page.screenshot({ path: path.join(out, 'timer-after-switch.png') });
} catch (error) {
  errors.push('FATAL: ' + error.message);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ results, errors }, null, 2));
