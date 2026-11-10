import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(root, 'verify-artifacts');
const errors = [];
const results = [];
const browser = await chromium.launch({
  executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  headless: true,
});
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', e => errors.push('page: ' + e.message));

  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'networkidle', timeout: 30000 });
  await page.getByRole('button', { name: /游客一键体验/ }).first().click();
  await page.getByText('学情工作台', { exact: true }).first().waitFor({ timeout: 25000 });

  // 1) 课程资源库上传 docx + pptx
  await page.locator('.nav-item').filter({ hasText: '课程资源库' }).first().click();
  await page.waitForTimeout(1200);
  const kbInput = page.locator('.view.active input[type="file"]').first();
  await kbInput.setInputFiles([path.join(out, 'sample.docx'), path.join(out, 'sample.pptx')]);
  await page.waitForTimeout(6000);
  const kbText = await page.locator('.view.active').innerText();
  results.push('知识库出现 docx: ' + kbText.includes('sample.docx'));
  results.push('知识库出现 pptx: ' + kbText.includes('sample.pptx'));
  results.push('知识库片段计数文本: ' + (kbText.match(/片段/g) || []).length);
  await page.screenshot({ path: path.join(out, 'kb-formats.png') });

  // 2) 对话里把 docx 入库
  await page.locator('.nav-item').filter({ hasText: '学情工作台' }).first().click();
  await page.waitForTimeout(800);
  await page.locator('.composer-file-input').setInputFiles(path.join(out, 'sample.docx'));
  await page.waitForTimeout(400);
  await page.locator('.composer-file-save').first().click();
  await page.waitForTimeout(2500);
  const toasts = await page.locator('.toast').allInnerTexts();
  results.push('对话入库提示: ' + toasts.join(' | '));
  await page.screenshot({ path: path.join(out, 'chat-docx-ingest.png') });
} catch (error) {
  errors.push('FATAL: ' + error.message);
} finally {
  await browser.close();
}
console.log(JSON.stringify({ results, errors }, null, 2));
