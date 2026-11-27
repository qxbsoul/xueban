import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
const outDir = 'D:/capital lion/verify-artifacts/v1.5.23';
const errors = [];
try {
  await fs.mkdir(outDir, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1500, height: 1200 } });
  await context.addInitScript(value => localStorage.setItem('sf_xueban_session', value), process.env.QA_TOKEN || '');
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-class-card').first().waitFor({ timeout: 30000 });
  await page.locator('.ta-class-card').first().click();
  await page.waitForTimeout(2500);
  const classText = (await page.locator('.teacher-main, .teacher-content, main').first().innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '01_class.png'), fullPage: false });
  const classFacts = {
    hasExpectedUndetermined: /应作答组合数|无法确定应答范围|应作答无法确定/.test(classText),
    hasClassSummary: classText.includes('班内共性问题') && classText.includes('班内差异问题'),
    hasQ038: classText.includes('Q038'),
    classSnippet: classText.slice(0, 1400),
  };
  await page.locator('.ta-tabs button, .ta-tab').filter({ hasText: '班级比较' }).click().catch(async () => {
    await page.getByText('班级比较', { exact: true }).first().click();
  });
  await page.waitForTimeout(2000);
  const compareText = (await page.locator('.teacher-main, .teacher-content, main').first().innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '02_compare.png'), fullPage: false });
  const compareFacts = {
    hasNoAdvantage: compareText.includes('无明显优势'),
    hasSampleWarning: compareText.includes('有效学生少于'),
    hasDifference: compareText.includes('班级差异说明'),
    snippet: compareText.slice(0, 1000),
  };
  await page.getByText('错误类型分析', { exact: true }).first().click();
  await page.waitForTimeout(2000);
  const errorText = (await page.locator('.teacher-main, .teacher-content, main').first().innerText()).replace(/\s+/g, ' ');
  await page.screenshot({ path: path.join(outDir, '03_errors.png'), fullPage: false });
  const errorFacts = {
    hasWeightedHeader: errorText.includes('加权次数'),
    hasActual: errorText.includes('实际次数'),
    hasUnrecognized: errorText.includes('未识别错误'),
    hasClassColumn: /涉及知识点\s*班级/.test(errorText),
    snippet: errorText.slice(0, 1000),
  };
  console.log(JSON.stringify({ classFacts, compareFacts, errorFacts, consoleErrors: errors }, null, 2));
} finally {
  await browser.close();
}
